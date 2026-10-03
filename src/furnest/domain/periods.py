"""Rent periods and conversion of any listed price to a monthly equivalent.

Sources quote rent per night, per week or per month. Matching and display always
compare monthly amounts, using these exact factors:

* nightly: x 365 / 12 (≈ 30.42 nights per month)
* weekly:  x 52 / 12  (≈ 4.33 weeks per month)
* monthly: x 1

The factors are kept as fractions and the result is rounded half-up once, to a
whole minor unit, so the conversion never accumulates float error.
"""

from __future__ import annotations

from enum import StrEnum
from fractions import Fraction

from furnest.domain.money import Money


class RentPeriod(StrEnum):
    NIGHTLY = "nightly"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


MONTHLY_FACTOR: dict[RentPeriod, Fraction] = {
    RentPeriod.NIGHTLY: Fraction(365, 12),
    RentPeriod.WEEKLY: Fraction(52, 12),
    RentPeriod.MONTHLY: Fraction(1),
}

_PERIOD_ALIASES: dict[str, RentPeriod] = {
    **dict.fromkeys(("nightly", "night", "nt", "nite", "daily", "day", "d"), RentPeriod.NIGHTLY),
    **dict.fromkeys(("weekly", "week", "wk", "w"), RentPeriod.WEEKLY),
    **dict.fromkeys(("monthly", "month", "mo", "mth", "mon", "m"), RentPeriod.MONTHLY),
}


def parse_period(raw: str) -> RentPeriod | None:
    """Map a source's period wording (``"/wk"``, ``"per night"``) to a :class:`RentPeriod`."""
    key = raw.strip().lower().removeprefix("/").removeprefix("per ").strip().rstrip(".")
    if key.endswith("s") and key[:-1] in _PERIOD_ALIASES:
        key = key[:-1]
    return _PERIOD_ALIASES.get(key)


def _round_half_up(value: Fraction) -> int:
    # Amounts are non-negative, so floor(x + 1/2) is round-half-up.
    return int(value + Fraction(1, 2))


def to_monthly(amount: Money, period: RentPeriod) -> Money:
    """Convert ``amount`` quoted per ``period`` to its monthly equivalent."""
    if period is RentPeriod.MONTHLY:
        return amount
    monthly = _round_half_up(amount.amount_minor * MONTHLY_FACTOR[period])
    return Money(amount_minor=monthly, currency=amount.currency)
