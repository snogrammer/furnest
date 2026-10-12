"""Mapping between furnest and the Furnished Finder data provider's JSON.

The adapter gets Furnished Finder listings through the Apify actor
``memo23/furnishedfinder-scraper-cheerio`` (see ``docs/adapters.md`` for why, and how it
compared with the other actors in #216). This module is pure: :func:`build_input`
turns criteria into the actor's input, and :func:`parse_item` turns one dataset item
into a :class:`NormalizedListing`.

The actor takes Furnished Finder URLs and nothing else. Each search location becomes
the city's search page, ``/housing/us--<st>--<city>`` (:func:`search_url`). Live runs
(#221) showed the page honours ``min-price`` and ``max-price`` but ignores
``property-type-class``, so the monthly price range in USD is sent and the property
type, beds and the move-in date are left to matching. ``moreDetails`` is on, because
the description, house rules and fees come from the property page.

Item fields, as live runs returned them, map like this:

========================================  ===========================================
actor field                               NormalizedListing
========================================  ===========================================
``listingId`` (``<id>_<unit>``, else      ``source_id``
``id``)
``url`` (else ``/property/<id>``)         ``url``
``name`` (else ``title``)                 ``title``
``description``,                          ``description``, a blank line between
``neighborhoodOverview``
``monthlyRent`` (``"1800.00"``) +         ``price`` per month (else ``rentAmount``'s
``rentAmount.currency``                   ``amount`` text parsed)
``city``, ``state``                       ``address.city``, ``address.region`` (US)
``latitude``, ``longitude``               ``lat``, ``lng``
``bedroomCount``, ``bathroomCount``       ``beds``, ``baths`` (to the nearest half)
``squareFootage``                         ``sqft``
``propertyTypeClass`` ``room``, else      ``property_type``
``propertyType``
``amenities`` (camelCase keys),           ``amenities``, as words ("airConditioning"
``houseRulesGraphql``, ``laundryType``,   → "air conditioning", ``petsNotAllowed`` →
``utilitiesIncluded``                     "no pets") the keyword extractor reads
``isAvailableNow``, ``availableOn``       ``available_from`` (today when available now)
(``MM/DD/YYYY``), ``availableOnDate``
``minimumStayDays``                       ``min_lease_months``, rounded up from 30-day
                                          months
``photos[].url``                          ``images``
``landlordCompany``                       ``owner_type`` hint: a name that reads as a
                                          property manager, else the text decides
========================================  ===========================================

Every listing is ``furnished``, and ``contact.platform_only`` is set: Furnished Finder
hides landlord contact details behind its own messaging. Items carry the landlord's
name, username, photo, biography and verifications, so ``raw`` keeps a whitelist of
listing facts (fees included, which have no field of their own) and nothing about the
person.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
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
    infer_owner_type,
    normalize_property_type,
    parse_price,
)
from furnest.sources.adapters._values import half, number, positive, text, whole

SOURCE = "furnished_finder"
ACTOR = "memo23/furnishedfinder-scraper-cheerio"
SITE = "https://www.furnishedfinder.com"
_USD = "USD"
DAYS_PER_MONTH = 30

# Listing facts kept in ``raw``. Everything else on an item is either mapped above or
# about the landlord (names, username, photo, biography, LinkedIn, verifications).
_RAW_KEPT = frozenset(
    {
        "id",
        "listingId",
        "url",
        "name",
        "title",
        "description",
        "neighborhoodOverview",
        "propertyType",
        "propertyTypeClass",
        "bedroomCount",
        "bathroomCount",
        "squareFootage",
        "totalSleeps",
        "monthlyRent",
        "rentAmount",
        "deposit",
        "petDeposit",
        "cleaningFee",
        "applicationFee",
        "utilitiesIncluded",
        "laundryType",
        "amenities",
        "houseRulesGraphql",
        "minimumStayDays",
        "isAvailableNow",
        "availableOn",
        "availableOnDate",
        "city",
        "state",
        "latitude",
        "longitude",
        "encodedLocationName",
        "averageRating",
        "totalReviewCount",
    }
)
# Amenity keys whose words would mislead the keyword extractor, or that don't split.
_AMENITY_OVERRIDES: dict[str, str | None] = {
    "petsLiveOnThisProperty": None,  # the landlord's pets, not "pets allowed"
    "petsOnProperty": None,
    "washeranddryer": "washer and dryer",
    "washeranddryeronpremises": "washer and dryer on-site",
}
# House rules, as words the extractor reads both ways.
_HOUSE_RULES: dict[str, str] = {
    "petsAllowed": "pets allowed",
    "petsNotAllowed": "no pets",
    "smokingAllowed": "smoking allowed",
    "smokingNotAllowed": "no smoking",
}
# ``laundryType``, lower-cased: codes, and a phrase the extractor would read as in-unit.
# Other values ("In-Unit W/D") are already words.
_LAUNDRY: dict[str, str | None] = {
    "in_unit": "in-unit laundry",
    "shared": "shared laundry",
    "in complex w/d": "shared laundry",
    "none": None,
}
# A ``landlordCompany`` that reads as a management business. The field is free text,
# and landlords also fill in an employer ("… High School"), so a plain name is no hint.
_MANAGER_NAME = re.compile(
    r"\b(?:management|properties|property|apartments|residential|realty|rentals|housing|"
    r"stays|suites|homes|communities|holdings|llc|inc)\b",
    re.IGNORECASE,
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_DATE_FORMATS = (
    "%m/%d/%Y",
    "%Y-%m-%d",
    "%b. %d, %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%B %d %Y",
)
_AVAILABLE_PREFIX = re.compile(r"^\s*available(?:\s+(?:on|from))?\s*:?\s*", re.IGNORECASE)
_SLUG = re.compile(r"^us--[a-z]{2}--[a-z0-9]+(?:-[a-z0-9]+)*$")
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_STATE = re.compile(r"^[A-Za-z]{2}$")


# --- Criteria → actor input -------------------------------------------------------


def build_input(
    criteria: Criteria, *, locations: tuple[str, ...], max_results: int
) -> dict[str, Any]:
    """The actor's input: one search URL per usable location (see :func:`search_url`)."""
    query = urlencode(_price_params(criteria))
    urls = [url for url in map(search_url, locations) if url]
    return {
        "startUrls": [{"url": f"{url}?{query}" if query else url} for url in urls],
        "maxItems": max_results,
        "moreDetails": True,
        # Reviews carry reviewers' names, and each email found is billed $0.05.
        "includeReviews": False,
        "enrichEmails": False,
    }


