"""criteria_form: flat editor fields to Criteria and back, and where errors land."""

from datetime import date

import pytest
from starlette.datastructures import FormData, UploadFile

from furnest.domain import (
    Area,
    Criteria,
    DistanceUnit,
    FilterRule,
    Money,
    OwnerType,
    PropertyType,
)
from furnest.web.criteria_form import GENERAL, FormState, parse_form, state_from_criteria

pytestmark = pytest.mark.unit


def submit(*pairs: tuple[str, str], **fields: str) -> FormData:
    return FormData([*pairs, *fields.items()])


def to_pairs(state: FormState) -> list[tuple[str, str | UploadFile]]:
    """What a browser would post for a rendered ``state``."""
    pairs: list[tuple[str, str | UploadFile]] = list(state.fields.items())
    pairs += [(group, value) for group, values in state.checked.items() for value in values]
    for i, row in enumerate(state.areas):
        pairs += [(f"areas-{i}-{key}", value) for key, value in row.items()]
    for i, (tag, weight) in enumerate(state.nice):
        pairs += [(f"nice-{i}-tag", tag), (f"nice-{i}-weight", weight)]
    return pairs


FULL = Criteria(
    areas=(Area(name="Downtown", lat=40.5, lng=-105.25, radius=3, unit=DistanceUnit.KILOMETERS),),
    zip_codes=("00001", "00002"),
    neighborhoods=("old town",),
    price_min=Money(amount_minor=150000),
    price_max=Money(amount_minor=240050),
    property_types_allow=(PropertyType.APARTMENT, PropertyType.TOWNHOUSE),
    property_types_deny=(PropertyType.ROOM,),
    owner_types_deny=(OwnerType.AGENT,),
    min_beds=0,
    min_baths=1.5,
    min_sqft=600,
    furnished_required=True,
    move_in_earliest=date(2026, 11, 1),
    move_in_latest=date(2026, 12, 1),
    lease_min_months=3,
    lease_max_months=12,
    must_have_amenities=("ac",),
    nice_to_have_amenities={"pool": 2.5, "gym": 1},
    keywords_allow=("quiet",),
    keywords_deny=("shared bathroom",),
    unknown_fails=(FilterRule.PRICE, FilterRule.FURNISHED),
    price_fit_weight=3,
    extra_beds_weight=1,
    my_move_in_date=date(2026, 11, 15),
    my_lease_months=6,
)


def test_full_profile_round_trips_through_the_form() -> None:
    parsed = parse_form(FormData(to_pairs(state_from_criteria(FULL))))
    assert parsed.errors == {}
    assert parsed.criteria == FULL


def test_empty_form_is_an_empty_profile_without_furnished() -> None:
    parsed = parse_form(submit())
    assert parsed.criteria == Criteria(furnished_required=False)


def test_default_state_ticks_furnished_and_uses_the_default_currency() -> None:
    state = state_from_criteria(Criteria())
    assert state.is_checked("furnished_required", "on")
    assert state.value("currency") == "USD"
    assert state.value("min_beds") == ""
    assert state.areas == []


def test_lists_split_on_commas_and_newlines() -> None:
    parsed = parse_form(submit(zip_codes="00001, 00002\n00003\n\n", keywords_deny="a,,b"))
    assert parsed.criteria is not None
    assert parsed.criteria.zip_codes == ("00001", "00002", "00003")
    assert parsed.criteria.keywords_deny == ("a", "b")


def test_prices_accept_dollar_signs_and_commas() -> None:
    parsed = parse_form(submit(price_min="$1,200", price_max="1999.99", currency="usd"))
    assert parsed.criteria is not None
    assert parsed.criteria.price_min == Money(amount_minor=120000)
    assert parsed.criteria.price_max == Money(amount_minor=199999)


def test_blank_and_removed_rows_are_dropped_and_renumbered() -> None:
    parsed = parse_form(
        submit(
            ("areas-0-name", ""),
            ("areas-0-unit", "mi"),
            ("areas-2-name", "Gone"),
            ("areas-2-remove", "on"),
            ("areas-5-name", "Kept"),
            ("areas-5-lat", "1"),
            ("areas-5-lng", "2"),
            ("areas-5-radius", "3"),
            ("areas-7-name", "Bad"),
            ("areas-7-lat", "100"),
            ("areas-7-lng", "0"),
            ("areas-7-radius", "1"),
        )
    )
    assert [row["name"] for row in parsed.state.areas] == ["Kept", "Bad"]
    assert set(parsed.errors) == {"areas-1-lat"}


def test_missing_area_fields_are_reported_per_row() -> None:
    parsed = parse_form(submit(("areas-0-name", "Somewhere")))
    assert parsed.errors == {
        "areas-0-lat": "Field required",
        "areas-0-lng": "Field required",
        "areas-0-radius": "Field required",
    }


@pytest.mark.parametrize(
    ("pairs", "errors"),
    [
        ([("nice-0-weight", "2")], {"nice-0-tag": "choose an amenity"}),
        ([("nice-0-tag", "pool")], {"nice-0-weight": "enter a weight"}),
        (
            [
                ("nice-0-tag", "pool"),
                ("nice-0-weight", "1"),
                ("nice-1-tag", "pool"),
                ("nice-1-weight", "2"),
            ],
            {"nice-1-tag": "listed twice"},
        ),
        (
            [
                ("nice-0-tag", "gym"),
                ("nice-0-weight", "1"),
                ("nice-1-tag", "pool"),
                ("nice-1-weight", "x"),
            ],
            {"nice-1-weight": "Input should be a valid number, unable to parse string as a number"},
        ),
    ],
)
def test_amenity_weight_errors(pairs: list[tuple[str, str]], errors: dict[str, str]) -> None:
    assert parse_form(submit(*pairs)).errors == errors


@pytest.mark.parametrize(
    ("fields", "errors"),
    [
        ({"price_max": "lots"}, {"price_max": "enter an amount such as 1500"}),
        ({"price_max": "-5"}, {"price_max": "enter an amount such as 1500"}),
        ({"currency": "XYZ", "price_max": "5"}, {"currency": "unknown currency 'XYZ'"}),
        (
            {"min_beds": "two"},
            {"min_beds": "Input should be a valid integer, unable to parse string as an integer"},
        ),
        (
            {"price_min": "3000", "price_max": "2000"},
            {GENERAL: "price_min must not exceed price_max"},
        ),
    ],
)
def test_field_errors(fields: dict[str, str], errors: dict[str, str]) -> None:
    parsed = parse_form(submit(**fields))
    assert parsed.criteria is None
    assert parsed.errors == errors


def test_checkbox_values_outside_the_choices_are_rejected() -> None:
    parsed = parse_form(submit(("property_types_allow", "castle")))
    assert set(parsed.errors) == {"property_types_allow"}


def test_failed_parse_keeps_what_was_typed() -> None:
    parsed = parse_form(submit(min_beds="two", keywords_allow="quiet\nsunny"))
    assert parsed.state.value("min_beds") == "two"
    assert parsed.state.value("keywords_allow") == "quiet\nsunny"
