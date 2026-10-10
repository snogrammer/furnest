"""Mapping between furnest and the Apartments.com data provider's JSON.

The adapter gets Apartments.com rentals through the Apify actor
``epctex/apartments-scraper-api`` (see ``docs/adapters.md`` for why, and why not the
older ``epctex/apartments-scraper``, which no longer works). This module is pure:
:func:`build_input` turns criteria into the actor's input, and :func:`parse_item`
turns one dataset item into listings.

The actor takes Apartments.com URLs (or one free-text ``search``, a single string
that can't carry several locations), and has no input for filters. Filters go in the
URL instead, the way the site does it, so :func:`build_input` turns each search
location into ``https://www.apartments.com/<city>-<st>/`` and adds the criteria's
minimum beds and price range as the site's path segment
(``min-2-bedrooms-1000-to-2000``). Live runs (#194) showed the actor accepts that
segment. The site has no move-in date filter, so that is left to matching.

**The actor has no furnished filter.** The site's own furnished page
(``/<city>-<st>/furnished/``) is taken by the actor for a property page and returns
nothing, and a ``-furnished`` suffix on the filter segment changes nothing. So
:func:`parse_item` filters after the fact: a building is kept only when its
amenities, lease and fee lists or description say "furnished" (as in "Furnished
Units Available"). Anything else raises :class:`NotFurnishedError`. The units carry
no furnished flag of their own, so every unit of a kept building is kept.

One item is one building. Its available units are in ``rentals``, and each becomes a
listing, with the building filling in the rest; a building with no ``rentals`` (only
floor plans in ``models``) is one listing at its rent range. Fields map like this:

========================================  ===========================================
actor field (unit, else building)         NormalizedListing
========================================  ===========================================
``id`` + unit ``key``                     ``source_id`` (``<id>-<key>``; the building
                                          alone is ``<id>``)
``url``                                   ``url`` (units link to their building)
``propertyName`` + ``unitNumber``         ``title`` (else the street address)
``description``                           ``description``
unit ``basePrice``; ``baseRent``          ``price`` per month, USD; a building's is the
                                          range of its floor plans
``location``                              ``address`` (US)
``coordinates``                           ``lat``, ``lng``
unit ``beds``, ``baths``, ``squareFeet``  ``beds``, ``baths``, ``sqft``; a building's
                                          ``beds`` text counts only when it is one
                                          figure ("2 bd"), not a range
unit ``availableDate``                    ``available_from`` (a past date is today)
``amenities`` groups, pet policy          ``amenities``, as the words the site uses
``fees`` "Lease Options"                  ``min_lease_months``
unit ``image``; ``models[].imageLarge``   ``images`` (floor plan pictures: the actor
                                          sends no photos unless paid extras are on)
``contact.phone`` / ``.name``             ``contact``: the leasing office's public
                                          phone, else platform only
========================================  ===========================================

``property_type`` is a studio for a unit with no bedroom (or a building with only
studios), else an apartment. Reviews, schools, transit, the contact block, floor
plans and units are left out of ``raw`` (a unit's listing has its own unit under
``unit``): they are bulky, already mapped, or name people.
"""

from __future__ import annotations

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
    OwnerType,
    Price,
    PropertyType,
    infer_owner_type,
)
from furnest.sources.adapters._values import half, number, positive, text, whole

SOURCE = "apartments_com"
ACTOR = "epctex/apartments-scraper-api"
SITE = "https://www.apartments.com"
_USD = "USD"