def search_url(location: str) -> str | None:
    """``Long Beach, CA`` → ``…/housing/us--ca--long-beach``; a slug passes as is.

    ``None`` when the location has no two-letter state, which the URL needs.
    """
    place = location.strip().lower()
    if _SLUG.match(place):
        return f"{SITE}/housing/{place}"
    city, _, state = location.rpartition(",")
    state = state.strip()
    city_slug = _NON_SLUG.sub("-", city.lower()).strip("-")
    if not city_slug or not _STATE.match(state):
        return None
    return f"{SITE}/housing/us--{state.lower()}--{city_slug}"


def _price_params(criteria: Criteria) -> dict[str, int]:
    # Whole dollars, rounded outwards so the search is never narrower than the criteria.
    params: dict[str, int] = {}
    if criteria.price_min is not None and criteria.price_min.currency == _USD:
        params["min-price"] = math.floor(criteria.price_min.major)
    if criteria.price_max is not None and criteria.price_max.currency == _USD:
        params["max-price"] = math.ceil(criteria.price_max.major)
    return params


# --- Actor item → NormalizedListing -------------------------------------------------


def parse_item(item: dict[str, Any], *, today: date) -> NormalizedListing:
    """Map one dataset item. Raises ``ValueError`` (pydantic's included) if unusable."""
    source_id = _source_id(item)
    url = item.get("url")
    if not (isinstance(url, str) and url.startswith(f"{SITE}/")):
        url = f"{SITE}/property/{source_id}"
    lat, lng = number(item.get("latitude")), number(item.get("longitude"))
    if lat is None or lng is None:
        lat = lng = None
    title = text(item.get("name")) or text(item.get("title"))
    description = _description(item)
    return NormalizedListing(
        source=SOURCE,
        source_id=source_id,
        url=url,  # type: ignore[arg-type]
        title=title,
        description=description,
        price=_price(item),
        address=Address(
            city=text(item.get("city")) or None, region=text(item.get("state")) or None
        ),
        lat=lat,
        lng=lng,
        beds=whole(item.get("bedroomCount")),
        baths=half(item.get("bathroomCount")),
        sqft=positive(item.get("squareFootage")),
        property_type=_property_type(item),
        owner_type=infer_owner_type(title, description, hint=_owner_type(item)),
        furnished=True,
        amenities=_amenities(item),
        available_from=_available_from(item, today),
        min_lease_months=_lease_months(item),
        images=_photos(item),  # type: ignore[arg-type]
        contact=Contact(platform_only=True),
        raw={k: v for k, v in item.items() if k in _RAW_KEPT},
    )


