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

from dataclasses import dataclass
from datetime import date
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

from furnest.domain.geo import DistanceUnit
from furnest.domain.money import Money
from furnest.domain.property_type import PropertyType


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


class Criteria(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Location: a listing qualifies if it is in any area, ZIP or neighborhood (#13).
    areas: tuple[Area, ...] = ()
    zip_codes: TextList = ()
    neighborhoods: TagList = ()

    # Monthly rent range (see furnest.domain.periods for the conversion).
    price_min: Money | None = None
    price_max: Money | None = None

    # Empty allow list means any type; deny always wins.
    property_types_allow: TypeList = ()
    property_types_deny: TypeList = ()

    min_beds: int | None = Field(default=None, ge=0)
    min_baths: float | None = Field(default=None, ge=0, multiple_of=0.5)
    min_sqft: int | None = Field(default=None, gt=0)
    furnished_required: bool = True

    # The listing must be available somewhere in this window.
    move_in_earliest: date | None = None
    move_in_latest: date | None = None
    lease_min_months: int | None = Field(default=None, ge=1)
    lease_max_months: int | None = Field(default=None, ge=1)

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

    # The searcher's own plans, quoted in inquiry emails (P6).
    my_move_in_date: date | None = None
    my_lease_months: int | None = Field(default=None, ge=1)

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
        if (
            self.move_in_earliest is not None
            and self.move_in_latest is not None
            and self.move_in_earliest > self.move_in_latest
        ):
            raise ValueError("move_in_earliest must not be after move_in_latest")
        if (
            self.lease_min_months is not None
            and self.lease_max_months is not None
            and self.lease_min_months > self.lease_max_months
        ):
            raise ValueError("lease_min_months must not exceed lease_max_months")
        if overlap := set(self.must_have_amenities) & set(self.nice_to_have_amenities):
            raise ValueError(f"amenities both required and nice-to-have: {sorted(overlap)}")
        if overlap := set(self.keywords_allow) & set(self.keywords_deny):
            raise ValueError(f"keywords both allowed and denied: {sorted(overlap)}")
        return self


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
