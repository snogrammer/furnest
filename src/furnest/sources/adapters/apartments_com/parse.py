"""Mapping between furnest and the Apartments.com data provider's JSON.

The adapter gets Apartments.com rentals through the Apify actor
``epctex/apartments-scraper-api`` (see ``docs/adapters.md`` for why, and why not the
older ``epctex/apartments-scraper``, which no longer works). This module is pure:
:func:`build_input` turns criteria into the actor's input, and :func:`parse_item`
turns one dataset item into a listing.

The actor takes Apartments.com URLs (or one free-text ``search``, a single string
that can't carry several locations), and has no input for filters. Filters go in the
URL instead, the way the site does it, so :func:`build_input` turns each search
location into ``https://www.apartments.com/<city>-<st>/`` and adds the criteria's
minimum beds and price range as the site's path segment
(``min-2-bedrooms-1000-to-2000``). The site has no move-in date filter, so that is
left to matching. **The actor has no furnished filter**, and the site's amenity
filter can't be combined with the others reliably, so :func:`parse_item` filters
after the fact: an item is kept only when its amenities, lease and fee lists or
description say "furnished" (as in "Furnished Units Available"). Anything else raises
:class:`NotFurnishedError`.

Item fields map like this:

========================================  ===========================================
actor field                               NormalizedListing
========================================  ===========================================
``id``                                    ``source_id``
``url``                                   ``url``
``propertyName``                          ``title`` (else the street address)
``description``                           ``description``
``rent.min``                              ``price`` per month, USD (the lowest rent
                                          of the building's floor plans)
``location``                              ``address`` (US)
``coordinates``                           ``lat``, ``lng``
``beds``, ``baths``, ``sqft``             ``beds``, ``baths``, ``sqft`` when the text
                                          is one figure ("2 bd"); a range ("1 bd - 2
                                          bd") is unknown, because the actor doesn't
                                          say which floor plan the rent is for
``amenities`` groups, pet policy          ``amenities``, as the words the site uses
``fees`` "Lease Options"                  ``min_lease_months``
``photos``                                ``images``
``contact.phone`` / ``.name``             ``contact``: the leasing office's public
                                          phone, else platform only
========================================  ===========================================

``property_type`` is an apartment (a studio when the item has only studios).
Reviews, schools, transit, points of interest, photos and the contact block are left
out of ``raw``: they are bulky, already mapped, or name people.
"""

from __future__ import annotations

import re
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
from furnest.sources.adapters._values import number, text

SOURCE = "apartments_com"
ACTOR = "epctex/apartments-scraper-api"
SITE = "https://www.apartments.com"
_USD = "USD"

