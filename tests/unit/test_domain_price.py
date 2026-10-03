"""Rent periods, monthly normalization and free-text price parsing."""

from fractions import Fraction

import pytest
from pydantic import ValidationError

from furnest.domain import (
    MONTHLY_FACTOR,
    Currency,
    Money,
    Price,
    RentPeriod,
    parse_period,
    parse_price,
    register_currency,
    to_monthly,
)


def usd(cents: int) -> Money:
    return Money(amount_minor=cents)


def test_documented_conversion_factors() -> None:
    assert MONTHLY_FACTOR[RentPeriod.NIGHTLY] == Fraction(365, 12)
    assert MONTHLY_FACTOR[RentPeriod.WEEKLY] == Fraction(52, 12)
    assert MONTHLY_FACTOR[RentPeriod.MONTHLY] == 1


@pytest.mark.parametrize(
    ("cents", "period", "monthly"),
    [
        (240000, RentPeriod.MONTHLY, 240000),
        (60000, RentPeriod.WEEKLY, 260000),  # 600 * 52 / 12
        (50000, RentPeriod.WEEKLY, 216667),  # 2166.666… rounds up
        (9500, RentPeriod.NIGHTLY, 288958),  # 95 * 365 / 12 = 2889.583…
        (12000, RentPeriod.NIGHTLY, 365000),
        (3, RentPeriod.WEEKLY, 13),  # 13.0
        (6, RentPeriod.NIGHTLY, 183),  # 182.5 → half rounds up
        (0, RentPeriod.NIGHTLY, 0),
    ],
)
def test_to_monthly(cents: int, period: RentPeriod, monthly: int) -> None:
    assert to_monthly(usd(cents), period) == usd(monthly)


def test_to_monthly_keeps_currency() -> None:
    register_currency(Currency(code="EUR", exponent=2, symbol="€"))
    assert to_monthly(Money(amount_minor=100, currency="EUR"), RentPeriod.WEEKLY).currency == "EUR"


@pytest.mark.parametrize(
    ("raw", "period"),
    [
        ("/mo", RentPeriod.MONTHLY),
        ("per month", RentPeriod.MONTHLY),
        ("Months", RentPeriod.MONTHLY),
        ("wk", RentPeriod.WEEKLY),
        ("weekly", RentPeriod.WEEKLY),
        ("per night", RentPeriod.NIGHTLY),
        ("Nights", RentPeriod.NIGHTLY),
        ("daily", RentPeriod.NIGHTLY),
        ("fortnight", None),
        ("", None),
    ],
)
def test_parse_period(raw: str, period: RentPeriod | None) -> None:
    assert parse_period(raw) is period


def test_single_price_monthly_max_equals_monthly() -> None:
    price = Price(amount=usd(60000), period=RentPeriod.WEEKLY)
    assert price.monthly == price.monthly_max == usd(260000)
    assert price.currency == "USD"


def test_range_normalizes_both_ends() -> None:
    price = Price(amount=usd(60000), amount_max=usd(90000), period=RentPeriod.WEEKLY)
    assert price.monthly == usd(260000)
    assert price.monthly_max == usd(390000)


def test_range_must_not_be_inverted() -> None:
    with pytest.raises(ValidationError, match="amount_max"):
        Price(amount=usd(2), amount_max=usd(1))


def test_range_must_share_currency() -> None:
    register_currency(Currency(code="EUR", exponent=2, symbol="€"))
    with pytest.raises(ValidationError, match="same currency"):
        Price(amount=usd(1), amount_max=Money(amount_minor=2, currency="EUR"))


@pytest.mark.parametrize(
    ("text", "cents", "max_cents", "period"),
    [
        ("$2,400/mo", 240000, None, RentPeriod.MONTHLY),
        ("$2,400", 240000, None, RentPeriod.MONTHLY),
        ("2400.50 per month", 240050, None, RentPeriod.MONTHLY),
        ("$1,200 \u2013 $1,500 per month", 120000, 150000, RentPeriod.MONTHLY),
        ("$1,200-1,500/mo", 120000, 150000, RentPeriod.MONTHLY),
        ("$1,200 \u2014 $1,500 monthly", 120000, 150000, RentPeriod.MONTHLY),
        ("$1,200 to $1,500 monthly", 120000, 150000, RentPeriod.MONTHLY),
        ("$1,500 - $1,200", 120000, 150000, RentPeriod.MONTHLY),
        ("$1,500 - $1,500/mo", 150000, None, RentPeriod.MONTHLY),
        ("$95/night", 9500, None, RentPeriod.NIGHTLY),
        ("From $95 a night", 9500, None, RentPeriod.NIGHTLY),
        ("1.5k/wk", 150000, None, RentPeriod.WEEKLY),
        ("1.5-2k per week", 150000, 200000, RentPeriod.WEEKLY),
        ("$600 weekly", 60000, None, RentPeriod.WEEKLY),
        ("2 bed / 1 bath, $2,000 per month", 200000, None, RentPeriod.MONTHLY),
    ],
)
def test_parse_price(text: str, cents: int, max_cents: int | None, period: RentPeriod) -> None:
    price = parse_price(text)
    assert price is not None
    assert price.amount == usd(cents)
    assert price.amount_max == (usd(max_cents) if max_cents is not None else None)
    assert price.period is period


@pytest.mark.parametrize(
    "text", [None, "", "   ", "Contact for price", "Call for pricing", "$0/mo"]
)
def test_missing_price_is_none(text: str | None) -> None:
    assert parse_price(text) is None


def test_parse_price_default_period_and_currency() -> None:
    register_currency(Currency(code="EUR", exponent=2, symbol="€"))
    price = parse_price("95", default_period=RentPeriod.NIGHTLY, currency="EUR")
    assert price == Price(
        amount=Money(amount_minor=9500, currency="EUR"), period=RentPeriod.NIGHTLY
    )
    assert price.monthly == Money(amount_minor=288958, currency="EUR")
