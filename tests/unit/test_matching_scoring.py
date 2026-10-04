"""score_listing / match_listing: 0-100 bounds, ties, breakdown text and the JSON round trip."""

import random

import pytest

from furnest.domain import (
    Amenity,
    AmenityResult,
    Criteria,
    Currency,
    FilterRule,
    NormalizedListing,
    register_currency,
)
from furnest.matching import (
    BreakdownItem,
    MatchResult,
    RuleStatus,
    match_listing,
    score_listing,
)


def listing(**fields: object) -> NormalizedListing:
    return NormalizedListing.model_validate(
        {
            "source": "test",
            "source_id": "1",
            "url": "https://example.com/1",
            "title": "Furnished 1BR",
            **fields,
        }
    )


def criteria(**fields: object) -> Criteria:
    return Criteria.model_validate({"furnished_required": False, **fields})


def usd(dollars: int) -> dict[str, int]:
    return {"amount_minor": dollars * 100}


def monthly(dollars: int, dollars_max: int | None = None, period: str = "monthly") -> object:
    price: dict[str, object] = {"amount": usd(dollars), "period": period}
    if dollars_max is not None:
        price["amount_max"] = usd(dollars_max)
    return price


def only(
    listing_fields: dict[str, object], criteria_fields: dict[str, object]
) -> tuple[int, BreakdownItem]:
    score, breakdown = score_listing(listing(**listing_fields), criteria(**criteria_fields))
    assert len(breakdown) == 1
    return score, breakdown[0]


# --- bounds -----------------------------------------------------------------


def test_nothing_to_score_gives_100_with_no_breakdown() -> None:
    assert score_listing(listing(), criteria()) == (100, ())


def test_everything_earned_gives_100_and_nothing_gives_0() -> None:
    wants = criteria(
        nice_to_have_amenities={"ac": 2, "pool": 1},
        price_max=usd(2000),
        price_fit_weight=1,
        min_beds=1,
        extra_beds_weight=1,
    )
    best = listing(amenities=["ac", "pool"], price=monthly(0), beds=3)
    worst = listing(price=monthly(2500), beds=1)
    assert score_listing(best, wants)[0] == 100
    assert score_listing(worst, wants)[0] == 0


def test_score_stays_within_bounds_for_random_inputs() -> None:
    rng = random.Random(17)  # noqa: S311 - test data, not crypto
    tags = [tag.value for tag in Amenity]
    for _ in range(300):
        wanted = rng.sample(tags, rng.randint(0, 6))
        wants = criteria(
            nice_to_have_amenities={tag: rng.uniform(0.01, 100) for tag in wanted},
            price_min=usd(rng.randint(0, 1000)),
            price_max=usd(rng.randint(1000, 4000)),
            price_fit_weight=rng.choice([None, rng.uniform(0.01, 100)]),
            min_beds=rng.randint(0, 3),
            extra_beds_weight=rng.choice([None, rng.uniform(0.01, 100)]),
        )
        candidate = listing(
            amenities=rng.sample(tags, rng.randint(0, 6)),
            price=rng.choice([None, monthly(rng.randint(1, 5000))]),
            beds=rng.choice([None, rng.randint(0, 6)]),
        )
        score, breakdown = score_listing(candidate, wants)
        assert 0 <= score <= 100
        assert all(0 <= item.points <= item.max_points for item in breakdown)
        if breakdown:
            assert (
                abs(sum(item.points for item in breakdown) - score) <= 0.05 * len(breakdown) + 0.5
            )


# --- amenities --------------------------------------------------------------


def test_amenity_points_are_the_weight_share_of_100() -> None:
    score, breakdown = score_listing(
        listing(amenities=["in_unit_laundry"]),
        criteria(nice_to_have_amenities={"in_unit_laundry": 3, "ac": 1}),
    )
    assert score == 75
    assert breakdown == (
        BreakdownItem(label="AC", points=0.0, max_points=25.0, reason="not mentioned"),
        BreakdownItem(label="In-unit laundry", points=75.0, max_points=75.0, reason="stated"),
    )


def test_extractor_result_overrides_listing_tags() -> None:
    wants = criteria(nice_to_have_amenities={"pets_allowed": 1, "king_bed": 1})
    extracted = AmenityResult(
        present=frozenset({Amenity.KING_BED}), absent=frozenset({Amenity.PETS_ALLOWED})
    )
    score, breakdown = score_listing(
        listing(amenities=["pets_allowed"]), wants, amenities=extracted
    )
    assert score == 50
    assert [(item.label, item.reason) for item in breakdown] == [
        ("King bed", "stated"),
        ("Pets allowed", "ruled out"),
    ]


def test_unknown_tag_says_so() -> None:
    score, item = only({"amenities": ["hot_tub"]}, {"nice_to_have_amenities": {"hot_tub": 1}})
    # Adapter labels outside the vocabulary are not trusted as present.
    assert (score, item.label, item.reason) == (0, "Hot tub", "not a known amenity tag")


# --- price fit --------------------------------------------------------------

RANGE = {"price_min": usd(1000), "price_max": usd(2000), "price_fit_weight": 1}


