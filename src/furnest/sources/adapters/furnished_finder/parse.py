"""Mapping between furnest and the Furnished Finder data provider's JSON.

The adapter gets Furnished Finder listings through the Apify actor
``abotapi/furnishedfinder-scraper`` (see ``docs/adapters.md`` for why). This module
is pure: :func:`build_input` turns criteria into the actor's input, and
:func:`parse_item` turns one dataset item into a :class:`NormalizedListing`.

Criteria the actor can filter on are sent along to keep runs (which are billed per
listing) small: monthly price range in USD, minimum beds and baths, and entire unit
vs room. Everything else is left to matching. Locations come from the adapter's
``search_locations``, because the actor searches by city name, not by area.

Item fields, as the actor documents them, map like this:

========================================  ===========================================
actor field                               NormalizedListing
========================================  ===========================================
``id`` (else ``propertyId_unitId``)       ``source_id``
``url`` (else ``/property/<id>``)         ``url``
``name``                                  ``title``
``description``, ``spaceDescription``,    ``description``, in that order, blank lines
``neighborhoodDescription``               between
``priceValue`` + ``priceCurrency``        ``price`` per month (else ``priceFormatted``
                                          parsed)
``city``, ``state``                       ``address.city``, ``address.region`` (US)
``latitude``, ``longitude``               ``lat``, ``lng``
``bedroomCount``, ``bathroomCount``       ``beds``, ``baths`` (to the nearest half)
``squareFootage``                         ``sqft``
``propertyTypeClass`` ``room``, else      ``property_type``
``propertyType``
``amenities`` (camelCase keys),           ``amenities``, as words ("airConditioning"
``laundryType``, ``utilitiesIncluded``    → "air conditioning") the keyword extractor
                                          reads
``isAvailableNow``, ``availableOnDate``   ``available_from`` (today when available now)
``minimumStayDays``                       ``min_lease_months``, rounded up from 30-day
                                          months
``photos``                                ``images``
========================================  ===========================================

Every listing is ``furnished``, and ``contact.platform_only`` is set: Furnished Finder
hides landlord contact details behind its own messaging. Reviews are dropped from
``raw`` because they carry reviewers' names.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
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

SOURCE = "furnished_finder"
ACTOR = "abotapi/furnishedfinder-scraper"
SITE = "https://www.furnishedfinder.com"
_USD = "USD"

# Item keys left out of ``raw``: reviews name their authors.
_RAW_EXCLUDED = frozenset({"reviews"})
# Amenity keys whose words would mislead the keyword extractor, or that don't split.
_AMENITY_OVERRIDES: dict[str, str | None] = {
    "petsLiveOnThisProperty": None,  # the landlord's pets, not "pets allowed"
    "petsOnProperty": None,
    "washeranddryer": "washer and dryer",
}
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y")
_AVAILABLE_PREFIX = re.compile(r"^\s*available(?:\s+(?:on|from))?\s*:?\s*", re.IGNORECASE)
DAYS_PER_MONTH = 30


# --- Criteria → actor input -------------------------------------------------------


def build_input(
    criteria: Criteria, *, locations: tuple[str, ...], max_results: int
) -> dict[str, Any]:
    run_input: dict[str, Any] = {
        "mode": "search",
        "locations": list(locations),
        "propertyTypeClass": _type_class(criteria),
        "fetchDetails": True,
        "fetchReviews": False,
        "maxPages": 0,
        "maxListings": max_results,
    }
    if criteria.price_min is not None and criteria.price_min.currency == _USD:
        run_input["minPrice"] = math.floor(criteria.price_min.major)
    if criteria.price_max is not None and criteria.price_max.currency == _USD:
        run_input["maxPrice"] = math.ceil(criteria.price_max.major)
    if criteria.min_beds is not None:
        run_input["minBedrooms"] = criteria.min_beds
    if criteria.min_baths is not None:
        # The actor takes whole baths; rounding down only widens the search.
        run_input["minBathrooms"] = math.floor(criteria.min_baths)
    return run_input


def _type_class(criteria: Criteria) -> str:
    allow = set(criteria.property_types_allow)
    if allow == {PropertyType.ROOM}:
        return "room"
    if PropertyType.ROOM in criteria.property_types_deny or (
        allow and PropertyType.ROOM not in allow
    ):
        return "entire_unit"
    return "any"


# --- Actor item → NormalizedListing -------------------------------------------------


def is_error_row(item: dict[str, Any]) -> bool:
    """The actor's "nothing was scanned" row: an empty search, or a blocked first page."""
    return item.get("error") is True


