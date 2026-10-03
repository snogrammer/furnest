"""A listed price: an amount (or a range) quoted per rent period.

Adapters keep the price as the source quoted it (``$95/night``, ``$1,200-$1,500/mo``)
and :class:`Price` exposes the monthly equivalent for matching. A listing without a
usable price has ``price=None``, never a zero amount, so "free" and "unknown" can't
be confused. :func:`parse_price` turns the free-text prices that many sources show
into a :class:`Price`.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from furnest.domain.money import DEFAULT_CURRENCY, Money
from furnest.domain.periods import RentPeriod, parse_period, to_monthly


class Price(BaseModel):
    """``amount`` per ``period``. ``amount_max`` is set when the source quotes a range."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: Money
    amount_max: Money | None = None
    period: RentPeriod = RentPeriod.MONTHLY

    @model_validator(mode="after")
    def _valid_range(self) -> Self:
        if self.amount_max is not None:
            if self.amount_max.currency != self.amount.currency:
                raise ValueError("amount and amount_max must use the same currency")
            if self.amount_max.amount_minor < self.amount.amount_minor:
                raise ValueError("amount_max must not be below amount")
        return self

    @property
    def currency(self) -> str:
        return self.amount.currency

    @property
    def monthly(self) -> Money:
        """The monthly equivalent of the (lower) amount."""
        return to_monthly(self.amount, self.period)

    @property
    def monthly_max(self) -> Money:
        """The monthly equivalent of the upper bound; equals :attr:`monthly` for a single price."""
        return to_monthly(self.amount_max or self.amount, self.period)


_NUMBER = r"(?P<{name}>\d[\d,]*(?:\.\d+)?)\s*(?P<{name}_k>k\b)?"
_PRICE_RE = re.compile(
    r"(?P<sym>\$)?\s*"
    + _NUMBER.format(name="lo")
    + r"(?:\s*(?:-|\u2013|\u2014|to)\s*\$?\s*"
    + _NUMBER.format(name="hi")
    + r")?",
    re.IGNORECASE,
)
_PERIOD_RE = re.compile(
    r"(?:/|\bper\b|\ba\b|\ban\b)?\s*"
    r"\b(nightly|nights?|nite|nt|daily|days?|weekly|weeks?|wk|monthly|months?|mo|mth)\b",
    re.IGNORECASE,
)


def _to_decimal(number: str, thousands: bool) -> Decimal:
    value = Decimal(number.replace(",", ""))
    return value * 1000 if thousands else value


def parse_price(
    text: str | None,
    *,
    default_period: RentPeriod = RentPeriod.MONTHLY,
    currency: str = DEFAULT_CURRENCY,
) -> Price | None:
    """Parse free text such as ``"$1,200 - $1,500 per month"`` or ``"1.5k/wk"``.

    Ranges may use a hyphen, an en or em dash, or "to". An amount written with ``$`` is
    preferred over a bare number, so ``"2 bed, $2,000/mo"`` reads as $2,000. The period
    comes from the first period word after the amount (else anywhere in the text), falling
    back to ``default_period``. Returns ``None`` when there is no usable (non-zero) amount.
    """
    if not text:
        return None
    matches = list(_PRICE_RE.finditer(text))
    if not matches:
        return None
    match = next((m for m in matches if m.group("sym")), matches[0])

    hi_k = bool(match.group("hi_k"))
    lo_k = bool(match.group("lo_k"))
    lo = _to_decimal(match.group("lo"), lo_k)
    hi = _to_decimal(match.group("hi"), hi_k) if match.group("hi") else None
    # "1.5-2k" means 1,500-2,000: a trailing k covers a bare lower bound too.
    if hi is not None and hi_k and not lo_k and lo < 1000:
        lo *= 1000
    if lo <= 0:
        return None
    if hi is not None and hi < lo:
        lo, hi = hi, lo

    period_match = _PERIOD_RE.search(text, match.end()) or _PERIOD_RE.search(text)
    period = (parse_period(period_match.group(1)) if period_match else None) or default_period

    amount = Money.from_major(lo, currency)
    amount_max = Money.from_major(hi, currency) if hi is not None and hi != lo else None
    return Price(amount=amount, amount_max=amount_max, period=period)
