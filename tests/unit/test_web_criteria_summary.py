from furnest.domain import Area, Criteria, Money
from furnest.web.criteria_summary import EMPTY_SUMMARY, summarize_criteria


def test_empty_criteria() -> None:
    assert summarize_criteria(Criteria(furnished_required=False)) == EMPTY_SUMMARY


def test_area_and_open_price_bounds() -> None:
    criteria = Criteria(
        furnished_required=False,
        min_beds=0,
        areas=(Area(name="Austin, TX", lat=30.27, lng=-97.74, radius=50),),
        price_max=Money(amount_minor=250050),
    )
    assert (
        summarize_criteria(criteria)
        == "Studio or larger, within 50 mi of Austin, TX, up to $2,500.50/mo"
    )
