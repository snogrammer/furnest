"""price_chart and top_severity: the SVG and banner level on the detail page."""

from datetime import UTC, datetime

from furnest.db.property_store import PropertyPricePoint
from furnest.domain import Money
from furnest.domain.price import Price
from furnest.web.enrichment_views import RiskItem, _difference, price_chart, top_severity


def point(major: int, day: int) -> PropertyPricePoint:
    return PropertyPricePoint(
        listing_id=day,
        source="test",
        price=Price(amount=Money.from_major(major)),
        observed_at=datetime(2026, 10, day, tzinfo=UTC),
    )


def test_no_chart_without_two_points() -> None:
    assert price_chart([]) is None
    assert price_chart([point(2000, 1)]) is None


def test_chart_is_an_accessible_svg_with_a_step_path() -> None:
    svg = price_chart([point(2000, 1), point(1900, 5), point(2100, 9)])

    assert svg is not None
    html = str(svg)
    assert html.startswith("<svg")
    assert 'role="img"' in html
    assert html.count("<circle") == 3
    assert "$2,100.00" in html and "$1,900.00" in html
    assert "2026-10-01" in html and "2026-10-09" in html
    assert " H" in html and " V" in html


def test_flat_price_draws_without_dividing_by_zero() -> None:
    svg = price_chart([point(2000, 1), point(2000, 2)])

    assert svg is not None
    assert "nan" not in str(svg).lower()


def test_top_severity() -> None:
    assert top_severity([]) is None
    risks = [RiskItem("a", "low", "x"), RiskItem("b", "high", "y"), RiskItem("c", "medium", "z")]
    assert top_severity(risks) == "high"


def test_difference_is_signed_and_formatted() -> None:
    assert _difference(-15000, "USD") == "-$150.00"
    assert _difference(2500, "USD") == "+$25.00"


def test_difference_unknown_for_bad_input() -> None:
    assert _difference(0, "USD") is None
    assert _difference(None, "USD") is None
    assert _difference(True, "USD") is None
    assert _difference("100", "USD") is None
    assert _difference(100, None) is None
    assert _difference(100, "XXZ") is None
