"""Criteria: the single search profile every listing is matched against.

A :class:`Criteria` is a frozen value. Editing the profile saves a new version
(see :mod:`furnest.db.criteria_store`), so every match can be explained against the
exact criteria that produced it. An empty ``Criteria()`` is valid and matches
broadly; only ``furnished_required`` defaults to on.

Collections are stored as sorted, de-duplicated tuples (tags and keywords also
lower-cased), so equal profiles serialize to identical JSON and :func:`diff_criteria`
reports only real changes. All prices are monthly amounts.
"""

from __future__ import annotations

import calendar
import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

from furnest.domain.geo import DistanceUnit, ring_area
from furnest.domain.money import Money
from furnest.domain.owner_type import OwnerType
from furnest.domain.places import places_within
from furnest.domain.property_type import PropertyType

logger = logging.getLogger(__name__)

MAX_FLEX_DAYS = 365
# Positions in one drawn outline, the closing one included.
MAX_OUTLINE_VERTICES = 500
# Cities searched for each outline; the biggest are kept (see criteria_cities).
MAX_OUTLINE_CITIES = 10
_DAYS_PER_MONTH = 30.4375
_LEGACY_DATE_FIELDS = frozenset(
    {
        "move_in_earliest",
        "move_in_latest",
        "lease_min_months",
        "lease_max_months",
        "my_move_in_date",
        "my_lease_months",
    }
)


def _legacy_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return day.replace(
        year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1])
    )


def _sorted_unique_lower(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({v.strip().lower() for v in values if v.strip()}))


def _sorted_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({v.strip() for v in values if v.strip()}))


def _sorted_members[E: StrEnum](values: tuple[E, ...]) -> tuple[E, ...]:
    return tuple(sorted(set(values)))


class FilterRule(StrEnum):
    """The hard-filter rules (#16), named so criteria can set per-rule behaviour."""

    LOCATION = "location"
    PRICE = "price"
    PROPERTY_TYPE = "property_type"
    OWNER_TYPE = "owner_type"
    MIN_BEDS = "min_beds"
    MIN_BATHS = "min_baths"
    MIN_SQFT = "min_sqft"
    FURNISHED = "furnished"
    MOVE_IN = "move_in"
    LEASE = "lease"
    MUST_HAVE_AMENITIES = "must_have_amenities"
    KEYWORDS_ALLOW = "keywords_allow"
    KEYWORDS_DENY = "keywords_deny"


TagList = Annotated[tuple[str, ...], AfterValidator(_sorted_unique_lower)]
TextList = Annotated[tuple[str, ...], AfterValidator(_sorted_unique)]
TypeList = Annotated[tuple[PropertyType, ...], AfterValidator(_sorted_members)]
OwnerTypeList = Annotated[tuple[OwnerType, ...], AfterValidator(_sorted_members)]
RuleList = Annotated[tuple[FilterRule, ...], AfterValidator(_sorted_members)]
Weight = Annotated[float, Field(gt=0, le=100)]


