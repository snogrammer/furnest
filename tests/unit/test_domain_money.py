"""Money: integer minor units, the currency registry and formatting."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from furnest.domain import USD, Currency, Money, get_currency, register_currency


def test_defaults_to_usd() -> None:
    assert Money(amount_minor=100).currency == "USD"
    assert get_currency("usd") is USD


@pytest.mark.parametrize(
    ("major", "minor"),
    [
        ("2400", 240000),
        ("1234.50", 123450),
        ("0.005", 1),
        ("0.004", 0),
        (Decimal("99.999"), 10000),
        (7, 700),
    ],
)
def test_from_major_rounds_half_up_to_cents(major: str | Decimal | int, minor: int) -> None:
    assert Money.from_major(major).amount_minor == minor


def test_from_major_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="not a number"):
        Money.from_major("abc")


def test_rejects_negative_amount_and_unknown_currency() -> None:
    with pytest.raises(ValidationError):
        Money(amount_minor=-1)
    with pytest.raises(ValidationError, match="unknown currency"):
        Money(amount_minor=1, currency="XYZ")


def test_currency_code_is_normalized_to_upper_case() -> None:
    assert Money(amount_minor=1, currency="usd").currency == "USD"


def test_major_and_format() -> None:
    money = Money(amount_minor=240050)
    assert money.major == Decimal("2400.50")
    assert money.format() == "$2,400.50"


def test_registered_currency_with_other_exponent() -> None:
    register_currency(Currency(code="JPY", exponent=0, symbol="¥"))
    money = Money.from_major("150000", "jpy")
    assert money.amount_minor == 150000
    assert money.format() == "¥150,000"
