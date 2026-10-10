"""Mapping between furnest and the Zumper data provider's JSON.

The adapter gets Zumper rentals through the Apify actor ``memo23/zumper-cheerio`` (see
``docs/adapters.md`` for why, and how it compares with
``benthepythondev/zumper-rental-scraper``). This module is pure: :func:`build_input`
turns criteria into the actor's input, and :func:`parse_item` turns one dataset item
into listings.

The actor takes Zumper URLs and nothing else. :func:`build_input` turns each search
location into Zumper's furnished search, ``/apartments-for-rent/<city>-<st>/furnished``.
Live runs (#196) showed that path is honoured (10 of 15 rows furnished, against 1 of 15
without it) while ``price-max`` and ``beds`` query params are ignored, so price, beds
and the move-in date are left to matching.

A dataset item is one Zumper page, with its record under ``entity.data`` (or
``activeListings[0]``, or at the top level):

- a ``Building``, whose units are in ``floorplan_listings``: each unit becomes a
  listing, with the building filling in what the unit lacks (address, coordinates,
  description, photos);
- a ``Listing``: one rental with its own ``listing_id`` and ``listing_location``.

The furnished search still returns buildings where only some units are furnished, so
**each unit is checked**: it is kept when its own ``amenity_tags`` include
"Furnished", or its title or description says furnished (not "unfurnished"). Other
units are dropped. A building's own tags ("Furnished Units Available") don't count.

========================================  ===========================================
actor field (unit, else its building)     NormalizedListing
========================================  ===========================================
``listing_id``                            ``source_id``
``url``                                   ``url`` (a unit links to its building)
``title`` (prefixed by the building       ``title``
``name``), ``description``                ``description``
``price`` / ``min_price``                 ``price`` per month (``currency_code`` 1 is
                                          USD)
``address``, ``listing_location`` or      ``address`` (US)
the building's ``city``/``state``/
``zipcode``
``lat``, ``lng``                          ``lat``, ``lng``
``bedrooms``, ``bathrooms`` (+ half),     ``beds``, ``baths``, ``sqft``
``square_feet``
``property_type`` (4 is an apartment)     ``property_type`` (a studio at 0 beds)
``min_lease_days``                        ``min_lease_months`` (rounded, at least 1)
``date_available`` (``YYYY/MM/DD``)       ``available_from``
``media[].media_id``                      ``images`` on Zumper's image CDN
``agent_name``, ``brokerage_name``,       ``owner_type`` hint: a name that reads as a
``listing_agents[]`` names                property manager, else a named agent or
                                          brokerage is an agent, else unknown
========================================  ===========================================

``contact.platform_only`` is set unless the unit or building carries a ``phone``. The
agent, brokerage, email and partner-link fields name people or businesses, so they
stay out of ``raw``, along with the photo list and price history.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from furnest.domain import (
    Address,
    Contact,
    Criteria,
    Money,
    NormalizedListing,
    OwnerType,
    Price,
    PropertyType,
    infer_owner_type,
)
from furnest.sources.adapters._values import half, number, positive, text, whole

SOURCE = "zumper"
ACTOR = "memo23/zumper-cheerio"
SITE = "https://www.zumper.com"
IMAGE_CDN = "https://img.zumpercdn.com"
# Zumper's property_type id for an apartment: 340 of 350 live units in apartment
# buildings carried it. 13 was seen once, on a room in a shared house; other ids stay
# ``other`` until their meaning is confirmed.
_APARTMENT = 4
_USD = 1  # Zumper's currency_code for US dollars, on every live row
_DAYS_PER_MONTH = 30

# Bulky blocks that aren't about the rental, and fields naming people or businesses.
_RAW_EXCLUDED = frozenset(
    {
        "floorplan_listings",
        "media",
        "image_ids",
        "prices",
        "agents",
        "listing_agents",
        "agent_name",
        "agent_email",
        "agent_phone",
        "agent_id",
        "brokerage_name",
        "brokerage_email",
        "brokerage_phone",
        "phone",
        "email",
        "contactEmail",
        "contactWebsite",
        "emailEnrichment",
        "provider_url",
        "homepage",
        "supplemental_content",
    }
)
_FURNISHED = re.compile(r"(?<!un)\bfurnished\b|\bfully[- ]furnished\b", re.IGNORECASE)
_MANAGER_NAME = re.compile(
    r"\b(?:management|properties|property|apartments|residential|realty group|communities|"
    r"holdings|living|rentals)\b",
    re.IGNORECASE,
)
_NON_SLUG = re.compile(r"[^a-z0-9]+")


# --- Criteria → actor input -------------------------------------------------------


def build_input(
    criteria: Criteria, *, locations: tuple[str, ...], max_results: int
) -> dict[str, Any]:
    # The /furnished path is the only filter the actor was seen to honour (#196);
    # price and beds params are ignored, so matching applies them.
    urls = [
        f"{SITE}/apartments-for-rent/{slug}/furnished" for slug in map(_slug, locations) if slug
    ]
    return {"startUrls": urls, "maxItems": max_results}


def _slug(location: str) -> str:
    """``Springfield, IL`` → ``springfield-il``."""
    return _NON_SLUG.sub("-", location.lower()).strip("-")


# --- Actor item → NormalizedListing -------------------------------------------------


def parse_item(item: dict[str, Any], *, today: date) -> list[NormalizedListing]:
    """Map one dataset item to its furnished listings: none, one, or one per unit.

    Raises ``ValueError`` (pydantic's included) if the item has neither a
    ``listing_id`` nor units.
    """
    core = _core(item)
    units = core.get("floorplan_listings")
    if isinstance(units, list):
        building = core
        rows: list[dict[str, Any]] = [u for u in units if isinstance(u, dict)]
    elif _listing_id(core) is not None:
        building, rows = {}, [core]
    else:
        msg = "item has no listing_id or units"
        raise ValueError(msg)
    return [_listing(unit, building, today=today) for unit in _furnished(rows)]


def _furnished(units: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for unit in units:
        if _listing_id(unit) is not None and _is_furnished(unit):
            yield unit


def _is_furnished(unit: dict[str, Any]) -> bool:
    tags = unit.get("amenity_tags")
    if isinstance(tags, list) and "Furnished" in tags:
        return True
    return any(_FURNISHED.search(text(unit.get(k))) for k in ("title", "description"))


def _listing(unit: dict[str, Any], building: dict[str, Any], *, today: date) -> NormalizedListing:
    source_id = _listing_id(unit) or ""

    def pick(key: str) -> object:
        value = unit.get(key)
        return value if value not in (None, "", []) else building.get(key)

    title = _title(unit, building)
    description = " ".join(
        text(pick("description")).split() or text(pick("short_description")).split()
    )
    address = _address(unit, building)
    beds = whole(unit.get("bedrooms"))
    phone = text(pick("phone")) or None
    return NormalizedListing(
        source=SOURCE,
        source_id=source_id,
        url=_url(unit, building, source_id),  # type: ignore[arg-type]
        title=title or address.line1 or "Rental",
        description=description,
        price=_price(unit),
        address=address,
        lat=_coordinate(unit, building, "lat"),
        lng=_coordinate(unit, building, "lng"),
        beds=beds,
        baths=_baths(unit),
        sqft=positive(unit.get("square_feet")),
        property_type=_property_type(unit, beds),
        owner_type=infer_owner_type(title, description, hint=_owner_type(unit, building)),
        furnished=True,
        available_from=_available_from(unit.get("date_available"), today),
        min_lease_months=_lease_months(pick("min_lease_days")),
        images=_images(pick("media")),  # type: ignore[arg-type]
        contact=Contact(phone=phone) if phone else Contact(platform_only=True),
        raw=_raw(unit),
    )


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _core(item: dict[str, Any]) -> dict[str, Any]:
    """The page's record: ``entity.data``, ``activeListings[0]`` or the item itself."""
    data = _object(_object(item.get("entity")).get("data"))
    if data:
        return data
    active = item.get("activeListings")
    if isinstance(active, list) and active and isinstance(active[0], dict):
        return active[0]
    return item


def _listing_id(record: dict[str, Any]) -> str | None:
    value = record.get("listing_id")
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    return str(value).strip() or None


def _title(unit: dict[str, Any], building: dict[str, Any]) -> str:
    """A unit's title is often a floorplan code ("1x1-A"), so the building names it."""
    own, name = text(unit.get("title")), text(building.get("name"))
    if not name or name.lower() in own.lower():
        return own
    return f"{name}: {own}" if own else name


def _url(unit: dict[str, Any], building: dict[str, Any], source_id: str) -> str:
    for source in (unit, building):
        value = text(source.get("url"))
        if value.startswith(f"{SITE}/"):
            return value
        if value.startswith("/"):
            return f"{SITE}{value}"
    # Not seen live: every row so far had a URL.
    return f"{SITE}/apartments/{source_id}"


def _price(unit: dict[str, Any]) -> Price | None:
    found = number(unit.get("price"))
    if found is None or found <= 0:
        found = number(unit.get("min_price"))
    if found is None or found <= 0:
        return None
    currency = _currency(unit.get("currency_code"))
    if currency is None:
        return None
    try:
        return Price(amount=Money.from_major(Decimal(str(found)), currency))
    except (InvalidOperation, ValueError):
        return None


def _currency(code: object) -> str | None:
    """USD for Zumper's id 1 (or none given), an ISO code as is, else unknown."""
    if code is None or code == _USD:
        return "USD"
    named = text(code).upper()
    return named if len(named) == 3 and named.isalpha() else None


def _address(unit: dict[str, Any], building: dict[str, Any]) -> Address:
    place = _object(unit.get("listing_location"))

    def part(key: str) -> str | None:
        return text(place.get(key)) or text(building.get(key)) or None

    return Address(
        line1=text(unit.get("address")) or text(building.get("address")) or None,
        city=part("city"),
        region=part("state"),
        postal_code=part("zipcode"),
        neighborhood=text(building.get("neighborhood_name")) or None,
    )


def _coordinate(unit: dict[str, Any], building: dict[str, Any], key: str) -> float | None:
    for source in (_object(unit.get("listing_location")), unit, building):
        lat, lng = number(source.get("lat")), number(source.get("lng"))
        if lat is not None and lng is not None:
            return lat if key == "lat" else lng
    return None


def _baths(unit: dict[str, Any]) -> float | None:
    full = half(unit.get("bathrooms"))
    if full is None:
        return None
    extra = whole(unit.get("half_bathrooms")) or 0
    return full + 0.5 * extra


def _property_type(unit: dict[str, Any], beds: int | None) -> PropertyType:
    if beds == 0:
        return PropertyType.STUDIO
    if whole(unit.get("property_type")) == _APARTMENT:
        return PropertyType.APARTMENT
    return PropertyType.OTHER


def _owner_type(unit: dict[str, Any], building: dict[str, Any]) -> OwnerType:
    """Who lists the place, from the agent and brokerage names (#113).

    A name that reads as a management company is a property manager; any other named
    agent or brokerage is an agent. Names stay out of ``raw``.
    """
    names = [text(building.get(k)) for k in ("brokerage_name", "agent_name")]
    for agent in unit.get("listing_agents") or ():
        names += [text(_object(agent).get(k)) for k in ("company_name", "long_name")]
    if any(_MANAGER_NAME.search(n) for n in names if n):
        return OwnerType.PROPERTY_MANAGER
    if any(names):
        return OwnerType.AGENT
    return OwnerType.UNKNOWN


def _available_from(value: object, today: date) -> date | None:
    when = text(value)
    if when.lower() in {"now", "available now", "immediately"}:
        return today
    try:
        return date.fromisoformat(when[:10].replace("/", "-"))
    except ValueError:
        return None


def _lease_months(days: object) -> int | None:
    found = whole(days)
    return max(1, round(found / _DAYS_PER_MONTH)) if found else None


def _images(media: object) -> tuple[str, ...]:
    if not isinstance(media, list):
        return ()
    ids = [_object(m).get("media_id") for m in media]
    return tuple(
        f"{IMAGE_CDN}/{i}/1280x960"
        for i in ids
        if isinstance(i, int | str) and not isinstance(i, bool) and str(i).isdigit()
    )


def _raw(unit: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in unit.items() if k not in _RAW_EXCLUDED}