class Area(BaseModel):
    """A circle to search in: a centre point and a radius."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    radius: float = Field(gt=0)
    unit: DistanceUnit = DistanceUnit.MILES


class Outline(BaseModel):
    """A shape drawn on the dashboard map (#214): one closed ring, GeoJSON order.

    ``polygon`` holds ``(lng, lat)`` positions, the first repeated as the last, so it
    is the ``coordinates[0]`` of a GeoJSON Polygon. Holes aren't supported.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    polygon: tuple[tuple[float, float], ...]

    @model_validator(mode="after")
    def _valid_ring(self) -> Self:
        ring = self.polygon
        if len(ring) < 4:
            msg = "an outline needs at least 3 corners"
            raise ValueError(msg)
        if len(ring) > MAX_OUTLINE_VERTICES:
            msg = f"an outline may have at most {MAX_OUTLINE_VERTICES} points"
            raise ValueError(msg)
        if ring[0] != ring[-1]:
            msg = "an outline must end where it starts"
            raise ValueError(msg)
        for lng, lat in ring:
            if not (-180 <= lng <= 180 and -90 <= lat <= 90):
                msg = f"({lng}, {lat}) is not a longitude, latitude pair"
                raise ValueError(msg)
        if ring_area(ring) == 0:
            msg = "an outline must enclose an area"
            raise ValueError(msg)
        return self


class Criteria(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Location: a listing qualifies if it is in any area, outline, ZIP or neighborhood
    # (#13, #214).
    areas: tuple[Area, ...] = ()
    outlines: tuple[Outline, ...] = ()
    zip_codes: TextList = ()
    neighborhoods: TagList = ()

    # Monthly rent range (see furnest.domain.periods for the conversion).
    price_min: Money | None = None
    price_max: Money | None = None

    # Empty allow list means any type; deny always wins.
    property_types_allow: TypeList = ()
    property_types_deny: TypeList = ()
    # Deny only, no allow list (#113). A listing whose owner type can't be told is
    # UNKNOWN for this rule, so ``unknown_fails`` decides whether that blocks it.
    owner_types_deny: OwnerTypeList = ()

    min_beds: int | None = Field(default=None, ge=0)
    min_baths: float | None = Field(default=None, ge=0, multiple_of=0.5)
    min_sqft: int | None = Field(default=None, gt=0)
    furnished_required: bool = True

    # When the searcher wants to move in and out, and how many days either can slip.
    # The move-in window gates matching and is quoted in inquiry emails (#181).
    move_in: date | None = None
    move_in_flex_days: int = Field(default=7, ge=0, le=MAX_FLEX_DAYS)
    move_out: date | None = None
    move_out_flex_days: int = Field(default=90, ge=0, le=MAX_FLEX_DAYS)

    must_have_amenities: TagList = ()
    # Tag → weight, for scoring (#17).
    nice_to_have_amenities: dict[str, Weight] = Field(default_factory=dict)
    # Optional fit components for scoring, weighted like amenities. Price fit rewards a
    # lower price within the range and needs price_max; extra beds need min_beds.
    price_fit_weight: Weight | None = None
    extra_beds_weight: Weight | None = None

    # Matched case-insensitively against title + description.
    keywords_allow: TagList = ()
    keywords_deny: TagList = ()

    # Rules where missing listing data counts as a fail. Elsewhere unknown passes,
    # flagged in the match reasons.
    unknown_fails: RuleList = ()

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_dates(cls, data: object) -> object:
        """Map criteria stored before #181 (earliest/latest, lease months, my_*)."""
        if not isinstance(data, dict) or not (_LEGACY_DATE_FIELDS & data.keys()):
            return data
        data = dict(data)
        earliest = _legacy_date(data.pop("move_in_earliest", None))
        latest = _legacy_date(data.pop("move_in_latest", None))
        lease_min = data.pop("lease_min_months", None)
        data.pop("lease_max_months", None)
        data.pop("my_move_in_date", None)
        data.pop("my_lease_months", None)
        target = latest or earliest
        if target is not None and "move_in" not in data:
            data["move_in"] = target
            if earliest is not None and latest is not None and earliest < latest:
                data.setdefault("move_in_flex_days", min((latest - earliest).days, MAX_FLEX_DAYS))
        if (
            target is not None
            and "move_out" not in data
            and isinstance(lease_min, int)
            and lease_min >= 1
        ):
            data["move_out"] = _add_months(target, lease_min)
        return data

    @model_validator(mode="before")
    @classmethod
    def _normalize_nice_to_have(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("nice_to_have_amenities"), dict):
            weights = data["nice_to_have_amenities"]
            data = {
                **data,
                "nice_to_have_amenities": {
                    str(tag).strip().lower(): weight
                    for tag, weight in sorted(weights.items())
                    if str(tag).strip()
                },
            }
        return data

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.price_min is not None and self.price_max is not None:
            if self.price_min.currency != self.price_max.currency:
                raise ValueError("price_min and price_max must use the same currency")
            if self.price_min.amount_minor > self.price_max.amount_minor:
                raise ValueError("price_min must not exceed price_max")
        if types := set(self.property_types_allow) & set(self.property_types_deny):
            raise ValueError(f"property types both allowed and denied: {sorted(types)}")
        if self.move_in and self.move_out and self.move_out <= self.move_in:
            raise ValueError("move_out must be after move_in")
        if overlap := set(self.must_have_amenities) & set(self.nice_to_have_amenities):
            raise ValueError(f"amenities both required and nice-to-have: {sorted(overlap)}")
        if overlap := set(self.keywords_allow) & set(self.keywords_deny):
            raise ValueError(f"keywords both allowed and denied: {sorted(overlap)}")
        return self

    def move_in_window(self) -> tuple[date, date] | None:
        """Earliest and latest acceptable move-in, or None when no target is set."""
        if self.move_in is None:
            return None
        flex = timedelta(days=self.move_in_flex_days)
        return self.move_in - flex, self.move_in + flex

    def move_out_window(self) -> tuple[date, date] | None:
        """Earliest and latest acceptable move-out, or None when no target is set."""
        if self.move_out is None:
            return None
        flex = timedelta(days=self.move_out_flex_days)
        return self.move_out - flex, self.move_out + flex

    def lease_months_range(self) -> tuple[int | None, int | None]:
        """Whole months of the shortest and longest stay the two windows allow.

        The shortest rounds down and the longest up, so a lease is only ruled out when
        it can't fit any stay. Both are None unless a move-in and a move-out are set.
        """
        in_window, out_window = self.move_in_window(), self.move_out_window()
        if in_window is None or out_window is None:
            return None, None
        shortest = max((out_window[0] - in_window[1]).days, 0)
        longest = max((out_window[1] - in_window[0]).days, 0)
        return (
            int(shortest // _DAYS_PER_MONTH) or None,
            math.ceil(longest / _DAYS_PER_MONTH) or None,
        )


@dataclass(frozen=True, slots=True)
class CriteriaChange:
    """One top-level field that differs between two criteria, as JSON values."""

    field: str
    old: JsonValue
    new: JsonValue


def diff_criteria(old: Criteria, new: Criteria) -> list[CriteriaChange]:
    """Fields that changed from ``old`` to ``new``, in declaration order."""
    before = old.model_dump(mode="json")
    after = new.model_dump(mode="json")
    return [
        CriteriaChange(field=name, old=before[name], new=after[name])
        for name in Criteria.model_fields
        if before[name] != after[name]
    ]


def criteria_cities(criteria: Criteria) -> tuple[str, ...]:
    """The places to search: areas named "City, State", then each outline's cities.

    The Apify actors only search by city, so an outline becomes the places whose
    centre is inside it, the biggest :data:`MAX_OUTLINE_CITIES` of them to keep run
    costs bounded; matching then keeps only listings inside the outline itself.
    In order and without repeats.
    """
    names = [area.name for area in criteria.areas if "," in area.name]
    for outline in criteria.outlines:
        places, dropped = places_within(outline.polygon, limit=MAX_OUTLINE_CITIES)
        if dropped:
            logger.warning(
                "outline %r holds %d more places than the %d searched",
                outline.name,
                dropped,
                MAX_OUTLINE_CITIES,
            )
        names.extend(place.label for place in places)
    return tuple(dict.fromkeys(names))
