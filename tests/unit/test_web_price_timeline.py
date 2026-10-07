"""price_timeline: a property's price changes across its listings, for the detail page."""

from datetime import UTC, datetime

from furnest.db.property_store import PropertyPricePoint
from furnest.domain import Money
from furnest.domain.money import Currency, register_currency
from furnest.domain.periods import RentPeriod
from furnest.domain.price import Price
from furnest.web.property_views import price_timeline


def point(
    major: int, day: int, *, currency: str = "USD", period: str = "monthly"
) -> PropertyPricePoint:
    return PropertyPricePoint(
        listing_id=day,
        source="test",
        price=Price(amount=Money.from_major(major, currency), period=RentPeriod(period)),
        observed_at=datetime(2026, 10, day, tzinfo=UTC),
    )


def test_one_price_or_none_is_no_timeline() -> None:
    assert price_timeline([]) is None
    assert price_timeline([point(2000, 1)]) is None


def test_steps_are_newest_first_with_their_change() -> None:
    timeline = price_timeline([point(2000, 1), point(1900, 2), point(1900, 3), point(2100, 4)])

    assert timeline is not None
    assert [step.point.observed_at.day for step in timeline.steps] == [4, 3, 2, 1]
    assert [step.change for step in timeline.steps] == ["+$200.00", None, "-$100.00", None]
    assert [step.change_minor for step in timeline.steps] == [20000, 0, -10000, None]
    assert timeline.first == Money.from_major(2000)
    assert timeline.lowest == Money.from_major(1900)
    assert timeline.current == Money.from_major(2100)


def test_changes_compare_monthly_prices() -> None:
    timeline = price_timeline([point(500, 1, period="weekly"), point(2000, 2)])

    assert timeline is not None
    weekly_monthly = Price(amount=Money.from_major(500), period=RentPeriod.WEEKLY).monthly
    assert timeline.steps[0].change_minor == 200000 - weekly_monthly.amount_minor


def test_a_price_in_another_currency_has_no_change() -> None:
    register_currency(Currency(code="EUR", symbol="€", exponent=2))
    timeline = price_timeline([point(1800, 1, currency="EUR"), point(2000, 2)])

    assert timeline is not None
    assert timeline.steps[0].change_minor is None
    assert timeline.lowest == Money.from_major(2000)
