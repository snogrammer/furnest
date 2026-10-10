"""Mapping between furnest and the Zumper data provider's JSON.

The adapter gets Zumper rentals through the Apify actor ``memo23/zumper-cheerio`` (see
``docs/adapters.md`` for why, and how it compares with
``benthepythondev/zumper-rental-scraper``). This module is pure: :func:`build_input`
turns criteria into the actor's input, and :func:`parse_item` turns one dataset item
into a listing.

The actor takes Zumper URLs and has no input for filters, so :func:`build_input` turns
each search location into ``https://www.zumper.com/apartments-for-rent/<city>-<st>``
and leaves price, beds and the move-in date to matching. **The actor has no furnished
filter either**, and the listing's amenities are Zumper's numeric ids, so
:func:`parse_item` keeps an item only when its title or description says it is
furnished (not "unfurnished"). Anything else raises :class:`NotFurnishedError`.

The listing sits under ``entity.data`` or ``activeListings[0]`` (Zumper's two page
variants) or at the top level; :func:`_core` finds it. Fields map like this:

========================================  ===========================================
actor field                               NormalizedListing
========================================  ===========================================
``listing_id``                            ``source_id``
``url`` (else the listing path)           ``url``
``title``, ``description``                ``title``, ``description``
``min_price``                             ``price`` per month (``currency_code``)
``address``, ``city``, ``state``,         ``address`` (US)
``zipcode``
``lat``, ``lng``                          ``lat``, ``lng``
``bedrooms``, ``bathrooms`` (+ half),     ``beds``, ``baths``, ``sqft``
``square_feet``
``property_type`` (13 is an apartment)    ``property_type`` (a studio at 0 beds)
``lease_terms``                           ``min_lease_months``, when it names months
``available_on`` and similar              ``available_from`` (a date, or a unix time)
``media[].media_id``                      ``images`` on Zumper's image CDN
``agent_name``, ``brokerage_name``,       ``owner_type`` hint: a brokerage that reads
``feed_name``                             as a property manager, else a named agent
                                          or brokerage is an agent, else unknown
========================================  ===========================================

``contact.platform_only`` is set unless the item carries a public ``phone``. The
agent, brokerage and email fields name people or businesses, so they stay out of
``raw`` (``contact*``, ``agent*``, ``brokerage*``, nearby listings, schools, reviews).
"""

from __future__ import annotations

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
_APARTMENT = 13

# Bulky blocks that aren't about the rental, and fields naming people or businesses.
_RAW_EXCLUDED = frozenset(
    {
        "units",
        "nearby",
        "ratings",
        "locationSchools",
        "buildingReviews",
        "prosAndCons",
        "media",
        "emailEnrichment",
        "contactEmail",
        "contactWebsite",
        "agent_name",
        "agent_email",
        "agent_phone",
        "brokerage_name",
        "brokerage_email",
        "brokerage_phone",
        "phone",
        "email",
        "provider_url",
    }
)
_FURNISHED = re.compile(r"(?<!un)\bfurnished\b|\bfully[- ]furnished\b", re.IGNORECASE)
_MANAGER_NAME = re.compile(
    r"\b(?:management|properties|property|apartments|residential|realty group|communities|"
    r"holdings|living|rentals)\b",
    re.IGNORECASE,
)
_MONTHS = re.compile(r"(\d+)\s*(?:-\s*\d+\s*)?(?:mo|month)", re.IGNORECASE)
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_AVAILABLE_KEYS = ("available_on", "available_date", "date_available", "available_from")


class NotFurnishedError(ValueError):
    """The item doesn't say it is furnished, and the actor can't filter on it."""


# --- Criteria → actor input -------------------------------------------------------


def build_input(
    criteria: Criteria, *, locations: tuple[str, ...], max_results: int
) -> dict[str, Any]:
    # Price, beds and the move-in date are left to matching: the URL filters the
    # actor honours aren't documented well enough to send blind.
    urls = [f"{SITE}/apartments-for-rent/{slug}" for slug in map(_slug, locations) if slug]
    return {"startUrls": urls, "maxItems": max_results}


def _slug(location: str) -> str:
    """``Springfield, IL`` → ``springfield-il``."""
    return _NON_SLUG.sub("-", location.lower()).strip("-")


# --- Actor item → NormalizedListing -------------------------------------------------


