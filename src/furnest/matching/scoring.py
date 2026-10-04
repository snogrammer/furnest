"""Scoring: how well does a listing fit, from 0 to 100, and why?

The hard filters (:mod:`furnest.matching.filters`) decide whether a listing qualifies at
all. Scoring ranks the ones that do. Each scoring component has a weight from the
criteria and earns a fraction of it between 0 and 1:

- every nice-to-have amenity tag earns all of its weight if the listing states it, and
  nothing if the listing rules it out or never mentions it;
- price fit (opt-in via ``price_fit_weight``, needs ``price_max``) earns more the lower
  the monthly price sits in the wanted range, using the midpoint of a quoted range;
- extra beds (opt-in via ``extra_beds_weight``, needs ``min_beds``) earns half for one
  bed above the minimum and all of it for two or more.

The score is the earned share of the total weight, scaled to 0-100 and rounded half-up,
so only the ratio of the weights matters. Each :class:`BreakdownItem` shows its share in
the same 0-100 points, so the items add up to the score (give or take rounding). Criteria
with nothing to score give every listing 100: there is nothing left to want.

:func:`match_listing` combines both steps into a :class:`MatchResult`, which serializes
to JSON together with the criteria version that produced it, so a stored result can
always be explained. Failed listings are scored too, but only ``passed`` ones are ranked.

Pure: no I/O and no DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field

from furnest.domain import (
    AddressParser,
    Amenity,
    AmenityResult,
    Criteria,
    Money,
    NormalizedListing,
    RentPeriod,
)
from furnest.matching.filters import RuleResult, evaluate_hard_filters, listing_amenities

# Beds above the minimum that earn the full extra-beds weight.
EXTRA_BEDS_FOR_FULL = 2

_VOCABULARY = {tag.value for tag in Amenity}
_LABELS: dict[str, str] = {
    Amenity.AC: "AC",
    Amenity.WIFI: "Wi-Fi",
    Amenity.IN_UNIT_LAUNDRY: "In-unit laundry",
    Amenity.EV_CHARGING: "EV charging",
    Amenity.DECK_PATIO: "Deck/patio",
}


@dataclass(frozen=True, slots=True)
class BreakdownItem:
    label: str
    # Points earned and available on the 0-100 scale of the score.
    points: float
    max_points: float
    reason: str


class MatchResult(BaseModel):
    """The full verdict for one listing against one criteria version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    score: int = Field(ge=0, le=100)
    breakdown: tuple[BreakdownItem, ...] = ()
    # Every hard-filter verdict, passing ones included.
    rules: tuple[RuleResult, ...] = ()
    criteria_version: int | None = None

    @property
    def failed_rules(self) -> tuple[RuleResult, ...]:
        return tuple(result for result in self.rules if result.blocking)


@dataclass(frozen=True, slots=True)
class _Component:
    label: str
    weight: float
    earned: float  # fraction of the weight, 0-1
    reason: str


def _label(tag: str) -> str:
    return _LABELS.get(tag) or tag.replace("_", " ").capitalize()


def _amenity(tag: str, weight: float, amenities: AmenityResult) -> _Component:
    status = amenities.status(tag)
    if status is True:
        return _Component(_label(tag), weight, 1.0, "stated")
    if status is False:
        return _Component(_label(tag), weight, 0.0, "ruled out")
    reason = "not mentioned" if tag in _VOCABULARY else "not a known amenity tag"
    return _Component(_label(tag), weight, 0.0, reason)


