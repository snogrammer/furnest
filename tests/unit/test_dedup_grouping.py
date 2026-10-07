import json
from pathlib import Path
from typing import Any

import pytest

from furnest.config import GroupingSettings
from furnest.dedup.grouping import (
    AddressKey,
    GroupingThresholds,
    ListingFacts,
    distance_m,
    score_pair,
)
from furnest.domain import ParsedAddress

pytestmark = pytest.mark.unit

PAIRS = json.loads((Path(__file__).parents[1] / "fixtures/grouping/pairs.json").read_text())


def facts(listing_id: int, data: dict[str, Any]) -> ListingFacts:
    address = data.get("address")
    return ListingFacts(
        listing_id=listing_id,
        address=None if address is None else AddressKey.stored(*address),
        lat=data.get("lat"),
        lng=data.get("lng"),
        beds=data.get("beds"),
        phashes=tuple(data.get("phashes", ())),
        title=data.get("title"),
        description=data.get("description"),
    )


@pytest.mark.parametrize("case", PAIRS, ids=[case["name"] for case in PAIRS])
def test_duplicate_fixtures(case: dict[str, Any]) -> None:
    a, b = facts(1, case["a"]), facts(2, case["b"])
    result = score_pair(a, b, GroupingThresholds())
    assert result.matched is case["matched"], result
    assert result.veto == case.get("veto")
    # The decision doesn't depend on the order of the pair.
    assert score_pair(b, a, GroupingThresholds()).matched is case["matched"]


def test_address_key_needs_a_house_number_and_street() -> None:
    assert AddressKey.of(ParsedAddress(city="springfield", region="IL")) is None
    assert AddressKey.of(ParsedAddress(street="main st", city="springfield")) is None
    key = AddressKey.of(
        ParsedAddress(
            house_number="123",
            street="n main st",
            unit="#4b",
            city="springfield",
            region="IL",
            postal_code="62701",
            parsed=True,
        )
    )
    assert key == AddressKey(
        canonical="123 n main st #4b, springfield, il 62701, us",
        street="123 n main st, 62701",
        unit="#4b",
    )


def test_address_key_without_a_place_has_no_street_key() -> None:
    key = AddressKey.of(ParsedAddress(house_number="1", street="main st"))
    assert key is not None
    assert key.street is None


def test_po_box_is_a_key() -> None:
    key = AddressKey.of(ParsedAddress(po_box="12", city="springfield"))
    assert key is not None
    assert key.canonical.startswith("po box 12")


def test_stored_key_reads_the_unit_back() -> None:
    assert AddressKey.stored(None, None) is None
    key = AddressKey.stored("123 n main st #4b, springfield, us", "123 n main st, springfield")
    assert key is not None
    assert key.unit == "#4b"
    plain = AddressKey.stored("123 n main st, springfield, us", None)
    assert plain is not None
    assert plain.unit is None


def test_missing_signals_are_left_out_of_the_score() -> None:
    a = ListingFacts(1, phashes=("0000000000000000",), title="Furnished loft")
    b = ListingFacts(2, phashes=("0000000000000000",), title="Furnished loft")
    result = score_pair(a, b, GroupingThresholds())
    assert set(result.signals) == {"photos", "title"}
    assert result.score == 1.0
    assert result.matched


def test_too_few_signals_never_match() -> None:
    a = ListingFacts(1, phashes=("0000000000000000",))
    b = ListingFacts(2, phashes=("0000000000000000",))
    result = score_pair(a, b, GroupingThresholds())
    assert result.signals == {"photos": 1.0}
    assert not result.matched
    assert score_pair(a, b, GroupingThresholds(min_signals=1)).matched


def test_a_zero_weight_turns_a_signal_off() -> None:
    a = ListingFacts(1, phashes=("0000000000000000",), title="Loft")
    b = ListingFacts(2, phashes=("0000000000000000",), title="Loft")
    weights = {"address": 3.0, "distance": 1.0, "photos": 3.0, "title": 0.0, "description": 1.0}
    result = score_pair(a, b, GroupingThresholds(weights=weights))
    assert set(result.signals) == {"photos"}
    assert not result.matched


def test_the_threshold_is_inclusive() -> None:
    a = ListingFacts(1, phashes=("0000000000000000",), title="Furnished loft downtown")
    b = ListingFacts(2, phashes=("0000000000000000",), title="Studio by the lake")
    score = score_pair(a, b, GroupingThresholds()).score
    assert score_pair(a, b, GroupingThresholds(match_threshold=score)).matched
    assert not score_pair(a, b, GroupingThresholds(match_threshold=score + 0.001)).matched


def test_nothing_comparable_scores_zero() -> None:
    result = score_pair(ListingFacts(1), ListingFacts(2), GroupingThresholds())
    assert result.score == 0.0
    assert result.signals == {}
    assert not result.matched


def test_distance_signal_falls_off_with_distance() -> None:
    a = ListingFacts(1, lat=39.7, lng=-89.7)
    b = ListingFacts(2, lat=39.7003, lng=-89.7)
    meters = distance_m(a, b)
    assert meters is not None
    assert 30 < meters < 36
    result = score_pair(a, b, GroupingThresholds(min_signals=1))
    assert result.signals["distance"] == pytest.approx(1 - meters / 75)
    assert distance_m(a, ListingFacts(3)) is None


def test_payload_rounds_the_breakdown() -> None:
    a = ListingFacts(1, phashes=("0000000000000000",), title="Furnished loft downtown")
    b = ListingFacts(2, phashes=("0000000000000000",), title="Furnished loft")
    payload = score_pair(a, b, GroupingThresholds()).as_payload()
    assert set(payload) == {"score", "signals"}
    assert payload["signals"] == {"photos": 1.0, "title": 1.0}


def test_thresholds_from_settings() -> None:
    settings = GroupingSettings(max_distance_m=50, match_threshold=0.9, weight_title=0)
    thresholds = GroupingThresholds.from_settings(settings)
    assert thresholds.max_distance_m == 50
    assert thresholds.match_threshold == 0.9
    assert thresholds.weights["title"] == 0
    assert thresholds.weights["photos"] == 3.0