def parse_item(item: dict[str, Any], *, today: date) -> NormalizedListing:
    """Map one dataset item.

    Raises ``ValueError`` (pydantic's included) if the item is unusable, and
    :class:`NotFurnishedError` if it doesn't say it is furnished.
    """
    core = _core(item)
    listing_id = core.get("listing_id")
    if isinstance(listing_id, bool) or not isinstance(listing_id, int | str):
        msg = "item has no listing_id"
        raise ValueError(msg)
    source_id = str(listing_id).strip()
    if not source_id:
        msg = "item has no listing_id"
        raise ValueError(msg)
    title = text(core.get("title"))
    description = " ".join(text(core.get("description")).split())
    if not any(_FURNISHED.search(s) for s in (title, description)):
        msg = f"item {source_id} isn't marked furnished"
        raise NotFurnishedError(msg)
    address = _address(core)
    beds = whole(core.get("bedrooms"))
    phone = text(core.get("phone")) or None
    return NormalizedListing(
        source=SOURCE,
        source_id=source_id,
        url=_url(core, item, source_id),  # type: ignore[arg-type]
        title=title or address.line1 or "Rental",
        description=description,
        price=_price(core),
        address=address,
        lat=_coordinate(core, "lat"),
        lng=_coordinate(core, "lng"),
        beds=beds,
        baths=_baths(core),
        sqft=positive(core.get("square_feet")),
        property_type=_property_type(core, beds),
        owner_type=infer_owner_type(title, description, hint=_owner_type(core)),
        furnished=True,
        available_from=_available_from(core, today),
        min_lease_months=_lease_months(core),
        images=_images(core),  # type: ignore[arg-type]
        contact=Contact(phone=phone) if phone else Contact(platform_only=True),
        raw=_raw(core),
    )


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _core(item: dict[str, Any]) -> dict[str, Any]:
    """The listing object: ``entity.data``, ``activeListings[0]`` or the item itself."""
    data = _object(_object(item.get("entity")).get("data"))
    if data:
        return data
    active = item.get("activeListings")
    if isinstance(active, list) and active and isinstance(active[0], dict):
        return active[0]
    return item


def _url(core: dict[str, Any], item: dict[str, Any], source_id: str) -> str:
    for source in (core, item):
        for key in ("url", "listing_url", "canonical_url"):
            value = text(source.get(key))
            if value.startswith(f"{SITE}/"):
                return value
            if value.startswith("/"):
                return f"{SITE}{value}"
    # Zumper redirects a bare id to the listing's own page.
    return f"{SITE}/apartments/{source_id}"


def _price(core: dict[str, Any]) -> Price | None:
    found = number(core.get("min_price"))
    if found is None or found <= 0:
        return None
    currency = text(core.get("currency_code")).upper() or "USD"
    try:
        return Price(amount=Money.from_major(Decimal(str(found)), currency))
    except (InvalidOperation, ValueError):
        return None


def _address(core: dict[str, Any]) -> Address:
    def part(key: str) -> str | None:
        return text(core.get(key)) or None

    return Address(
        line1=part("address"),
        city=part("city"),
        region=part("state"),
        postal_code=part("zipcode"),
        neighborhood=part("neighborhood_name"),
    )


def _coordinate(core: dict[str, Any], key: str) -> float | None:
    lat, lng = number(core.get("lat")), number(core.get("lng"))
    if lat is None or lng is None:
        return None
    return lat if key == "lat" else lng


def _baths(core: dict[str, Any]) -> float | None:
    full = half(core.get("bathrooms"))
    if full is None:
        return None
    extra = whole(core.get("half_bathrooms")) or 0
    return full + 0.5 * extra


def _property_type(core: dict[str, Any], beds: int | None) -> PropertyType:
    # Only the apartment id (13) is documented; the other ids are Zumper's own enum.
    if whole(core.get("property_type")) == _APARTMENT:
        return PropertyType.STUDIO if beds == 0 else PropertyType.APARTMENT
    return PropertyType.STUDIO if beds == 0 else PropertyType.OTHER


def _owner_type(core: dict[str, Any]) -> OwnerType:
    """Who lists the place, from the item's agent and brokerage fields (#113).

    A name that reads as a management company is a property manager; any other named
    agent or brokerage is an agent. Names stay out of ``raw``.
    """
    names = [text(core.get(k)) for k in ("brokerage_name", "agent_name")]
    if any(_MANAGER_NAME.search(n) for n in names if n):
        return OwnerType.PROPERTY_MANAGER
    if any(names):
        return OwnerType.AGENT
    return OwnerType.UNKNOWN


def _available_from(core: dict[str, Any], today: date) -> date | None:
    for key in _AVAILABLE_KEYS:
        value = core.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            try:
                return datetime.fromtimestamp(value, UTC).date()
            except (OverflowError, OSError, ValueError):
                continue
        when = text(value)
        if when.lower() in {"now", "available now", "immediately"}:
            return today
        try:
            return date.fromisoformat(when[:10])
        except ValueError:
            continue
    return None


def _lease_months(core: dict[str, Any]) -> int | None:
    terms = core.get("lease_terms")
    labels = terms if isinstance(terms, list) else [terms]
    for label in labels:
        if found := _MONTHS.search(text(label if isinstance(label, str) else _lease_label(label))):
            return max(1, int(found.group(1)))
    return None


def _lease_label(value: object) -> str:
    return " ".join(text(v) for v in value.values()) if isinstance(value, dict) else ""


def _images(core: dict[str, Any]) -> tuple[str, ...]:
    media = core.get("media")
    if not isinstance(media, list):
        return ()
    ids = [_object(m).get("media_id") for m in media]
    return tuple(
        f"{IMAGE_CDN}/{i}/1280x960"
        for i in ids
        if isinstance(i, int | str) and not isinstance(i, bool) and str(i).isdigit()
    )


def _raw(core: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in core.items() if k not in _RAW_EXCLUDED}
