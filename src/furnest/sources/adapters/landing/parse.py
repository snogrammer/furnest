"""Reading Landing's public API: URLs in, search rows, homes and quotes out.

Landing (hellolanding.com) publishes a read-only REST API that needs no auth
(``/api/public``, OpenAPI at ``/api/public/openapi.json``). Its robots.txt allows that
path and disallows the HTML search pages, and its terms forbid spiders on the site,
so only the API is used. This module is pure: it builds URLs and maps JSON, and the
adapter (``__init__``) does the fetching.

Three endpoints are read:

- ``markets``: the market and display-city slugs a search can name;
- ``search?market=<slug>&check_in=&check_out=&bedrooms=&max_price=``: the first page
  (25 homes) of homes free for the whole stay, each with its price for those dates;
- ``homes/{slug}`` and ``homes/{slug}/quote?check_in=&check_out=``: amenities, photos
  and pet policy, and whether the home can still be booked for the stay.

Landing's stays start at about a month, so :func:`stay_window` always asks for at least
one, as the Airbnb adapter does, and every listing is free for all of it.

Fields map like this:

=====================================  ===============================================
API field                              NormalizedListing
=====================================  ===============================================
``slug``                               ``source_id``
``home_url`` (dates pre-filled)        ``url``
``name``, else the address             ``title``
quote ``display_price_with_discount``  ``price`` (monthly unless the quote says nightly;
(else the search row's)                includes all fees but taxes)
``address``                            ``address`` (street, city, region, postal code)
``latitude``, ``longitude``            ``lat``, ``lng``
``bedrooms`` (0 is a studio),          ``beds``, ``baths``, ``sqft``
``bathrooms``, ``square_feet``
``available_from``                     ``available_from``, no earlier than the check-in
``minimum_nightly_stay``               ``min_lease_months``, when 28 nights or more
home ``property.amenities``, parking,  ``amenities``
laundry, ``pet_policy``
``photos`` (else ``media``,            ``images``
``hero_image_url``)
=====================================  ===============================================

Every listing is ``furnished`` and a ``property_manager``'s: Landing leases buildings
from operators. Reservations are made on Landing, so the contact is ``platform_only``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote, urlencode

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
from furnest.sources.adapters._values import half, number, text, whole
from furnest.sources.adapters.airbnb.parse import StayWindow, stay_window

SOURCE = "landing"
SITE = "https://www.hellolanding.com"
API = f"{SITE}/api/public"
_USD = "USD"
# Market and display-city slugs: lower-case words joined by hyphens ("austin", "austin-tx").
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# Landing takes a move-in at most this many days out.
_MAX_LEAD_DAYS = 365
MAX_IMAGES = 20
_MONTH_NIGHTS = 28
_POSTAL = re.compile(r"^(\d{5})(?:-\d{4})?$")
_REGION = re.compile(r"^[A-Z]{2}$")


# --- Criteria → URLs ----------------------------------------------------------------


def landing_window(criteria: Criteria, *, today: date) -> StayWindow:
    """A stay of the minimum lease (or a month) from the earliest move-in or today.

    The API takes check-ins up to a year out, so a later move-in is clamped to that.
    """
    window = stay_window(criteria, today=today)
    latest = today + timedelta(days=_MAX_LEAD_DAYS)
    if window.check_in <= latest:
        return window
    return StayWindow(latest, latest + (window.check_out - window.check_in))


def markets_url() -> str:
    return f"{API}/markets"


def search_url(market: str, window: StayWindow, criteria: Criteria) -> str:
    params: dict[str, str | int] = {
        "market": market,
        "check_in": window.check_in.isoformat(),
        "check_out": window.check_out.isoformat(),
    }
    if criteria.min_beds is not None:
        params["bedrooms"] = criteria.min_beds
    cap = criteria.price_max
    if cap is not None and cap.currency == _USD:
        # Whole dollars, rounded up: the search compares all-in prices, matching decides.
        params["max_price"] = math.ceil(cap.major)
    return f"{API}/search?{urlencode(params)}"


def home_url(slug: str) -> str:
    return f"{API}/homes/{quote(slug, safe='')}"


def quote_url(slug: str, window: StayWindow) -> str:
    query = urlencode(
        {"check_in": window.check_in.isoformat(), "check_out": window.check_out.isoformat()}
    )
    return f"{home_url(slug)}/quote?{query}"


# --- JSON → rows --------------------------------------------------------------------


def parse_markets(payload: object) -> frozenset[str]:
    """Every market slug and display-city slug. Raises ``ValueError`` if unreadable."""
    markets = payload.get("markets") if isinstance(payload, dict) else None
    if not isinstance(markets, list):
        msg = "markets response has no 'markets' list"
        raise ValueError(msg)
    slugs: set[str] = set()
    for market in markets:
        if not isinstance(market, dict):
            continue
        slugs.add(text(market.get("slug")))
        for city in market.get("cities") or ():
            if isinstance(city, dict):
                slugs.add(text(city.get("slug")))
    slugs.discard("")
    return frozenset(slugs)


def parse_search(payload: object) -> list[dict[str, Any]]:
    """The homes of a search response. Raises ``ValueError`` if it isn't one."""
    homes = payload.get("homes") if isinstance(payload, dict) else None
    if not isinstance(homes, list):
        msg = "search response has no 'homes' list"
        raise ValueError(msg)
    return [home for home in homes if isinstance(home, dict) and text(home.get("slug"))]


