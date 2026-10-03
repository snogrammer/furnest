"""Criteria: defaults, normalization, consistency rules and diffs."""

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from furnest.domain import (
    Area,
    Criteria,
    CriteriaChange,
    Currency,
    DistanceUnit,
    Money,
    PropertyType,
    diff_criteria,
    register_currency,
)


def criteria(**fields: object) -> Criteria:
    return Criteria.model_validate(fields)


def test_empty_criteria_is_valid_and_requires_furnished() -> None:
    empty = Criteria()
    assert empty.furnished_required is True
    assert empty.areas == ()
    assert empty.price_min is None
    assert empty.nice_to_have_amenities == {}


def test_full_profile_round_trips_through_json() -> None:
    profile = criteria(
        areas=[{"name": "Downtown", "lat": 40.0, "lng": -105.0, "radius": 5}],
        zip_codes=["00002", " 00001", "00001"],
        neighborhoods=["Old Town", "old town ", "Riverside"],
        price_min={"amount_minor": 150000},
        price_max={"amount_minor": 300000},
        property_types_allow=["condo", "apartment", "apartment"],
        property_types_deny=["room"],
        min_beds=1,
        min_baths=1.5,
        min_sqft=500,
        furnished_required=False,
        move_in_earliest=date(2026, 11, 1),
        move_in_latest=date(2026, 12, 1),
        lease_min_months=1,
        lease_max_months=6,
        must_have_amenities=["In_Unit_Laundry", "ac"],
        nice_to_have_amenities={"King_Bed": 3, "deck_patio": 1.5},
        keywords_allow=["Quiet"],
        keywords_deny=["NO PETS", "basement"],
        my_move_in_date=date(2026, 11, 15),
        my_lease_months=3,
    )
    assert profile.areas == (Area(name="Downtown", lat=40.0, lng=-105.0, radius=5),)
    assert profile.areas[0].unit is DistanceUnit.MILES
    assert profile.zip_codes == ("00001", "00002")
    assert profile.neighborhoods == ("old town", "riverside")
    assert profile.property_types_allow == (PropertyType.APARTMENT, PropertyType.CONDO)
    assert profile.must_have_amenities == ("ac", "in_unit_laundry")
    assert profile.nice_to_have_amenities == {"deck_patio": 1.5, "king_bed": 3}
    assert profile.keywords_deny == ("basement", "no pets")
    assert Criteria.model_validate_json(profile.model_dump_json()) == profile


def test_equal_profiles_serialize_identically() -> None:
    a = criteria(neighborhoods=["b", "a"], nice_to_have_amenities={"z": 1, "a": 2})
    b = criteria(neighborhoods=["A", "B"], nice_to_have_amenities={"a": 2, "z": 1})
    assert a.model_dump_json() == b.model_dump_json()


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"price_min": {"amount_minor": 2}, "price_max": {"amount_minor": 1}}, "price_min"),
        (
            {"property_types_allow": ["condo"], "property_types_deny": ["condo"]},
            "allowed and denied",
        ),
        ({"move_in_earliest": "2026-12-01", "move_in_latest": "2026-11-01"}, "move_in_earliest"),
        ({"lease_min_months": 6, "lease_max_months": 3}, "lease_min_months"),
        ({"must_have_amenities": ["ac"], "nice_to_have_amenities": {"AC": 1}}, "required and nice"),
        ({"keywords_allow": ["quiet"], "keywords_deny": ["Quiet"]}, "keywords both"),
        ({"nice_to_have_amenities": {"ac": 0}}, "greater than 0"),
        ({"min_baths": 1.2}, "multiple of 0.5"),
        ({"areas": [{"name": "x", "lat": 0, "lng": 0, "radius": 0}]}, "greater than 0"),
        ({"areas": [{"name": " ", "lat": 0, "lng": 0, "radius": 1}]}, "at least 1 character"),
        ({"property_types_allow": ["castle"]}, "enum"),
        ({"surprise": True}, "Extra inputs"),
    ],
)
def test_inconsistent_criteria_are_rejected(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        criteria(**fields)


def test_price_range_must_share_currency() -> None:
    register_currency(Currency(code="EUR", exponent=2, symbol="€"))
    with pytest.raises(ValidationError, match="same currency"):
        Criteria(price_min=Money(amount_minor=1), price_max=Money(amount_minor=2, currency="EUR"))


def test_diff_lists_changed_fields_in_order() -> None:
    old = criteria(min_beds=1, neighborhoods=["a"], furnished_required=True)
    new = criteria(min_beds=2, neighborhoods=["a"], furnished_required=False, keywords_deny=["x"])
    assert diff_criteria(old, new) == [
        CriteriaChange(field="min_beds", old=1, new=2),
        CriteriaChange(field="furnished_required", old=True, new=False),
        CriteriaChange(field="keywords_deny", old=[], new=["x"]),
    ]
    assert diff_criteria(new, new) == []
