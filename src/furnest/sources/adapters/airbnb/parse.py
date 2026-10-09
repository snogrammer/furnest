"""Mapping between furnest and the Airbnb data provider's JSON.

The adapter gets Airbnb stays through the Apify actor ``tri_angle/airbnb-scraper``
(see ``docs/adapters.md`` for why). This module is pure: :func:`build_input` turns
criteria into the actor's input, and :func:`parse_item` turns one dataset item into
a listing.

Airbnb has no "monthly stays" switch the actor exposes. A search with a check-in and
a check-out at least 28 nights later *is* its monthly-stay search: hosts who don't
take stays that long drop out, and the quotes carry the monthly discount. So
:func:`build_input` always sends a :class:`StayWindow` of at least a month, starting
at the earliest move-in date, and every listing that comes back is free for all of it.

Prices come back for that stay. When the item has the stay's total before taxes, the
listing's price is that total spread over a month (``total x (365/12) / nights``):
the monthly discount, cleaning fee and service fee are all in it, so it's what a
renter would pay. Without a total, the quoted rate is kept with its period ("$107
night" stays nightly) and :class:`~furnest.domain.Price` converts it to monthly.

Item fields, as the actor documents them, map like this:

=====================================  ==============================================
actor field                            NormalizedListing
=====================================  ==============================================
``id``                                 ``source_id``
``url`` (else ``/rooms/<id>``)         ``url``
``title`` (else ``subDescription``)    ``title``, without the " - … - Airbnb" suffix
``description``                        ``description``
``price`` (``.price``, else            ``price``, per month or per quoted period, USD
``.amount``; or the stay total)
``locationSubtitle`` (else             ``address`` city and region, US places only
``subDescription.title``)
``coordinates``                        ``lat``, ``lng``
``subDescription.items``               ``beds`` ("Studio" is 0), ``baths``
``roomType``, ``subDescription``       ``property_type``
``amenities`` groups                   ``amenities``; "Not included" as "no <name>"
``images`` (else ``thumbnail``)        ``images``
=====================================  ==============================================

Every listing is ``furnished``, is available from the check-in date, and has
``contact.platform_only`` set: hosts are reached through Airbnb's messages. Host and
co-host details are dropped from ``raw`` because they name people.
"""

from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from typing import Any

from furnest.domain import (
    MONTHLY_FACTOR,
    Address,
    Contact,
    Criteria,
    Money,
    NormalizedListing,
    OwnerType,
    Price,
    PropertyType,
    RentPeriod,
    infer_owner_type,
    normalize_property_type,
    parse_period,
    parse_price,
)
from furnest.sources.adapters._values import half, number, text, whole

SOURCE = "airbnb"
ACTOR = "tri_angle/airbnb-scraper"
SITE = "https://www.airbnb.com"
_USD = "USD"

# Item keys left out of ``raw``: people (host, co-hosts) and app deep links.
_RAW_EXCLUDED = frozenset({"host", "coHosts", "androidLink", "iosLink"})
# A host with this many reviews runs listings at scale; a team of co-hosts is a company.
_MANAGER_RATING_COUNT = 1000
_MANAGER_COHOSTS = 2
_NOT_INCLUDED = "not included"
_TITLE_SUFFIX = re.compile(r"(?:^|\s+)-\s+.*\s+-\s+Airbnb$")
_COUNT = re.compile(r"^(?P<n>\d+(?:\.\d+)?)\s+(?:\w+\s+)?(?P<what>bedroom|bath|half-bath)s?\b")
_US = frozenset({"united states", "usa", "us"})


@dataclass(frozen=True)
class StayWindow:
    """The dates a search asked for; every result is free for all of them."""

    check_in: date
    check_out: date

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days


# --- Criteria → actor input -------------------------------------------------------


def stay_window(criteria: Criteria, *, today: date) -> StayWindow:
    """From the earliest move-in (or today) for the minimum lease, or a month.

    A month is never shorter than 28 nights, Airbnb's monthly-stay threshold.
    """
    earliest = criteria.move_in_earliest
    check_in = earliest if earliest is not None and earliest >= today else today
    months = criteria.lease_min_months or 1
    return StayWindow(check_in, _add_months(check_in, months))


