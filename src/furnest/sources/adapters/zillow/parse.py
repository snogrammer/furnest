"""Mapping between furnest and the Zillow data provider's JSON.

The adapter gets Zillow rentals through the Apify actor ``igolaizola/zillow-scraper-ppe``
(see ``docs/adapters.md`` for why). This module is pure: :func:`build_input` turns
criteria into the actor's input, and :func:`parse_item` turns one dataset item into
listings.

The input always asks for furnished rentals, which is Zillow's own "Furnished"
filter. Criteria the actor can filter on are sent along to keep billed runs small:
entire place vs room, minimum beds and baths, and the latest move-in date. Price is
*not* sent. The actor documents rent prices as "per week" while its own rent example
uses monthly figures, and guessing wrong would silently drop matches, so matching
filters price instead. Locations come from the adapter's ``search_locations``.

A rent search returns two kinds of item, told apart by ``_type``:

- ``property``: one rentable home, mapped to one listing;
- ``propertyGroup``: an apartment building, with ``unitsGroup`` summarising its units
  by bedroom count. Each summary becomes its own listing
  (``<building id>-<beds>bd``) priced at that group's lowest rent, so beds and price
  match as they would for a single unit.

Item fields, as the actor documents them, map like this:

========================================  ===========================================
actor field                               NormalizedListing
========================================  ===========================================
``zpid`` (else ``lotId``)                 ``source_id``
``url`` (else ``hdpUrl``, else            ``url``
``/homedetails/<zpid>_zpid/``)
``buildingName`` (else ``title``),       ``title`` ("N bd in City" when none)
``address``
``_details.description``                  ``description``
``price.value`` / ``unitsGroup.minPrice`` ``price`` per month, USD
``address`` (street, city, state, zip)    ``address`` (US)
``location.latitude``, ``.longitude``     ``lat``, ``lng``
``bedrooms``, ``bathrooms``               ``beds``, ``baths`` (to the nearest half)
``livingArea``                            ``sqft``
``propertyType`` / ``homeType``           ``property_type`` (a room when
                                          ``isRoomForRent`` is set on the item,
                                          its ``rental`` or the unit group)
``_details.resoFacts`` feature lists      ``amenities``, as the words Zillow uses
``resoFacts.availabilityDate`` (epoch     ``available_from``
ms, or a date string)
``_details.resoFacts.leaseTerm``          ``min_lease_months`` when it names months
``media`` photos                          ``images``
========================================  ===========================================

Every listing is ``furnished`` (the search asked for it), and ``contact.platform_only``
is set: Zillow routes renters through its own contact form. Agent and broker details
are dropped from ``raw`` because they name people.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from furnest.domain import (
    Address,
    Contact,
    Criteria,
    Money,
    NormalizedListing,
    Price,
    PropertyType,
    normalize_property_type,
    parse_price,
)
from furnest.sources.adapters._values import half, number, positive, text, whole

SOURCE = "zillow"
ACTOR = "igolaizola/zillow-scraper-ppe"
SITE = "https://www.zillow.com"
_USD = "USD"
GROUP = "propertyGroup"

# Item keys left out of ``raw``: they name agents, brokers or the people listing.
_RAW_EXCLUDED = frozenset({"propertyDisplayRules", "attributionInfo", "listingAgent", "contact"})
# Of ``_details``, only these go into ``raw``; the rest (price history, nearby homes,
# schools) is large and not about the rental.
_RAW_DETAILS = ("description", "resoFacts")
# ``resoFacts`` lists that hold amenity words.
_FEATURE_LISTS = (
    "appliances",
    "cooling",
    "heating",
    "interiorFeatures",
    "parkingFeatures",
    "communityFeatures",
    "exteriorFeatures",
    "otherFacts",
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_MONTHS = re.compile(r"(\d+)\s*(?:-\s*)?(?:mo|month)", re.IGNORECASE)
_DATE_FORMATS = ("%m/%d/%Y", "%b %d, %Y", "%B %d, %Y")


# --- Criteria → actor input -------------------------------------------------------


def build_input(
    criteria: Criteria, *, locations: tuple[str, ...], max_results: int, today: date
) -> dict[str, Any]:
    run_input: dict[str, Any] = {
        "operation": "rent",
        "locations": list(locations),
        "furnished": True,
        "space": _space(criteria),
        "sortBy": "newest",
        "fetchDetails": True,
        "maxItems": max_results,
    }
    # The actor reads 0 as "any" and -1 as studio, so 0 beds sends nothing.
    if criteria.min_beds is not None and criteria.min_beds > 0:
        run_input["minBeds"] = criteria.min_beds
    if criteria.min_baths is not None and criteria.min_baths >= 1:
        # The actor takes whole baths; rounding down only widens the search.
        run_input["minBaths"] = math.floor(criteria.min_baths)
    if criteria.move_in_latest is not None and criteria.move_in_latest >= today:
        run_input["moveInDate"] = criteria.move_in_latest.isoformat()
    return run_input


def _space(criteria: Criteria) -> str:
    allow = set(criteria.property_types_allow)
    if allow == {PropertyType.ROOM}:
        return "roomForRent"
    if PropertyType.ROOM in criteria.property_types_deny or (
        allow and PropertyType.ROOM not in allow
    ):
        return "entirePlace"
    return ""


# --- Actor item → NormalizedListing -------------------------------------------------


def parse_item(item: dict[str, Any], *, today: date) -> list[NormalizedListing]:
    """Map one dataset item: one listing, or one per unit group of a building.

    Raises ``ValueError`` (pydantic's included) if the item is unusable.
    """
    if item.get("_type") == GROUP:
        return _parse_group(item, today=today)
    return [_parse_property(item, today=today)]


def _parse_property(item: dict[str, Any], *, today: date) -> NormalizedListing:
    zpid = _id(item.get("zpid"))
    if zpid is None:
        msg = "item has no zpid"
        raise ValueError(msg)
    beds = whole(item.get("bedrooms"))
    return _listing(
        item,
        source_id=zpid,
        url=_url(item) or f"{SITE}/homedetails/{zpid}_zpid/",
        beds=beds,
        price=_money(_dig(item, "price", "value")),
        today=today,
    )


def _parse_group(item: dict[str, Any], *, today: date) -> list[NormalizedListing]:
    building = _id(item.get("zpid")) or _id(item.get("lotId"))
    if building is None:
        msg = "building has no zpid or lotId"
        raise ValueError(msg)
    url = _url(item)
    if url is None:
        msg = f"building {building} has no URL"
        raise ValueError(msg)
    listings: list[NormalizedListing] = []
    for group in item.get("unitsGroup") or ():
        if not isinstance(group, dict):
            continue
        beds = whole(group.get("bedrooms", group.get("beds")))
        if beds is None:
            continue
        listings.append(
            _listing(
                item,
                source_id=f"{building}-{beds}bd",
                url=url,
                beds=beds,
                price=_money(group.get("minPrice", group.get("price"))),
                today=today,
                room=group.get("isRoomForRent"),
            )
        )
    if not listings:
        msg = f"building {building} has no unit groups"
        raise ValueError(msg)
    return listings


def _listing(
    item: dict[str, Any],
    *,
    source_id: str,
    url: str,
    beds: int | None,
    price: Price | None,
    today: date,
    room: object = None,
) -> NormalizedListing:
    details = _object(item.get("_details"))
    facts = _object(details.get("resoFacts"))
    address = _address(item)
    lat = number(_dig(item, "location", "latitude"))
    lng = number(_dig(item, "location", "longitude"))
    if lat is None or lng is None:
        lat = lng = None
    return NormalizedListing(
        source=SOURCE,
        source_id=source_id,
        url=url,  # type: ignore[arg-type]
        title=_title(item, address, beds),
        description=text(details.get("description")),
        price=price,
        address=address,
        lat=lat,
        lng=lng,
        beds=beds,
        baths=half(item.get("bathrooms")),
        sqft=positive(item.get("livingArea")),
        property_type=_property_type(item, beds, room=room),
        furnished=True,
        amenities=_amenities(facts),
        available_from=_available_from(item, details, facts, today),
        min_lease_months=_lease_months(facts),
        images=_photos(item),  # type: ignore[arg-type]
        contact=Contact(platform_only=True),
        raw=_raw(item, details),
    )


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dig(item: dict[str, Any], *path: str) -> object:
    value: object = item
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    return str(value).strip() or None


def _url(item: dict[str, Any]) -> str | None:
    for key in ("url", "hdpUrl"):
        value = text(item.get(key))
        if value.startswith(f"{SITE}/"):
            return value
        if value.startswith("/"):
            return f"{SITE}{value}"
    return None


def _money(value: object) -> Price | None:
    if isinstance(value, str):
        return parse_price(text(value) or None, currency=_USD)
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            return None
        if amount > 0 and amount.is_finite():
            return Price(amount=Money.from_major(amount, _USD))
    return None


def _address(item: dict[str, Any]) -> Address:
    def part(key: str) -> str | None:
        return text(_dig(item, "address", key)) or None

    return Address(
        line1=part("streetAddress"),
        city=part("city"),
        region=part("state"),
        postal_code=part("zipcode"),
    )


def _title(item: dict[str, Any], address: Address, beds: int | None) -> str:
    # Live building items carry the name in ``title``; the README says ``buildingName``.
    if name := text(item.get("buildingName")) or text(item.get("title")):
        return name
    if address.line1:
        return address.line1
    size = "Studio" if beds == 0 else f"{beds} bd" if beds is not None else "Rental"
    return f"{size} in {address.city}" if address.city else size


def _property_type(item: dict[str, Any], beds: int | None, *, room: object) -> PropertyType:
    flags = (item.get("isRoomForRent"), _dig(item, "rental", "isRoomForRent"), room)
    if any(flag is True for flag in flags):
        return PropertyType.ROOM
    for key in ("propertyType", "homeType"):
        label = text(item.get(key))
        # "singleFamily" → "single Family"; "SINGLE_FAMILY" is handled downstream.
        found = normalize_property_type(_CAMEL_BOUNDARY.sub(" ", label))
        if found is not PropertyType.OTHER:
            return PropertyType.STUDIO if found is PropertyType.APARTMENT and beds == 0 else found
    if item.get("_type") == GROUP:
        return PropertyType.STUDIO if beds == 0 else PropertyType.APARTMENT
    return PropertyType.OTHER


def _amenities(facts: dict[str, Any]) -> frozenset[str]:
    tags: set[str] = set()
    for key in _FEATURE_LISTS:
        for value in facts.get(key) or ():
            if label := text(value):
                tags.add(label.lower())
    for value in facts.get("laundryFeatures") or ():
        if label := text(value):
            # "In Unit" alone doesn't say laundry; the extractor needs the noun.
            label = label.lower()
            tags.add(label if "laundry" in label or "washer" in label else f"{label} laundry")
    return frozenset(tags)


def _available_from(
    item: dict[str, Any], details: dict[str, Any], facts: dict[str, Any], today: date
) -> date | None:
    for value in (
        details.get("availabilityDate"),
        facts.get("availabilityDate"),
        item.get("availabilityDate"),
    ):
        if isinstance(value, int | float) and not isinstance(value, bool):
            # resoFacts gives epoch milliseconds at midnight UTC.
            return _epoch_ms_date(value)
        when = text(value)
        if when.lower() in {"now", "available now"}:
            return today
        if found := _date(when):
            return found
    return None


def _epoch_ms_date(value: float) -> date | None:
    try:
        return datetime.fromtimestamp(value / 1000, UTC).date()
    except (OverflowError, OSError, ValueError):
        return None


def _date(when: str) -> date | None:
    try:
        # ISO dates, with or without a time after them.
        return date.fromisoformat(when[:10])
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(when, fmt).date()
        except ValueError:
            continue
    return None


def _lease_months(facts: dict[str, Any]) -> int | None:
    found = _MONTHS.search(text(facts.get("leaseTerm")))
    return max(1, int(found.group(1))) if found else None


def _photos(item: dict[str, Any]) -> tuple[str, ...]:
    photos = _dig(item, "media", "allPropertyPhotos", "highResolution")
    if not isinstance(photos, list) or not photos:
        single = _dig(item, "media", "propertyPhotoLinks", "highResolutionLink")
        photos = [single]
    return tuple(p for p in photos if isinstance(p, str) and p.startswith("http"))


def _raw(item: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    raw = {k: v for k, v in item.items() if k not in _RAW_EXCLUDED and k != "_details"}
    if kept := {k: details[k] for k in _RAW_DETAILS if k in details}:
        raw["_details"] = kept
    return raw
