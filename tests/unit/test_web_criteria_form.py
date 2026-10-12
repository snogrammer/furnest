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
    Outline,
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
    for i, row in enumerate(state.outlines):
        pairs += [(f"outlines-{i}-name", row["name"]), (f"outlines-{i}-polygon", row["polygon"])]
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
    move_in=date(2026, 11, 1),
    move_in_flex_days=14,
    move_out=date(2027, 4, 1),
    move_out_flex_days=60,
    must_have_amenities=("ac",),
    nice_to_have_amenities={"pool": 2.5, "gym": 1},
    keywords_allow=("quiet",),
    keywords_deny=("shared bathroom",),
    unknown_fails=(FilterRule.PRICE, FilterRule.FURNISHED),
    price_fit_weight=3,
    extra_beds_weight=1,
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
    # A coordinate was typed, so the name isn't looked up: the rest is required.
    parsed = parse_form(submit(("areas-0-name", "Somewhere"), ("areas-0-lat", "1")))
    assert parsed.errors == {
        "areas-0-lng": "Field required",
        "areas-0-radius": "Field required",
    }


def test_area_name_alone_is_looked_up_with_the_default_radius() -> None:
    parsed = parse_form(submit(("areas-0-name", "springfield il"), ("areas-0-unit", "km")))
    assert parsed.errors == {}
    assert parsed.criteria is not None
    (area,) = parsed.criteria.areas
    assert (area.name, area.radius, area.unit) == ("Springfield, IL", 50, DistanceUnit.MILES)
    assert 39.5 < area.lat < 40.1
    assert -89.9 < area.lng < -89.3
    assert parsed.lookup is False


def test_looked_up_area_keeps_a_typed_radius() -> None:
    parsed = parse_form(
        submit(
            ("areas-0-name", "Springfield, IL"), ("areas-0-radius", "10"), ("areas-0-unit", "km")
        )
    )
    assert parsed.criteria is not None
    (area,) = parsed.criteria.areas
    assert (area.radius, area.unit) == (10, DistanceUnit.KILOMETERS)


def test_typed_coordinates_are_not_overwritten() -> None:
    parsed = parse_form(
        submit(
            ("areas-0-name", "Springfield, IL"),
            ("areas-0-lat", "39.7"),
            ("areas-0-lng", "-89.6"),
            ("areas-0-radius", "5"),
        )
    )
    assert parsed.criteria is not None
    assert (parsed.criteria.areas[0].lat, parsed.criteria.areas[0].lng) == (39.7, -89.6)


def test_unknown_city_is_an_error_on_the_name_and_kept_as_typed() -> None:
    parsed = parse_form(submit(("areas-0-name", "Nowhereville QQ"), ("areas-0-radius", "5")))
    assert parsed.criteria is None
    assert set(parsed.errors) == {"areas-0-name"}
    assert "no US city matches" in parsed.errors["areas-0-name"]
    assert parsed.state.areas[0]["name"] == "Nowhereville QQ"


def test_ambiguous_city_lists_the_options() -> None:
    parsed = parse_form(submit(("areas-0-name", "springfield")))
    assert set(parsed.errors) == {"areas-0-name"}
    assert "Springfield, IL" in parsed.errors["areas-0-name"]
    assert parsed.errors["areas-0-name"].startswith("pick one: ")


def test_lookup_button_re_resolves_its_row() -> None:
    # The submitted index (3) is the one the button carries, before renumbering.
    parsed = parse_form(
        submit(
            ("areas-3-name", "Springfield, IL"),
            ("areas-3-lat", "1"),
            ("areas-3-lng", "2"),
            ("areas-3-radius", "5"),
            ("lookup", "3"),
        )
    )
    assert parsed.lookup is True
    assert parsed.criteria is not None
    area = parsed.criteria.areas[0]
    assert 39.5 < area.lat < 40.1
    assert area.radius == 5


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


# --- Outlines (#214): carried through the form, never edited in it ------------

RING = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0))
WITH_OUTLINES = Criteria(
    outlines=(Outline(name="North", polygon=RING), Outline(name="South", polygon=RING))
)


def test_outlines_survive_a_save_of_the_form() -> None:
    state = state_from_criteria(WITH_OUTLINES)

    assert [row["corners"] for row in state.outlines] == ["3", "3"]
    parsed = parse_form(FormData(to_pairs(state)))
    assert parsed.errors == {}
    assert parsed.criteria is not None
    assert parsed.criteria.outlines == WITH_OUTLINES.outlines


def test_an_outline_can_be_removed() -> None:
    pairs = [*to_pairs(state_from_criteria(WITH_OUTLINES)), ("outlines-0-remove", "on")]

    parsed = parse_form(FormData(pairs))

    assert parsed.criteria is not None
    assert [o.name for o in parsed.criteria.outlines] == ["South"]


def test_an_unreadable_outline_is_reported_on_its_row() -> None:
    pairs = [
        *to_pairs(state_from_criteria(Criteria())),
        ("outlines-0-name", "Broken"),
        ("outlines-0-polygon", "[[0, 0"),
        ("outlines-1-name", "Open"),
        ("outlines-1-polygon", "[[0, 0], [1, 0], [1, 1], [0, 1]]"),
    ]

    parsed = parse_form(FormData(pairs))

    assert parsed.criteria is None
    assert "unreadable" in parsed.errors["outlines-0-name"]
    assert parsed.errors["outlines-1-name"] == "an outline must end where it starts"
    assert parsed.state.outlines[0]["corners"] == "?"