def _source_id(item: dict[str, Any]) -> str:
    for key in ("listingId", "id"):
        value = item.get(key)
        if isinstance(value, str | int) and not isinstance(value, bool) and str(value).strip():
            return str(value).strip()
    msg = "item has no id"
    raise ValueError(msg)


def _description(item: dict[str, Any]) -> str:
    parts = (text(item.get(key)) for key in ("description", "neighborhoodOverview"))
    return "\n\n".join(part for part in parts if part)


def _price(item: dict[str, Any]) -> Price | None:
    rent = item.get("rentAmount")
    rent = rent if isinstance(rent, dict) else {}
    currency = text(rent.get("currency")).upper() or _USD
    found = number(item.get("monthlyRent"))
    if found is not None and found > 0:
        try:
            return Price(amount=Money.from_major(Decimal(str(item["monthlyRent"])), currency))
        except (InvalidOperation, ValueError):
            pass
    return parse_price(text(rent.get("amount")) or None, currency=currency)


def _photos(item: dict[str, Any]) -> tuple[str, ...]:
    urls = (
        photo.get("url") if isinstance(photo, dict) else photo for photo in item.get("photos") or ()
    )
    return tuple(url for url in urls if isinstance(url, str) and url.startswith("http"))


def _property_type(item: dict[str, Any]) -> PropertyType:
    if item.get("propertyTypeClass") == "room":
        return PropertyType.ROOM
    label = text(item.get("propertyType"))
    found = normalize_property_type(label)
    if found is PropertyType.OTHER and " - " in label:
        # "Room - House", "Entire Unit - Townhouse": try each part.
        for part in label.split(" - "):
            if (found := normalize_property_type(part)) is not PropertyType.OTHER:
                break
    return found


def _owner_type(item: dict[str, Any]) -> OwnerType:
    """A landlord company that reads as a management business; else no hint (#113)."""
    if _MANAGER_NAME.search(text(item.get("landlordCompany"))):
        return OwnerType.PROPERTY_MANAGER
    return OwnerType.UNKNOWN


def _amenities(item: dict[str, Any]) -> frozenset[str]:
    tags: set[str] = set()
    for key in item.get("amenities") or ():
        if not isinstance(key, str) or not key.strip():
            continue
        label = _AMENITY_OVERRIDES.get(key, _CAMEL_BOUNDARY.sub(" ", key.strip()))
        if label:
            tags.add(label.lower())
    for rule in item.get("houseRulesGraphql") or ():
        if isinstance(rule, dict) and (words := _HOUSE_RULES.get(text(rule.get("name")))):
            tags.add(words)
    laundry = text(item.get("laundryType")).lower()
    if laundry := _LAUNDRY.get(laundry, laundry) or "":
        tags.add(laundry)
    if item.get("utilitiesIncluded") is True:
        tags.add("utilities included")
    return frozenset(tags)


def _available_from(item: dict[str, Any], today: date) -> date | None:
    if item.get("isAvailableNow") is True:
        return today
    for value in (item.get("availableOn"), item.get("availableOnDate")):
        when = _AVAILABLE_PREFIX.sub("", text(value))
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(when, fmt).date()
            except ValueError:
                continue
    return None


def _lease_months(item: dict[str, Any]) -> int | None:
    days = whole(item.get("minimumStayDays", item.get("minimumStayInDays")))
    if not days:
        return None
    return max(1, math.ceil(days / DAYS_PER_MONTH))
