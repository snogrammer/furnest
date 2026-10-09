"""Reading Blueground's city pages: URLs in, a page of rows out, rows to listings.

Blueground (theblueground.com) has no API, but its city pages
(``/furnished-apartments-<city>-usa``) answer plain HTTP with no bot check, and
robots.txt allows them. Each page carries the same homes twice:

- a JSON-LD list of ``Apartment`` items (beds, baths, floor size, coordinates, one
  photo, a street address), and
- ``window.__INITIAL_STATE__``, whose ``properties.allProperties.main`` holds the
  full rows (id, path, building and neighbourhood, availability, price, photos).

The state is the primary source; JSON-LD fills in what a row lacks. A page lists 18
homes and takes ``?page=<n>``. Without dates every price is 0, so a search passes
``checkIn`` and ``checkOut``: the page then lists only homes free for the stay, each
with its monthly rent for those dates. Blueground's terms forbid automated access,
so the adapter stays off by default (see ``docs/adapters.md``).

This module is pure: it builds URLs and maps text, and the adapter (``__init__``)
does the fetching.

=====================================  ===============================================
Page field                             NormalizedListing
=====================================  ===============================================
``path`` (``p/furnished-apartments/    ``source_id`` (``chi-819``) and ``url``
chi-819``)
``name``                               ``title``
``rent.amount`` (0 means no stay       ``price``, monthly USD
was asked for)
``address`` (``building``, ``city``,   ``address`` (street from ``name``, city, state
``level2``) and the page's state       from the page, neighbourhood)
``address.lat``, ``address.lng``       ``lat``, ``lng``
``bedrooms`` (0 is a studio),          ``beds``, ``baths``, ``sqft``
``bathrooms``, ``lotSize``
``availableFrom``                      ``available_from``
photo captions, utilities              ``amenities``
``photos`` (else the JSON-LD photo)    ``images``
=====================================  ===============================================

Every listing is ``furnished``, a ``property_manager``'s (Blueground leases and
operates its homes, with no broker fee) and reserved on Blueground, so its contact is
``platform_only``. Stays start at 30 days, so ``min_lease_months`` is 1.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from furnest.domain import (
    Address,
    Contact,
    Criteria,
    Money,
    NormalizedListing,
    OwnerType,
    Price,
    PropertyType,
    RentPeriod,
)
from furnest.sources.adapters._values import half, number, positive, text, whole
from furnest.sources.adapters.airbnb.parse import StayWindow, stay_window

SOURCE = "blueground"
SITE = "https://www.theblueground.com"
_USD = "USD"
# City slugs: lower-case words joined by hyphens ("chicago", "new-york", "chicago-usa").
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
PAGE_SIZE = 18
MAX_IMAGES = 20
# Blueground's shortest stay.
_MIN_NIGHTS = 30
# ``p/furnished-apartments/chi-819``: the home's path, its last part the id.
_PATH = re.compile(r"p/[a-z0-9-]+/(?P<id>[a-z0-9]+(?:-[a-z0-9]+)*)")
_STATE_MARKER = "window.__INITIAL_STATE__="
_JSON_LD = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)
_REGION = re.compile(r"^[A-Z]{2}$")
# Photo captions that say something the row doesn't.
_CAPTION_AMENITIES = {
    "washerDryerInApartment": "in-unit laundry",
    "washerInApartment": "in-unit laundry",
    "inBuildingSharedLaundryRoom": "shared laundry",
    "buildingsFitnessCenter": "gym",
    "buildingsSwimmingPool": "pool",
    "buildingsIndoorSwimmingPool": "pool",
    "buildingsSauna": "sauna",
    "buildingsRooftop": "rooftop",
    "balcony": "balcony",
    "balconyView": "balcony",
    "queenSizeBed": "queen bed",
    "officeArea": "workspace",
}


@dataclass(frozen=True)
class CityPage:
    """One page of a city's homes."""

    rows: list[dict[str, Any]]
    # Homes across all pages for the stay.
    total: int
    region: str | None = None
    # JSON-LD ``Apartment`` items by home path.
    ld: dict[str, dict[str, Any]] = field(default_factory=dict)


# --- Criteria → URLs ----------------------------------------------------------------


def blueground_window(criteria: Criteria, *, today: date) -> StayWindow:
    """A stay of the minimum lease (or a month) from the earliest move-in or today.

    Never under 30 nights, Blueground's shortest stay.
    """
    window = stay_window(criteria, today=today)
    if window.nights >= _MIN_NIGHTS:
        return window
    return StayWindow(window.check_in, window.check_in + timedelta(days=_MIN_NIGHTS))