def _add_months(day: date, months: int) -> date:
    # Jan 31 + 1 month is the last day of February, not an error.
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return day.replace(
        year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1])
    )


def build_input(
    criteria: Criteria, *, locations: tuple[str, ...], max_results: int, today: date
) -> dict[str, Any]:
    window = stay_window(criteria, today=today)
    run_input: dict[str, Any] = {
        "locationQueries": list(locations),
        "checkIn": window.check_in.isoformat(),
        "checkOut": window.check_out.isoformat(),
        "currency": _USD,
        "locale": "en-US",
        "maxResults": max_results,
        # Host profiles cost an extra request each, bill as "enriched", and name people.
        "enrichUserProfiles": False,
    }
    # Price isn't sent: the actor doesn't say whether its bounds are nightly or for the
    # stay, and guessing wrong would silently drop matches. Matching filters price.
    if criteria.min_beds is not None and criteria.min_beds > 0:
        run_input["minBedrooms"] = criteria.min_beds
    if criteria.min_baths is not None and criteria.min_baths >= 1:
        # The actor takes whole baths; rounding down only widens the search.
        run_input["minBathrooms"] = math.floor(criteria.min_baths)
    return run_input


def window_of(run_input: dict[str, Any]) -> StayWindow:
    """The :class:`StayWindow` a :func:`build_input` result asked for."""
    return StayWindow(
        date.fromisoformat(run_input["checkIn"]), date.fromisoformat(run_input["checkOut"])
    )


# --- Actor item → NormalizedListing -------------------------------------------------


def parse_item(item: dict[str, Any], *, window: StayWindow) -> NormalizedListing:
    """Map one dataset item. Raises ``ValueError`` (pydantic's included) if it's unusable."""
    room_id = _id(item.get("id"))
    if room_id is None:
        msg = "item has no id"
        raise ValueError(msg)
    sub = _object(item.get("subDescription"))
    kind, place = _kind_and_place(text(sub.get("title")))
    beds, baths = _sizes(sub.get("items"))
    lat = number(_dig(item, "coordinates", "latitude"))
    lng = number(_dig(item, "coordinates", "longitude"))
    if lat is None or lng is None:
        lat = lng = None
    return NormalizedListing(
        source=SOURCE,
        source_id=room_id,
        url=_url(item) or f"{SITE}/rooms/{room_id}",  # type: ignore[arg-type]
        title=_title(item, sub),
        description=text(item.get("description")),
        price=_price(_object(item.get("price")), window),
        # Live items name the country only in ``locationSubtitle``
        # ("Exampleville, Texas, United States"); ``subDescription`` stops at the state.
        address=_address(text(item.get("locationSubtitle"))) or _address(place) or Address(),
        lat=lat,
        lng=lng,
        beds=beds,
        baths=baths,
        property_type=_property_type(text(item.get("roomType")), kind, beds),
        owner_type=infer_owner_type(
            _title(item, sub), text(item.get("description")), hint=_owner_type(item)
        ),
        furnished=True,
        amenities=_amenities(item.get("amenities")),
        available_from=window.check_in,
        images=_photos(item),  # type: ignore[arg-type]
        contact=Contact(platform_only=True),
        raw={k: v for k, v in item.items() if k not in _RAW_EXCLUDED},
    )


def _owner_type(item: dict[str, Any]) -> OwnerType:
    """Who hosts the place (#113), from counts in the host block, which itself stays
    out of ``raw``.

    An Airbnb account is a person's by default, so a host is an individual unless the
    account works at scale: thousands of reviews, or a team of co-hosts. The Superhost
    badge and the time as host say nothing about that. No host block: unknown.
    """
    host = _object(item.get("host"))
    if not host:
        return OwnerType.UNKNOWN
    reviews = whole(host.get("ratingCount")) or 0
    cohosts = item.get("coHosts")
    team = len(cohosts) if isinstance(cohosts, list) else 0
    if reviews >= _MANAGER_RATING_COUNT or team >= _MANAGER_COHOSTS:
        return OwnerType.PROPERTY_MANAGER
    return OwnerType.INDIVIDUAL


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
    value = text(item.get("url"))
    return value if value.startswith(f"{SITE}/") else None