@dataclass(frozen=True)
class Detail:
    """A home's page and its quote for the stay, as the API returned them."""

    home: dict[str, Any]
    quote: dict[str, Any]

    @property
    def available(self) -> bool:
        """False only when the quote says so; a missing flag is not a "no"."""
        return self.quote.get("available") is not False


def parse_detail(home: object, quote_: object) -> Detail:
    if not isinstance(home, dict) or not isinstance(quote_, dict):
        msg = "home or quote response is not an object"
        raise ValueError(msg)
    return Detail(home=home, quote=quote_)


# --- Rows → NormalizedListing -------------------------------------------------------


def to_listing(
    row: dict[str, Any], detail: Detail | None, *, window: StayWindow
) -> NormalizedListing:
    """Map a search row, completed by ``detail`` when there is one.

    Raises ``ValueError`` (pydantic's included) if the row is unusable.
    """
    slug = text(row.get("slug"))
    if not slug:
        msg = "row has no slug"
        raise ValueError(msg)
    home = detail.home if detail else {}
    quoted = detail.quote if detail else {}
    # The search row carries the stay's dates in its link; the home's own link doesn't.
    link = text(row.get("home_url")) or text(quoted.get("home_url")) or f"{SITE}/homes/{slug}"
    address = text(row.get("address")) or text(home.get("address"))
    lat = number(row.get("latitude"))
    lng = number(row.get("longitude"))
    if lat is None or lng is None:
        lat = lng = None
    beds = whole(row.get("bedrooms"))
    nights = whole(row.get("minimum_nightly_stay"))
    available = _date(row.get("available_from"))
    raw: dict[str, Any] = {"search": row}
    if detail:
        raw["home"] = home
        raw["quote"] = quoted
    return NormalizedListing(
        source=SOURCE,
        source_id=slug,
        url=link,  # type: ignore[arg-type]
        title=text(row.get("name")) or text(home.get("name")) or address or "Landing home",
        description="",
        price=_price(quoted, row),
        address=_address(row, address),
        lat=lat,
        lng=lng,
        beds=beds,
        baths=half(row.get("bathrooms")),
        sqft=whole(row.get("square_feet")) or None,
        property_type=PropertyType.STUDIO if beds == 0 else PropertyType.APARTMENT,
        owner_type=OwnerType.PROPERTY_MANAGER,
        furnished=True,
        amenities=_amenities(home),
        available_from=max(available, window.check_in) if available else window.check_in,
        min_lease_months=1 if nights is not None and nights >= _MONTH_NIGHTS else None,
        images=_photos(home, quoted, row),  # type: ignore[arg-type]
        contact=Contact(platform_only=True),
        raw=raw,
    )


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(text(value))
    except ValueError:
        return None


def _price(quoted: dict[str, Any], row: dict[str, Any]) -> Price | None:
    """The stay's price from the quote, else from the search row (also for the stay)."""
    for source in (quoted, row):
        amount = number(source.get("display_price_with_discount"))
        if amount is None or amount <= 0:
            continue
        currency = text(source.get("currency")) or _USD
        if currency != _USD:
            continue
        period = (
            RentPeriod.NIGHTLY if source.get("is_price_nightly") is True else RentPeriod.MONTHLY
        )
        return Price(amount=Money.from_major(str(amount), currency), period=period)
    return None


def _address(row: dict[str, Any], full: str) -> Address:
    """Street, city, state and ZIP from "2000 2nd Ave S., Exampleville, AL 35233"."""
    city = text(row.get("city")) or None
    region = text(row.get("state")).upper() or None
    parts = [part.strip() for part in full.split(",") if part.strip()]
    line1: str | None = None
    postal: str | None = None
    if len(parts) >= 3:
        line1 = parts[0]
        state, _, zip_code = parts[-1].partition(" ")
        if _REGION.fullmatch(state):
            region = region or state
            if found := _POSTAL.match(zip_code.strip()):
                postal = found.group(1)
        city = city or parts[-2]
    return Address(
        line1=line1,
        city=city,
        region=region if region and _REGION.fullmatch(region) else None,
        postal_code=postal,
    )


def _amenities(home: dict[str, Any]) -> frozenset[str]:
    tags: set[str] = set()
    prop = home.get("property")
    for label in (prop.get("amenities") if isinstance(prop, dict) else None) or ():
        if isinstance(label, str) and label.strip():
            tags.add(label.strip().lower())
    if home.get("has_parking") is True:
        tags.add("parking")
    laundry = text(home.get("laundry_type")).lower()
    if laundry:
        tags.add(f"{laundry} laundry")
    pets = home.get("pet_policy")
    if isinstance(pets, dict):
        cats, dogs = pets.get("cats_allowed"), pets.get("dogs_allowed")
        if cats is True:
            tags.add("cats allowed")
        if dogs is True:
            tags.add("dogs allowed")
        if cats is False and dogs is False:
            tags.add("no pets")
    return frozenset(tags)


def _photos(*sources: dict[str, Any]) -> tuple[str, ...]:
    """Photo URLs in the API's order, from the first source that has any, else a hero."""
    urls: list[str] = []
    for source in sources:
        for key in ("photos", "media"):
            entries = source.get(key)
            for entry in entries if isinstance(entries, list) else ():
                url = entry.get("url") if isinstance(entry, dict) else None
                if isinstance(url, str) and url.startswith("https://") and url not in urls:
                    urls.append(url)
        if urls:
            break
    if not urls:
        for source in sources:
            hero = source.get("hero_image_url")
            if isinstance(hero, str) and hero.startswith("https://"):
                urls.append(hero)
                break
    return tuple(urls[:MAX_IMAGES])