def city_path(slug: str) -> str:
    """``chicago`` → ``furnished-apartments-chicago-usa`` (``chicago-usa`` as is)."""
    return f"furnished-apartments-{slug if slug.endswith('-usa') else f'{slug}-usa'}"


def city_url(slug: str, window: StayWindow, page: int = 1) -> str:
    params: dict[str, str | int] = {
        "checkIn": window.check_in.isoformat(),
        "checkOut": window.check_out.isoformat(),
    }
    if page > 1:
        params["page"] = page
    return f"{SITE}/{city_path(slug)}?{urlencode(params)}"


def home_url(path: str) -> str:
    return f"{SITE}/{path}"


# --- Page → rows --------------------------------------------------------------------


def parse_city_page(html: str) -> CityPage:
    """The homes on a city page. Raises ``ValueError`` if it isn't one.

    A page without the state, or with a state that has no home list, must not read as
    "no listings": that would mark every stored listing missing.
    """
    state = _initial_state(html)
    properties = state.get("properties")
    listed = properties.get("allProperties") if isinstance(properties, dict) else None
    main = listed.get("main") if isinstance(listed, dict) else None
    if not isinstance(main, list):
        raise ValueError("the Blueground page has no home list (did its layout change?)")
    rows = [row for row in main if isinstance(row, dict)]
    meta = properties.get("meta") if isinstance(properties, dict) else None
    total = whole(meta.get("totalItems")) if isinstance(meta, dict) else None
    return CityPage(
        rows=rows,
        total=total if total is not None else len(rows),
        region=_region(state),
        ld=_apartments(html),
    )


def _initial_state(html: str) -> dict[str, Any]:
    start = html.find(_STATE_MARKER)
    if start < 0:
        raise ValueError("the Blueground page has no __INITIAL_STATE__ (did its layout change?)")
    try:
        state, _ = json.JSONDecoder().raw_decode(html[start + len(_STATE_MARKER) :])
    except ValueError as exc:
        raise ValueError(f"the Blueground __INITIAL_STATE__ is not JSON: {exc}") from exc
    if not isinstance(state, dict):
        raise ValueError("the Blueground __INITIAL_STATE__ is not an object")
    return state


def _region(state: dict[str, Any]) -> str | None:
    """The state's two-letter code, from the city's ancestry."""
    config = state.get("configuration")
    initial = config.get("initialData") if isinstance(config, dict) else None
    info = initial.get("cityInfo") if isinstance(initial, dict) else None
    ancestry = info.get("ancestry") if isinstance(info, dict) else None
    for place in ancestry if isinstance(ancestry, list) else ():
        if isinstance(place, dict) and place.get("type") == "state":
            code = text(place.get("shortName")).upper()
            return code if _REGION.fullmatch(code) else None
    return None


def _apartments(html: str) -> dict[str, dict[str, Any]]:
    """The page's JSON-LD ``Apartment`` items, by the home path in their URL."""
    found: dict[str, dict[str, Any]] = {}
    for block in _JSON_LD.findall(html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict) or item.get("@type") != "Apartment":
                continue
            url = text(item.get("url"))
            if url.startswith(f"{SITE}/"):
                found[url.removeprefix(f"{SITE}/")] = item
    return found


# --- Row → listing ------------------------------------------------------------------