# Bulky blocks that aren't about the rental, the units (mapped one by one), and the
# leasing office's contact block (the phone is kept on ``contact``).
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
        "models",
        "rentals",
        "photos",
        "score",
        "scores",
    }
)
# A unit's links and picture are in the listing already, or lead off the site.
_UNIT_RAW_EXCLUDED = frozenset({"image", "applyNowUrl"})
# "Furnished", "Furnished Units Available", "Can Be Furnished"; not "Unfurnished".
_FURNISHED = re.compile(r"\bfurnished\b", re.IGNORECASE)
_PRIVATE_OWNER = re.compile(r"\bprivate owner\b|\bowner[- ]managed\b", re.IGNORECASE)
_MANAGED = re.compile(
    r"\bprofessionally managed\b|\bproperty management\b|\bmanagement (?:company|group|team)\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
# "3 - 16 Month Leases", "12 - 24 mo"; not "Call Office for Lease Terms".
_MONTHS = re.compile(r"(\d+)\s*(?:-\s*\d+\s*)?(?:mo|month)", re.IGNORECASE)
_NON_SLUG = re.compile(r"[^a-z0-9]+")
# "Dogs Allowed", "Cats Allowed", "Other Pets Allowed"; birds and fish don't count.
_PETS = re.compile(r"\b(?:dogs|cats|pets) allowed\b", re.IGNORECASE)


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


def parse_item(item: dict[str, Any], *, today: date) -> list[NormalizedListing]:
    """Map one dataset item (a building) to a listing per available unit, or to one
    listing for the building when it lists no units.

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
    building = _Building(item, today=today)
    if not building.furnished:
        msg = f"item {source_id} isn't marked furnished"
        raise NotFurnishedError(msg)
    units = [u for u in item.get("rentals") or () if isinstance(u, dict) and text(u.get("key"))]
    if not units:
        return [building.listing()]
    return [building.unit_listing(unit) for unit in units]


class _Building:
    """What every listing of one building shares."""

    def __init__(self, item: dict[str, Any], *, today: date) -> None:
        self.item = item
        self.today = today
        self.id = text(item.get("id"))
        self.url = text(item.get("url"))
        self.amenity_values = _amenity_values(item)
        self.description = " ".join(text(item.get("description")).split())
        self.address = _address(item)
        self.name = text(item.get("propertyName")) or self.address.line1 or "Rental"
        self.contact = _object(item.get("contact"))
        self.raw = {k: v for k, v in item.items() if k not in _RAW_EXCLUDED}

    @property
    def furnished(self) -> bool:
        keys = [key for _, keys in _fee_policies(self.item) for key in keys]
        said = (*self.amenity_values, *keys, self.description)
        return any(_FURNISHED.search(s) for s in said)

    def listing(self) -> NormalizedListing:
        beds = _single(self.item.get("beds"), r"bd|bed|studio")
        return self._listing(
            source_id=self.id,
            title=self.name,
            price=_price(_object(self.item.get("baseRent"))),
            beds=None if beds is None else int(beds),
            baths=_half(_single(self.item.get("baths"), r"ba")),
            sqft=_sqft(_single(self.item.get("sqft"), r"sq")),
            property_type=_building_type(self.item.get("beds")),
            available_from=None,
            images=_model_images(self.item),
            raw=self.raw,
        )

    def unit_listing(self, unit: dict[str, Any]) -> NormalizedListing:
        beds = whole(unit.get("beds"))
        unit_number = text(unit.get("unitNumber"))
        image = text(unit.get("image"))
        return self._listing(
            source_id=f"{self.id}-{text(unit.get('key'))}",
            title=f"{self.name}, unit {unit_number}" if unit_number else self.name,
            price=_price({"min": unit.get("basePrice")}),
            beds=beds,
            baths=half(unit.get("baths")),
            sqft=positive(unit.get("squareFeet")),
            property_type=PropertyType.STUDIO if beds == 0 else PropertyType.APARTMENT,
            available_from=_available_from(unit, self.today),
            images=(image,) if image.startswith("http") else _model_images(self.item),
            raw={
                **self.raw,
                "unit": {k: v for k, v in unit.items() if k not in _UNIT_RAW_EXCLUDED},
            },
        )

    def _listing(
        self,
        *,
        source_id: str,
        title: str,
        price: Price | None,
        beds: int | None,
        baths: float | None,
        sqft: int | None,
        property_type: PropertyType,
        available_from: date | None,
        images: tuple[str, ...],
        raw: dict[str, Any],
    ) -> NormalizedListing:
        phone = text(self.contact.get("phone")) or None
        contact_name = text(self.contact.get("name"))
        return NormalizedListing(
            source=SOURCE,
            source_id=source_id,
            url=self.url,  # type: ignore[arg-type]
            title=title,
            description=self.description,
            price=price,
            address=self.address,
            lat=_coordinate(self.item, "latitude"),
            lng=_coordinate(self.item, "longitude"),
            beds=beds,
            baths=baths,
            sqft=sqft,
            property_type=property_type,
            owner_type=infer_owner_type(
                title, self.description, hint=_owner_type(self.description, contact_name)
            ),
            furnished=True,
            amenities=_amenities(self.item, self.amenity_values),
            available_from=available_from,
            min_lease_months=_lease_months(self.item),
            images=images,  # type: ignore[arg-type]
            contact=Contact(name=contact_name or None, phone=phone)
            if phone
            else Contact(platform_only=True),
            raw=raw,
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


def _amenities(item: dict[str, Any], values: list[str]) -> frozenset[str]:
    tags = {value.lower() for value in values}
    if any(_PETS.search(header) for header, _ in _fee_policies(item)):
        tags.add("pets allowed")
    return frozenset(tags)


def _address(item: dict[str, Any]) -> Address:
    place = _object(item.get("location"))

    def part(key: str) -> str | None:
        return text(place.get(key)) or None

    return Address(
        line1=part("streetAddress"),
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


def _price(rent: dict[str, Any]) -> Price | None:
    """``{"min": …, "max": …}`` as a monthly USD price, a range when ``max`` is higher."""
    low, high = (_money(rent.get(k)) for k in ("min", "max"))
    if low is None:
        return None
    if high is None or high.amount_minor <= low.amount_minor:
        return Price(amount=low)
    return Price(amount=low, amount_max=high)


def _money(value: object) -> Money | None:
    found = number(value)
    if found is None or found <= 0:
        return None
    try:
        return Money.from_major(Decimal(str(found)), _USD)
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


def _building_type(beds: object) -> PropertyType:
    label = text(beds).lower()
    if label and label.replace("studio", "").strip() == "":
        return PropertyType.STUDIO
    return PropertyType.APARTMENT


def _available_from(unit: dict[str, Any], today: date) -> date | None:
    """The unit's ``availableDate``, else its ``availability`` ("Now", "11/01/26").

    Units available "Now" carry the date they became free, which can be long past.
    """
    when = text(unit.get("availableDate"))
    try:
        found = date.fromisoformat(when[:10])
    except ValueError:
        label = text(unit.get("availability"))
        if label.lower() == "now":
            return today
        try:
            found = datetime.strptime(label, "%m/%d/%y").date()
        except ValueError:
            return None
    return max(found, today)


def _model_images(item: dict[str, Any]) -> tuple[str, ...]:
    found = (text(_object(m).get("imageLarge")) for m in item.get("models") or ())
    return tuple(dict.fromkeys(url for url in found if url.startswith("http")))


def _lease_months(item: dict[str, Any]) -> int | None:
    for header, keys in _fee_policies(item):
        if "lease" not in header.lower():
            continue
        for key in keys:
            if found := _MONTHS.search(key):
                return max(1, int(found.group(1)))
    return None
