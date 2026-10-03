"""Money as integer minor units plus a currency code.

Amounts are never floats: a price is a whole number of the currency's minor unit
(cents for USD), so comparisons and sums are exact. Currencies live in a small
registry rather than being hardcoded, so adding a locale means registering one
:class:`Currency`, not touching matching or storage code. USD is the default.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator


@dataclass(frozen=True, slots=True)
class Currency:
    """An ISO 4217 currency and how many minor units make one major unit."""

    code: str
    exponent: int
    symbol: str

    @property
    def minor_per_major(self) -> int:
        return int(10**self.exponent)


USD = Currency(code="USD", exponent=2, symbol="$")
DEFAULT_CURRENCY = USD.code

_CURRENCIES: dict[str, Currency] = {}


def register_currency(currency: Currency) -> None:
    """Make ``currency`` known to :class:`Money`. Re-registering a code replaces it."""
    _CURRENCIES[currency.code.upper()] = currency


def get_currency(code: str) -> Currency:
    """Look up a registered currency by code (case-insensitive)."""
    try:
        return _CURRENCIES[code.upper()]
    except KeyError:
        raise ValueError(f"unknown currency {code!r}") from None


register_currency(USD)


class Money(BaseModel):
    """A non-negative amount in minor units of a registered currency."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount_minor: int = Field(ge=0)
    currency: str = DEFAULT_CURRENCY

    @field_validator("currency")
    @classmethod
    def _known_currency(cls, value: str) -> str:
        return get_currency(value).code

    @classmethod
    def from_major(cls, amount: Decimal | str | int, currency: str = DEFAULT_CURRENCY) -> Self:
        """Build from a major-unit amount (``"1234.50"`` → 123450 cents), rounding half-up."""
        cur = get_currency(currency)
        try:
            major = Decimal(amount)
        except InvalidOperation:
            raise ValueError(f"not a number: {amount!r}") from None
        minor = (major * cur.minor_per_major).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return cls(amount_minor=int(minor), currency=cur.code)

    @property
    def major(self) -> Decimal:
        cur = get_currency(self.currency)
        return Decimal(self.amount_minor).scaleb(-cur.exponent)

    def format(self) -> str:
        """Human-readable form, e.g. ``$2,400.00``."""
        cur = get_currency(self.currency)
        return f"{cur.symbol}{self.major:,.{cur.exponent}f}"