def to_listing(row: dict[str, Any], page: CityPage) -> NormalizedListing:
    """Map a row. Raises ``ValueError`` if it has no usable path, or no name."""
    path = text(row.get("path")).strip("/")
    match = _PATH.fullmatch(path)
    if match is None:
        raise ValueError(f"home has no usable path: {row.get('path')!r}")
    ld = page.ld.get(path, {})
    address = row.get("address")
    place = address if isinstance(address, dict) else {}
    name = text(row.get("name")) or text(ld.get("name"))
    if not name:
        raise ValueError(f"home {path} has no name")

    lat, lng = _coordinates(place, ld)
    beds = whole(row.get("bedrooms"))
    if beds is None:
        beds = whole(ld.get("numberOfBedrooms"))
    baths = half(row.get("bathrooms"))
    if baths is None:
        baths = half(ld.get("numberOfBathroomsTotal"))
    sqft = positive(row.get("lotSize"))
    if sqft is None:
        floor = ld.get("floorSize")
        sqft = positive(floor.get("value")) if isinstance(floor, dict) else None
    neighborhood = text(place.get("level2")) or None
    city = text(place.get("city")) or None

    return NormalizedListing(
        source=SOURCE,
        source_id=match.group("id"),
        url=home_url(path),  # type: ignore[arg-type]
        title=name,
        description=_description(beds, neighborhood, city, row),
        price=_price(row),
        address=Address(
            line1=_street(name, text(place.get("building"))),
            city=city,
            region=page.region,
            neighborhood=neighborhood,
        ),
        lat=lat,
        lng=lng,
        beds=beds,
        baths=baths,
        sqft=sqft,
        property_type=PropertyType.STUDIO if beds == 0 else PropertyType.APARTMENT,
        owner_type=OwnerType.PROPERTY_MANAGER,
        furnished=True,
        amenities=_amenities(row),
        available_from=_date(row.get("availableFrom")),
        min_lease_months=1,
        images=_photos(row, ld),  # type: ignore[arg-type]
        contact=Contact(platform_only=True),
        raw=_raw(row),
    )


def _price(row: dict[str, Any]) -> Price | None:
    """The monthly rent for the searched stay; 0 means the page was loaded without one."""
    rent = row.get("rent")
    if not isinstance(rent, dict):
        return None
    amount = number(rent.get("amount"))
    if amount is None or amount <= 0 or text(rent.get("currency")) not in ("", _USD):
        return None
    return Price(amount=Money.from_major(str(amount), _USD), period=RentPeriod.MONTHLY)


def _coordinates(place: dict[str, Any], ld: dict[str, Any]) -> tuple[float | None, float | None]:
    for lat, lng in (
        (place.get("lat"), place.get("lng")),
        (ld.get("latitude"), ld.get("longitude")),
    ):
        found = number(lat), number(lng)
        if found[0] is not None and found[1] is not None:
            return found
    return None, None


def _street(name: str, building: str) -> str | None:
    """``3Eleven, 311 W Illinois St`` is the building then the street; some are just a street."""
    if building and name.startswith(f"{building},"):
        name = name.removeprefix(f"{building},").strip()
    return name or None


def _description(
    beds: int | None, neighborhood: str | None, city: str | None, row: dict[str, Any]
) -> str:
    size = "Furnished studio" if beds == 0 else "Furnished apartment"
    if beds:
        size = f"Furnished {beds}-bedroom apartment"
    where = ", ".join(part for part in (neighborhood, city) if part)
    sentence = f"{size} in {where}." if where else f"{size}."
    if row.get("utilitiesIncludedInRent") is True:
        sentence += " Utilities included."
    return sentence


def _amenities(row: dict[str, Any]) -> frozenset[str]:
    tags: set[str] = set()
    if row.get("utilitiesIncludedInRent") is True:
        tags.add("utilities included")
    for photo in _photo_entries(row):
        if tag := _CAPTION_AMENITIES.get(text(photo.get("captionKey"))):
            tags.add(tag)
    return frozenset(tags)


def _photo_entries(row: dict[str, Any]) -> list[dict[str, Any]]:
    photos = row.get("photos")
    return (
        [photo for photo in photos if isinstance(photo, dict)] if isinstance(photos, list) else []
    )


def _photos(row: dict[str, Any], ld: dict[str, Any]) -> tuple[str, ...]:
    """The home's own photos first, then the building's; else the JSON-LD photo."""
    own: list[str] = []
    building: list[str] = []
    for photo in _photo_entries(row):
        url = text(photo.get("url"))
        tags = photo.get("tags")
        bucket = building if isinstance(tags, list) and "building" in tags else own
        if url.startswith("https://") and url not in own and url not in building:
            bucket.append(url)
    urls = [*own, *building]
    if not urls:
        shot = ld.get("photo")
        url = text(shot.get("contentUrl")) if isinstance(shot, dict) else ""
        if url.startswith("https://"):
            urls.append(url.split("?", 1)[0])
    return tuple(urls[:MAX_IMAGES])


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(text(value))
    except ValueError:
        return None


def _raw(row: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "id",
        "code",
        "cityCode",
        "businessModel",
        "specialPrice",
        "hasJuniorBedroom",
        "highestFloor",
        "utilitiesIncludedInRent",
        "baseRent",
        "rent",
    )
    return {key: row[key] for key in keep if key in row}