def _price_fit(listing: NormalizedListing, criteria: Criteria, weight: float) -> _Component:
    label = "Price fit"
    high, low = criteria.price_max, criteria.price_min
    assert high is not None  # noqa: S101 - the caller checks
    price = listing.price
    if price is None:
        return _Component(label, weight, 0.0, "price unknown")
    if price.currency != high.currency:
        return _Component(
            label, weight, 0.0, f"price in {price.currency}, criteria in {high.currency}"
        )
    lo, hi = price.monthly, price.monthly_max
    mid = Money(amount_minor=(lo.amount_minor + hi.amount_minor) // 2, currency=lo.currency)
    quoted = (
        f"{mid.format()}/mo"
        if lo == hi
        else f"{mid.format()}/mo (midpoint of {lo.format()}-{hi.format()})"
    )
    if price.period is not RentPeriod.MONTHLY:
        quoted += f" (quoted {price.period})"
    floor = low.amount_minor if low is not None else 0
    if mid.amount_minor >= high.amount_minor:
        return _Component(label, weight, 0.0, f"{quoted} is at or above max {high.format()}")
    if mid.amount_minor <= floor:
        bound = f"min {low.format()}" if low is not None else "nothing"
        return _Component(label, weight, 1.0, f"{quoted} is at or below {bound}")
    earned = (high.amount_minor - mid.amount_minor) / (high.amount_minor - floor)
    pct = _round(earned * 100)
    if low is None:
        reason = f"{quoted} is {pct}% below max {high.format()}"
    else:
        reason = (
            f"{quoted} is {pct}% of the way from max {high.format()} down to min {low.format()}"
        )
    return _Component(label, weight, earned, reason)


def _extra_beds(listing: NormalizedListing, minimum: int, weight: float) -> _Component:
    label = "Extra beds"
    beds = listing.beds
    if beds is None:
        return _Component(label, weight, 0.0, "beds unknown")
    have = "studio" if beds == 0 else f"{beds} bed{'' if beds == 1 else 's'}"
    extra = beds - minimum
    if extra < 0:
        return _Component(label, weight, 0.0, f"{have} is below minimum {minimum}")
    if extra == 0:
        return _Component(label, weight, 0.0, f"{have}, none above minimum {minimum}")
    earned = min(extra, EXTRA_BEDS_FOR_FULL) / EXTRA_BEDS_FOR_FULL
    return _Component(label, weight, earned, f"{have}, {extra} above minimum {minimum}")


def _components(
    listing: NormalizedListing, criteria: Criteria, amenities: AmenityResult
) -> list[_Component]:
    components = [
        _amenity(tag, weight, amenities) for tag, weight in criteria.nice_to_have_amenities.items()
    ]
    if criteria.price_fit_weight is not None and criteria.price_max is not None:
        components.append(_price_fit(listing, criteria, criteria.price_fit_weight))
    if criteria.extra_beds_weight is not None and criteria.min_beds is not None:
        components.append(_extra_beds(listing, criteria.min_beds, criteria.extra_beds_weight))
    return components


def _round(value: float) -> int:
    return int(Decimal(repr(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _points(value: float) -> float:
    return float(Decimal(repr(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def score_listing(
    listing: NormalizedListing,
    criteria: Criteria,
    *,
    amenities: AmenityResult | None = None,
) -> tuple[int, tuple[BreakdownItem, ...]]:
    """Score ``listing`` from 0 to 100 against the scoring components in ``criteria``.

    ``amenities`` is the extractor's result. If it is omitted, the listing's own
    amenity tags count as present.
    """
    if amenities is None:
        amenities = listing_amenities(listing)
    components = _components(listing, criteria, amenities)
    total = sum(component.weight for component in components)
    if not total:
        return 100, ()
    scale = 100 / total
    breakdown = tuple(
        BreakdownItem(
            label=component.label,
            points=_points(component.weight * component.earned * scale),
            max_points=_points(component.weight * scale),
            reason=component.reason,
        )
        for component in components
    )
    earned = sum(component.weight * component.earned for component in components)
    return min(100, max(0, _round(earned * scale))), breakdown


def match_listing(
    listing: NormalizedListing,
    criteria: Criteria,
    *,
    criteria_version: int | None = None,
    amenities: AmenityResult | None = None,
    parser: AddressParser | None = None,
) -> MatchResult:
    """Run the hard filters and scoring for ``listing`` and record ``criteria_version``."""
    if amenities is None:
        amenities = listing_amenities(listing)
    filters = evaluate_hard_filters(listing, criteria, amenities=amenities, parser=parser)
    score, breakdown = score_listing(listing, criteria, amenities=amenities)
    return MatchResult(
        passed=filters.passed,
        score=score,
        breakdown=breakdown,
        rules=filters.rules,
        criteria_version=criteria_version,
    )