def parse_item(item: dict[str, Any], *, today: date) -> NormalizedListing:
    """Map one dataset item. Raises ``ValueError`` (pydantic's included) if unusable."""
    source_id = _source_id(item)
    url = item.get("url")
    if not (isinstance(url, str) and url.startswith(f"{SITE}/")):
        url = f"{SITE}/property/{source_id}"
    lat, lng = _number(item.get("latitude")), _number(item.get("longitude"))
    if lat is None or lng is None:
        lat = lng = None
    return NormalizedListing(
        source=SOURCE,
        source_id=source_id,
        url=url,  # type: ignore[arg-type]
        title=_text(item.get("name")),
        description=_description(item),
        price=_price(item),
        address=Address(
            city=_text(item.get("city")) or None, region=_text(item.get("state")) or None
        ),
        lat=lat,
        lng=lng,
        beds=_whole(item.get("bedroomCount")),
        baths=_half(item.get("bathroomCount")),
        sqft=_positive(item.get("squareFootage")),
        property_type=_property_type(item),
        furnished=True,
        amenities=_amenities(item),
        available_from=_available_from(item, today),
        min_lease_months=_lease_months(item),
        images=_photos(item),  # type: ignore[arg-type]
        contact=Contact(platform_only=True),
        raw={k: v for k, v in item.items() if k not in _RAW_EXCLUDED},
    )


def _source_id(item: dict[str, Any]) -> str:
    if isinstance(item.get("id"), str | int) and str(item["id"]).strip():
        return str(item["id"]).strip()
    property_id, unit_id = item.get("propertyId"), item.get("unitId")
    if property_id is not None and unit_id is not None:
        return f"{property_id}_{unit_id}"
    msg = "item has no id"
    raise ValueError(msg)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _description(item: dict[str, Any]) -> str:
    parts = (
        _text(item.get(key))
        for key in ("description", "spaceDescription", "neighborhoodDescription")
    )
    return "\n\n".join(part for part in parts if part)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _whole(value: object) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 else None


def _half(value: object) -> float | None:
    number = _number(value)
    return round(number * 2) / 2 if number is not None and number >= 0 else None


def _positive(value: object) -> int | None:
    number = _whole(value)
    return number if number else None


def _price(item: dict[str, Any]) -> Price | None:
    currency = _text(item.get("priceCurrency")).upper() or _USD
    value = item.get("priceValue")
    if isinstance(value, int | float | str) and not isinstance(value, bool):
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            amount = Decimal(0)
        if amount > 0:
            return Price(amount=Money.from_major(amount, currency))
    return parse_price(_text(item.get("priceFormatted")) or None, currency=currency)


def _photos(item: dict[str, Any]) -> tuple[str, ...]:
    photos = item.get("photos") or ()
    return tuple(p for p in photos if isinstance(p, str) and p.startswith("http"))


def _property_type(item: dict[str, Any]) -> PropertyType:
    if item.get("propertyTypeClass") == "room":
        return PropertyType.ROOM
    label = _text(item.get("propertyType"))
    found = normalize_property_type(label)
    if found is PropertyType.OTHER and " - " in label:
        # "Room - Apartment", "Entire Unit - House": try each part.
        for part in label.split(" - "):
            if (found := normalize_property_type(part)) is not PropertyType.OTHER:
                break
    return found


def _amenities(item: dict[str, Any]) -> frozenset[str]:
    tags: set[str] = set()
    for key in item.get("amenities") or ():
        if not isinstance(key, str) or not key.strip():
            continue
        label = _AMENITY_OVERRIDES.get(key, _CAMEL_BOUNDARY.sub(" ", key.strip()))
        if label:
            tags.add(label.lower())
    if laundry := _text(item.get("laundryType")):
        tags.add(laundry.lower())
    if item.get("utilitiesIncluded") is True:
        tags.add("utilities included")
    return frozenset(tags)


def _available_from(item: dict[str, Any], today: date) -> date | None:
    if item.get("isAvailableNow") is True:
        return today
    text = _AVAILABLE_PREFIX.sub("", _text(item.get("availableOnDate")))
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _lease_months(item: dict[str, Any]) -> int | None:
    days = _whole(item.get("minimumStayDays", item.get("minimumStayInDays")))
    if not days:
        return None
    return max(1, math.ceil(days / DAYS_PER_MONTH))