def _title(item: dict[str, Any], sub: dict[str, Any]) -> str:
    # "Cozy Loft - Apartments for Rent in Exampleville, ... - Airbnb" → "Cozy Loft".
    title = _TITLE_SUFFIX.sub("", text(item.get("title")))
    return title or text(sub.get("title")) or "Airbnb stay"


def _kind_and_place(title: str) -> tuple[str, str]:
    # "Entire rental unit in Exampleville, Illinois, United States"; the last " in "
    # splits, since a kind can hold one ("Private room in home").
    kind, found, place = title.rpartition(" in ")
    return (kind, place) if found else (title, "")


def _address(place: str) -> Address | None:
    parts = [part.strip() for part in place.split(",") if part.strip()]
    if not parts:
        return None
    if len(parts) >= 3 and parts[-1].lower() in _US:
        return Address(city=parts[0], region=parts[-2])
    # US-first: a region outside the US isn't a US state, so only the city is kept.
    return Address(city=parts[0])


def _sizes(values: object) -> tuple[int | None, float | None]:
    """Beds and baths from strings like "Studio", "2 bedrooms", "1.5 shared baths"."""
    beds: int | None = None
    baths: float | None = None
    for value in values if isinstance(values, list) else ():
        label = text(value).lower()
        if label == "studio":
            beds = 0
        elif found := _COUNT.match(label):
            if found.group("what") == "bedroom":
                beds = whole(found.group("n"))
            elif found.group("what") == "bath":
                baths = half(found.group("n"))
            else:
                baths = 0.5
        elif label in {"half-bath", "shared half-bath", "private half-bath"}:
            baths = 0.5
    return beds, baths


def _property_type(room_type: str, kind: str, beds: int | None) -> PropertyType:
    if room_type.lower() in {"private room", "shared room", "hotel room"}:
        return PropertyType.ROOM
    # "Entire rental unit" → "rental unit", then its last word ("unit") if needed.
    # In "Room in bed and breakfast" the place itself comes first.
    label = kind.lower().partition(" in ")[0].removeprefix("entire ").strip()
    found = normalize_property_type(label)
    if found is PropertyType.OTHER and label:
        found = normalize_property_type(label.split()[-1])
    if found is PropertyType.ROOM:
        return found
    if beds == 0 and found in {PropertyType.APARTMENT, PropertyType.CONDO, PropertyType.OTHER}:
        return PropertyType.STUDIO
    return found


def _price(price: dict[str, Any], window: StayWindow) -> Price | None:
    total = parse_price(
        text(_dig(price, "breakDown", "totalBeforeTaxes", "price")) or None, currency=_USD
    )
    if total is not None and window.nights > 0:
        monthly = total.amount.amount_minor * MONTHLY_FACTOR[RentPeriod.NIGHTLY] / window.nights
        return Price(amount=Money(amount_minor=int(monthly + Fraction(1, 2)), currency=_USD))
    # Live items quote in ``price`` ("$4,336", after any discount); the README says
    # ``amount``.
    quoted = parse_price(
        text(price.get("price")) or text(price.get("amount")) or None, currency=_USD
    )
    if quoted is None:
        return None
    period = parse_period(text(price.get("qualifier"))) or RentPeriod.NIGHTLY
    return Price(amount=quoted.amount, period=period)


def _amenities(groups: object) -> frozenset[str]:
    tags: set[str] = set()
    for group in groups if isinstance(groups, list) else ():
        if not isinstance(group, dict):
            continue
        missing = text(group.get("title")).lower() == _NOT_INCLUDED
        for value in group.get("values") or ():
            label = text(value.get("title")).lower() if isinstance(value, dict) else ""
            if not label:
                continue
            if missing:
                tags.add(f"no {label}")
            elif value.get("available") is True:
                tags.add(label)
    return frozenset(tags)


def _photos(item: dict[str, Any]) -> tuple[str, ...]:
    photos = item.get("images")
    urls: list[object] = []
    for photo in photos if isinstance(photos, list) else ():
        urls.append(photo.get("imageUrl") if isinstance(photo, dict) else photo)
    if not urls:
        urls = [item.get("thumbnail")]
    return tuple(url for url in urls if isinstance(url, str) and url.startswith("http"))
