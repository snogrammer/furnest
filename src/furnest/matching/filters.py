"""Hard filters: does a listing meet every hard constraint in the criteria, and why (not)?

Each rule in :class:`~furnest.domain.criteria.FilterRule` answers with one of three
statuses and a human-readable reason. ``PASS`` and ``FAIL`` are definite. ``UNKNOWN``
means the listing lacks the data to decide, such as a missing bed count, no price, or
an amenity it never mentions. Unknown passes unless the rule is listed in
``criteria.unknown_fails``, so a sparse listing is kept by default but every gap shows
up in the breakdown.

Only rules the criteria configure are evaluated. An empty ``Criteria()`` yields just
the furnished rule, which is on by default. A listing passes when no rule blocks it,
and the result keeps every rule's verdict so the dashboard and Discord can show why.

Prices are compared as monthly equivalents. A quoted range passes if it overlaps the
wanted range, and a price in another currency is unknown because there is no FX.
Amenities come from an :class:`~furnest.domain.amenities.AmenityResult`, so an amenity
the listing ruled out ("no pets") fails a must-have, while one it never mentions is
unknown.

Pure: no I/O and no DB. Location delegates to :func:`~furnest.matching.location.match_location`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from furnest.domain import (
    AddressParser,
    Amenity,
    AmenityResult,
    Criteria,
    FilterRule,
    NormalizedListing,
    OwnerType,
    PropertyType,
    RentPeriod,
)
from furnest.matching.location import LocationStatus, match_location


class RuleStatus(StrEnum):
    PASS = "pass"  # noqa: S105 - a status, not a password
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule: FilterRule
    status: RuleStatus
    reason: str
    # True when this rule rejects the listing: a fail, or an unknown the criteria treat as one.
    blocking: bool


@dataclass(frozen=True, slots=True)
class HardFilterResult:
    rules: tuple[RuleResult, ...]

    @property
    def passed(self) -> bool:
        return not any(result.blocking for result in self.rules)

    @property
    def failed(self) -> tuple[RuleResult, ...]:
        return tuple(result for result in self.rules if result.blocking)

    @property
    def unknown(self) -> tuple[RuleResult, ...]:
        return tuple(result for result in self.rules if result.status is RuleStatus.UNKNOWN)


@dataclass(frozen=True, slots=True)
class _Context:
    listing: NormalizedListing
    criteria: Criteria
    amenities: AmenityResult
    parser: AddressParser | None


Verdict = tuple[RuleStatus, str]
_VOCABULARY = {tag.value: tag for tag in Amenity}


def _num(value: float) -> str:
    return f"{value:g}"


def _plural(count: float, noun: str) -> str:
    return f"{_num(count)} {noun}{'' if count == 1 else 's'}"


def _location(ctx: _Context) -> Verdict | None:
    result = match_location(ctx.listing, ctx.criteria, parser=ctx.parser)
    status = {
        LocationStatus.MATCH: RuleStatus.PASS,
        LocationStatus.NO_MATCH: RuleStatus.FAIL,
        LocationStatus.UNKNOWN: RuleStatus.UNKNOWN,
    }.get(result.status)
    return None if status is None else (status, "; ".join(result.reasons))


def _price(ctx: _Context) -> Verdict | None:
    low, high = ctx.criteria.price_min, ctx.criteria.price_max
    if low is None and high is None:
        return None
    price = ctx.listing.price
    if price is None:
        return RuleStatus.UNKNOWN, "price unknown"
    currency = next(bound.currency for bound in (low, high) if bound is not None)
    if price.currency != currency:
        return RuleStatus.UNKNOWN, f"price in {price.currency}, criteria in {currency}"
    lo, hi = price.monthly, price.monthly_max
    quoted = lo.format() if lo == hi else f"{lo.format()}-{hi.format()}"
    quoted += "/mo"
    if price.period is not RentPeriod.MONTHLY:
        quoted += f" (quoted {price.period})"
    if high is not None and lo.amount_minor > high.amount_minor:
        return RuleStatus.FAIL, f"{quoted} is above max {high.format()}"
    if low is not None and hi.amount_minor < low.amount_minor:
        return RuleStatus.FAIL, f"{quoted} is below min {low.format()}"
    bounds = ", ".join(
        f"{word} {bound.format()}"
        for word, bound in (("min", low), ("max", high))
        if bound is not None
    )
    return RuleStatus.PASS, f"{quoted} is within {bounds}"


def _property_type(ctx: _Context) -> Verdict | None:
    allow, deny = ctx.criteria.property_types_allow, ctx.criteria.property_types_deny
    if not (allow or deny):
        return None
    kind = ctx.listing.property_type
    if kind in deny:
        return RuleStatus.FAIL, f"{kind} is denied"
    if kind in allow:
        return RuleStatus.PASS, f"{kind} is allowed"
    if kind is PropertyType.OTHER:
        return RuleStatus.UNKNOWN, "property type unrecognized"
    if allow:
        return RuleStatus.FAIL, f"{kind} is not in the allowed types"
    return RuleStatus.PASS, f"{kind} is not denied"


def _owner_type(ctx: _Context) -> Verdict | None:
    deny = ctx.criteria.owner_types_deny
    if not deny:
        return None
    kind = ctx.listing.owner_type
    if kind in deny:
        return RuleStatus.FAIL, f"{kind} is denied"
    if kind is OwnerType.UNKNOWN:
        return RuleStatus.UNKNOWN, "owner type unknown"
    return RuleStatus.PASS, f"{kind} is not denied"


def _at_least(value: float | None, minimum: float | None, noun: str, label: str) -> Verdict | None:
    if minimum is None:
        return None
    if value is None:
        return RuleStatus.UNKNOWN, f"{noun} unknown"
    if value >= minimum:
        return RuleStatus.PASS, f"{label} meets minimum {_num(minimum)}"
    return RuleStatus.FAIL, f"{label} is below minimum {_num(minimum)}"


def _min_beds(ctx: _Context) -> Verdict | None:
    beds = ctx.listing.beds
    label = "studio" if beds == 0 else _plural(beds or 0, "bed")
    return _at_least(beds, ctx.criteria.min_beds, "beds", label)


def _min_baths(ctx: _Context) -> Verdict | None:
    baths = ctx.listing.baths
    return _at_least(baths, ctx.criteria.min_baths, "baths", _plural(baths or 0, "bath"))


def _min_sqft(ctx: _Context) -> Verdict | None:
    sqft = ctx.listing.sqft
    return _at_least(sqft, ctx.criteria.min_sqft, "square footage", f"{sqft} sqft")


def _furnished(ctx: _Context) -> Verdict | None:
    if not ctx.criteria.furnished_required:
        return None
    return {
        True: (RuleStatus.PASS, "furnished"),
        False: (RuleStatus.FAIL, "unfurnished"),
        None: (RuleStatus.UNKNOWN, "furnished status unknown"),
    }[ctx.listing.furnished]


def _move_in(ctx: _Context) -> Verdict | None:
    window = ctx.criteria.move_in_window()
    if window is None:
        return None
    available = ctx.listing.available_from
    if available is None:
        return RuleStatus.UNKNOWN, "availability date unknown"
    earliest, latest = window
    # A place free before the window opens is still free during it.
    if available > latest:
        return RuleStatus.FAIL, f"available from {available}, after latest move-in {latest}"
    if available < earliest:
        return RuleStatus.PASS, f"available from {available}, before the move-in window opens"
    return RuleStatus.PASS, f"available from {available}, within the move-in window"


def _lease(ctx: _Context) -> Verdict | None:
    want_min, want_max = ctx.criteria.lease_months_range()
    if want_min is None and want_max is None:
        return None
    has_min, has_max = ctx.listing.min_lease_months, ctx.listing.max_lease_months
    if want_max is not None and has_min is not None and has_min > want_max:
        return RuleStatus.FAIL, (
            f"minimum lease {_plural(has_min, 'month')} exceeds maximum {want_max}"
        )
    if want_min is not None and has_max is not None and has_max < want_min:
        return RuleStatus.FAIL, (
            f"maximum lease {_plural(has_max, 'month')} is below minimum {want_min}"
        )
    if (want_max is not None and has_min is None) or (want_min is not None and has_max is None):
        return RuleStatus.UNKNOWN, "lease terms unknown"
    return RuleStatus.PASS, "lease terms fit"


def _must_have_amenities(ctx: _Context) -> Verdict | None:
    wanted = ctx.criteria.must_have_amenities
    if not wanted:
        return None
    absent = [tag for tag in wanted if ctx.amenities.status(tag) is False]
    if absent:
        return RuleStatus.FAIL, f"ruled out: {', '.join(absent)}"
    unknown = [
        tag if tag in _VOCABULARY else f"{tag} (not a known amenity tag)"
        for tag in wanted
        if ctx.amenities.status(tag) is None
    ]
    if unknown:
        return RuleStatus.UNKNOWN, f"not stated: {', '.join(unknown)}"
    return RuleStatus.PASS, f"has {', '.join(wanted)}"


def _keyword_hits(ctx: _Context, keywords: tuple[str, ...]) -> list[str]:
    text = f"{ctx.listing.title}\n{ctx.listing.description}"
    return [
        keyword
        for keyword in keywords
        if re.search(
            rf"(?<!\w){r'\s+'.join(map(re.escape, keyword.split()))}(?!\w)", text, re.IGNORECASE
        )
    ]


def _keywords_allow(ctx: _Context) -> Verdict | None:
    wanted = ctx.criteria.keywords_allow
    if not wanted:
        return None
    if hits := _keyword_hits(ctx, wanted):
        return RuleStatus.PASS, f"mentions {', '.join(hits)}"
    return RuleStatus.FAIL, f"mentions none of: {', '.join(wanted)}"


def _keywords_deny(ctx: _Context) -> Verdict | None:
    denied = ctx.criteria.keywords_deny
    if not denied:
        return None
    if hits := _keyword_hits(ctx, denied):
        return RuleStatus.FAIL, f"mentions denied {', '.join(hits)}"
    return RuleStatus.PASS, "no denied keywords"


_RULES: tuple[tuple[FilterRule, Callable[[_Context], Verdict | None]], ...] = (
    (FilterRule.LOCATION, _location),
    (FilterRule.PRICE, _price),
    (FilterRule.PROPERTY_TYPE, _property_type),
    (FilterRule.OWNER_TYPE, _owner_type),
    (FilterRule.MIN_BEDS, _min_beds),
    (FilterRule.MIN_BATHS, _min_baths),
    (FilterRule.MIN_SQFT, _min_sqft),
    (FilterRule.FURNISHED, _furnished),
    (FilterRule.MOVE_IN, _move_in),
    (FilterRule.LEASE, _lease),
    (FilterRule.MUST_HAVE_AMENITIES, _must_have_amenities),
    (FilterRule.KEYWORDS_ALLOW, _keywords_allow),
    (FilterRule.KEYWORDS_DENY, _keywords_deny),
)


def listing_amenities(listing: NormalizedListing) -> AmenityResult:
    # Without an extractor result, trust the adapter's tags; nothing is known to be absent.
    return AmenityResult(
        present=frozenset(_VOCABULARY[tag] for tag in listing.amenities if tag in _VOCABULARY)
    )


def evaluate_hard_filters(
    listing: NormalizedListing,
    criteria: Criteria,
    *,
    amenities: AmenityResult | None = None,
    parser: AddressParser | None = None,
) -> HardFilterResult:
    """Check ``listing`` against every configured hard constraint in ``criteria``.

    ``amenities`` is the extractor's result for the listing. If it is omitted, the
    listing's own amenity tags count as present. ``parser`` is passed to location matching.
    """
    if amenities is None:
        amenities = listing_amenities(listing)
    ctx = _Context(listing, criteria, amenities, parser)
    results: list[RuleResult] = []
    for rule, check in _RULES:
        verdict = check(ctx)
        if verdict is None:
            continue
        status, reason = verdict
        blocking = status is RuleStatus.FAIL or (
            status is RuleStatus.UNKNOWN and rule in criteria.unknown_fails
        )
        results.append(RuleResult(rule, status, reason, blocking))
    return HardFilterResult(tuple(results))