# Bulky blocks that aren't about the rental, and the leasing office's contact block
# (the phone is kept on ``contact``).
_RAW_EXCLUDED = frozenset(
    {
        "contact",
        "reviews",
        "schools",
        "transportation",
        "poi",
        "transitAndPOI",
        "neighborhoodDescription",
        "virtualTours",
        "virtualTourExtended",
        "floorPlans",
        "photos",
        "score",
        "scores",
    }
)
# "Furnished", "Furnished Units Available", "Can Be Furnished"; not "Unfurnished".
_FURNISHED = re.compile(r"\bfurnished\b", re.IGNORECASE)
_PRIVATE_OWNER = re.compile(r"\bprivate owner\b|\bowner[- ]managed\b", re.IGNORECASE)
_MANAGED = re.compile(
    r"\bprofessionally managed\b|\bproperty management\b|\bmanagement (?:company|group|team)\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_MONTHS = re.compile(r"(\d+)\s*(?:-\s*\d+\s*)?(?:mo|month)", re.IGNORECASE)
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_PET_HEADERS = frozenset({"dogs allowed", "cats allowed", "pets allowed"})


class NotFurnishedError(ValueError):
    """The item says nothing about being furnished, and the actor can't filter on it."""


# --- Criteria → actor input -------------------------------------------------------


def build_input(
    criteria: Criteria, *, locations: tuple[str, ...], max_results: int
) -> dict[str, Any]:
    filters = _filters(criteria)
    urls = [f"{SITE}/{slug}/{filters}" for slug in map(_slug, locations) if slug]
    return {"startUrls": urls, "maxItems": max_results}


def _slug(location: str) -> str:
    """``Springfield, IL`` → ``springfield-il``; a ZIP code is its own slug."""
    return _NON_SLUG.sub("-", location.lower()).strip("-")


def _filters(criteria: Criteria) -> str:
    """The site's filter path segment, with a trailing slash, or ``""``."""
    parts: list[str] = []
    if criteria.min_beds:
        parts.append(f"min-{criteria.min_beds}-bedrooms")
    low, high = (_whole_dollars(m) for m in (criteria.price_min, criteria.price_max))
    if low and high:
        parts.append(f"{low}-to-{high}")
    elif high:
        parts.append(f"under-{high}")
    elif low:
        parts.append(f"over-{low}")
    return f"{'-'.join(parts)}/" if parts else ""


def _whole_dollars(money: Money | None) -> int | None:
    # Rounded down, so the search can only widen.
    return int(money.major) if money is not None and money.major >= 1 else None


# --- Actor item → NormalizedListing -------------------------------------------------


def parse_item(item: dict[str, Any], *, today: date) -> NormalizedListing:
    """Map one dataset item.

    Raises ``ValueError`` (pydantic's included) if the item is unusable, and
    :class:`NotFurnishedError` if it doesn't say it is furnished.
    """
    source_id = text(item.get("id"))
    if not source_id:
        msg = "item has no id"
        raise ValueError(msg)
    url = text(item.get("url"))
    if not url.startswith(f"{SITE}/"):
        msg = f"item {source_id} has no apartments.com URL"
        raise ValueError(msg)
    amenities = _amenity_values(item)
    description = " ".join(text(item.get("description")).split())
    if not _is_furnished(item, amenities, description):
        msg = f"item {source_id} isn't marked furnished"
        raise NotFurnishedError(msg)
    address = _address(item)
    beds = _single(item.get("beds"), r"bd|bed|studio")
    contact = _object(item.get("contact"))
    phone = text(contact.get("phone")) or None
    title = text(item.get("propertyName")) or address.line1 or "Rental"
    return NormalizedListing(
        source=SOURCE,
        source_id=source_id,
        url=url,  # type: ignore[arg-type]
        title=title,
        description=description,
        price=_price(_object(item.get("rent")).get("min")),
        address=address,
        lat=_coordinate(item, "latitude"),
        lng=_coordinate(item, "longitude"),
        beds=None if beds is None else int(beds),
        baths=_half(_single(item.get("baths"), r"ba")),
        sqft=_sqft(_single(item.get("sqft"), r"sq")),
        property_type=_property_type(item.get("beds")),
        owner_type=infer_owner_type(
            title, description, hint=_owner_type(description, text(contact.get("name")))
        ),
        furnished=True,
        amenities=_amenities(item, amenities),
        available_from=_available_from(item, today),
        min_lease_months=_lease_months(item),
        images=_photos(item),  # type: ignore[arg-type]
        contact=Contact(name=text(contact.get("name")) or None, phone=phone)
        if phone
        else Contact(platform_only=True),
        raw={k: v for k, v in item.items() if k not in _RAW_EXCLUDED},
    )


def _owner_type(description: str, contact_name: str) -> OwnerType:
    """Who lists the place, from how the listing describes itself (#113).

    The item carries no structured field. Apartments.com is nearly all operators, but
    only a listing that says so is called one.
    """
    said = f"{description}\n{contact_name}"
    if _PRIVATE_OWNER.search(said):
        return OwnerType.INDIVIDUAL
    if _MANAGED.search(said):
        return OwnerType.PROPERTY_MANAGER
    return OwnerType.UNKNOWN


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _amenity_values(item: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for group in item.get("amenities") or ():
        for value in _object(group).get("value") or ():
            if label := text(value):
                values.append(label)
    return values


def _fee_policies(item: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """``(header, [key, ...])`` for every policy under ``fees``."""
    found: list[tuple[str, list[str]]] = []
    for section in item.get("fees") or ():
        for policy in _object(section).get("policies") or ():
            policy = _object(policy)
            keys = [text(_object(v).get("key")) for v in policy.get("values") or ()]
            found.append((text(policy.get("header")), [k for k in keys if k]))
    return found


def _is_furnished(item: dict[str, Any], amenities: list[str], description: str) -> bool:
    keys = [key for _, keys in _fee_policies(item) for key in keys]
    return any(_FURNISHED.search(s) for s in (*amenities, *keys, description))


def _amenities(item: dict[str, Any], values: list[str]) -> frozenset[str]:
    tags = {value.lower() for value in values}
    if any(header.lower() in _PET_HEADERS for header, _ in _fee_policies(item)):
        tags.add("pets allowed")
    return frozenset(tags)


def _address(item: dict[str, Any]) -> Address:
    place = _object(item.get("location"))

    def part(*keys: str) -> str | None:
        return next((v for k in keys if (v := text(place.get(k)))), None)

    return Address(
        # The README spells the street key "streedAddress"; accept both.
        line1=part("streetAddress", "streedAddress"),
        city=part("city"),
        region=part("state"),
        postal_code=part("postalCode"),
        neighborhood=part("neighborhood"),
    )


def _coordinate(item: dict[str, Any], key: str) -> float | None:
    coordinates = _object(item.get("coordinates"))
    lat, lng = (number(coordinates.get(k)) for k in ("latitude", "longitude"))
    if lat is None or lng is None:
        return None
    return lat if key == "latitude" else lng


def _price(value: object) -> Price | None:
    found = number(value)
    if found is None or found <= 0:
        return None
    try:
        return Price(amount=Money.from_major(Decimal(str(found)), _USD))
    except InvalidOperation:
        return None


def _single(value: object, unit: str) -> float | None:
    """The one figure in "2 bd", "1.5 ba" or "850 sq ft"; ``None`` for a range or none.

    "Studio" counts as 0 for the ``bd`` unit.
    """
    label = text(value).lower()
    if not re.search(unit, label):
        return None
    figures = {float(n.replace(",", "")) for n in _NUMBER.findall(label)}
    if "studio" in label:
        figures.add(0.0)
    return figures.pop() if len(figures) == 1 else None


def _half(value: float | None) -> float | None:
    return round(value * 2) / 2 if value is not None else None


def _sqft(value: float | None) -> int | None:
    return int(value) if value else None


def _property_type(beds: object) -> PropertyType:
    label = text(beds).lower()
    return (
        PropertyType.STUDIO
        if label and label.replace("studio", "").strip() == ""
        else (PropertyType.APARTMENT)
    )


def _available_from(item: dict[str, Any], today: date) -> date | None:
    for key in ("availableFrom", "availability", "availableDate"):
        when = text(item.get(key))
        if when.lower() in {"now", "available now", "immediately"}:
            return today
        try:
            return date.fromisoformat(when[:10])
        except ValueError:
            continue
    return None


def _photos(item: dict[str, Any]) -> tuple[str, ...]:
    photos = item.get("photos")
    if not isinstance(photos, list):
        return ()
    return tuple(p for p in photos if isinstance(p, str) and p.startswith("http"))


def _lease_months(item: dict[str, Any]) -> int | None:
    for header, keys in _fee_policies(item):
        if "lease" not in header.lower():
            continue
        for key in keys:
            if found := _MONTHS.search(key):
                return max(1, int(found.group(1)))
    return None