@pytest.mark.parametrize(
    ("price", "score", "reason"),
    [
        (monthly(1000), 100, "$1,000.00/mo is at or below min $1,000.00"),
        (monthly(800), 100, "$800.00/mo is at or below min $1,000.00"),
        (monthly(2000), 0, "$2,000.00/mo is at or above max $2,000.00"),
        (monthly(2500), 0, "$2,500.00/mo is at or above max $2,000.00"),
        (
            monthly(1250),
            75,
            "$1,250.00/mo is 75% of the way from max $2,000.00 down to min $1,000.00",
        ),
        (
            monthly(1400, 1800),
            40,
            "$1,600.00/mo (midpoint of $1,400.00-$1,800.00) is 40% of the way"
            " from max $2,000.00 down to min $1,000.00",
        ),
        (None, 0, "price unknown"),
    ],
)
def test_price_fit(price: object, score: int, reason: str) -> None:
    got, item = only({"price": price}, RANGE)
    assert (got, item.label, item.reason) == (score, "Price fit", reason)


def test_price_fit_with_only_a_max_measures_from_zero() -> None:
    score, item = only({"price": monthly(1500)}, {"price_max": usd(2000), "price_fit_weight": 1})
    assert (score, item.reason) == (25, "$1,500.00/mo is 25% below max $2,000.00")


def test_price_fit_converts_nightly_prices() -> None:
    score, item = only({"price": monthly(50, period="nightly")}, RANGE)
    assert (score, item.reason) == (
        48,
        "$1,520.83/mo (quoted nightly) is 48% of the way from max $2,000.00 down to min $1,000.00",
    )


def test_price_fit_in_another_currency_earns_nothing() -> None:
    register_currency(Currency(code="EUR", exponent=2, symbol="€"))
    price = {"amount": {"amount_minor": 100000, "currency": "EUR"}}
    score, item = only({"price": price}, RANGE)
    assert (score, item.reason) == (0, "price in EUR, criteria in USD")


def test_fit_components_need_their_bound() -> None:
    assert score_listing(listing(price=monthly(10)), criteria(price_fit_weight=1)) == (100, ())
    assert score_listing(listing(beds=5), criteria(extra_beds_weight=1)) == (100, ())


# --- extra beds -------------------------------------------------------------


@pytest.mark.parametrize(
    ("beds", "score", "reason"),
    [
        (2, 0, "2 beds, none above minimum 2"),
        (3, 50, "3 beds, 1 above minimum 2"),
        (5, 100, "5 beds, 3 above minimum 2"),
        (1, 0, "1 bed is below minimum 2"),
        (None, 0, "beds unknown"),
    ],
)
def test_extra_beds(beds: int | None, score: int, reason: str) -> None:
    got, item = only({"beds": beds}, {"min_beds": 2, "extra_beds_weight": 1})
    assert (got, item.label, item.reason) == (score, "Extra beds", reason)


def test_studio_is_named() -> None:
    _, item = only({"beds": 0}, {"min_beds": 0, "extra_beds_weight": 1})
    assert item.reason == "studio, none above minimum 0"


# --- ties and rounding ------------------------------------------------------


def test_only_the_weight_ratio_matters() -> None:
    small = criteria(nice_to_have_amenities={"ac": 1, "pool": 3})
    large = criteria(nice_to_have_amenities={"ac": 10, "pool": 30})
    candidate = listing(amenities=["ac"])
    assert score_listing(candidate, small) == score_listing(candidate, large)


def test_equal_listings_tie() -> None:
    wants = criteria(nice_to_have_amenities={"ac": 1, "pool": 1})
    assert (
        score_listing(listing(amenities=["ac"]), wants)[0]
        == score_listing(listing(source_id="2", amenities=["pool"]), wants)[0]
        == 50
    )


def test_half_points_round_up() -> None:
    # 5 of 8 weight = 62.5 → 63; 1 of 8 = 12.5 → 13.
    wants = criteria(nice_to_have_amenities={"ac": 5, "pool": 1, "gym": 2})
    assert score_listing(listing(amenities=["ac"]), wants)[0] == 63
    assert score_listing(listing(amenities=["pool"]), wants)[0] == 13


# --- match_listing ----------------------------------------------------------


def test_match_result_combines_filters_and_score() -> None:
    wants = criteria(min_beds=2, extra_beds_weight=1, nice_to_have_amenities={"ac": 1})
    result = match_listing(listing(beds=3, amenities=["ac"]), wants, criteria_version=4)
    assert result.passed
    assert result.score == 75
    assert result.criteria_version == 4
    assert result.failed_rules == ()
    assert [rule.rule for rule in result.rules] == [FilterRule.MIN_BEDS]


def test_failed_listing_is_still_scored() -> None:
    wants = criteria(min_beds=2, nice_to_have_amenities={"ac": 1})
    result = match_listing(listing(beds=1, amenities=["ac"]), wants)
    assert not result.passed
    assert result.score == 100
    assert [(rule.rule, rule.status) for rule in result.failed_rules] == [
        (FilterRule.MIN_BEDS, RuleStatus.FAIL)
    ]


def test_match_result_round_trips_through_json() -> None:
    wants = criteria(
        furnished_required=True,
        unknown_fails=["furnished"],
        price_max=usd(2000),
        price_fit_weight=2,
        nice_to_have_amenities={"pool": 1},
    )
    result = match_listing(listing(price=monthly(1500)), wants, criteria_version=7)
    stored = result.model_dump(mode="json")
    assert stored["criteria_version"] == 7
    assert [(rule["rule"], rule["status"], rule["blocking"]) for rule in stored["rules"]] == [
        ("price", "pass", False),
        ("furnished", "unknown", True),
    ]
    assert stored["breakdown"][0]["label"] == "Pool"
    assert MatchResult.model_validate(stored) == result
    assert MatchResult.model_validate_json(result.model_dump_json()) == result


def test_score_is_bounded_in_the_model() -> None:
    with pytest.raises(ValueError, match="less than or equal to 100"):
        MatchResult(passed=True, score=101)
