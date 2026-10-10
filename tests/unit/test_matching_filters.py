"""evaluate_hard_filters: per-rule pass/fail/unknown with reasons, and the combined verdict."""

from collections.abc import Mapping
from datetime import date

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
from furnest.matching import HardFilterResult, RuleResult, RuleStatus, evaluate_hard_filters

PASS, FAIL, UNKNOWN = RuleStatus.PASS, RuleStatus.FAIL, RuleStatus.UNKNOWN


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
    # Turn the default furnished rule off so each test sees only the rule it configures.
    return Criteria.model_validate({"furnished_required": False, **fields})


def usd(dollars: int) -> dict[str, int]:
    return {"amount_minor": dollars * 100}


def only(result: HardFilterResult) -> RuleResult:
    assert len(result.rules) == 1
    return result.rules[0]


def check(
    listing_fields: Mapping[str, object],
    criteria_fields: Mapping[str, object],
    amenities: AmenityResult | None = None,
) -> RuleResult:
    return only(
        evaluate_hard_filters(
            listing(**listing_fields), criteria(**criteria_fields), amenities=amenities
        )
    )


# --- location ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "status", "reason"),
    [
        ({"address": {"postal_code": "00001"}}, PASS, "ZIP 00001 is in the list"),
        ({"address": {"postal_code": "00009"}}, FAIL, "ZIP 00009 is not in the list"),
        ({}, UNKNOWN, "location unknown: no ZIP code"),
    ],
)
def test_location(fields: dict[str, object], status: RuleStatus, reason: str) -> None:
    result = check(fields, {"zip_codes": ["00001"]})
    assert (result.rule, result.status, result.reason) == (FilterRule.LOCATION, status, reason)


def test_location_joins_every_reason() -> None:
    result = check(
        {"address": {"postal_code": "00009"}},
        {"zip_codes": ["00001"], "neighborhoods": ["old town"]},
    )
    assert result.status is UNKNOWN
    assert result.reason == "ZIP 00009 is not in the list; location unknown: no neighborhood"


# --- price ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "status", "reason"),
    [
        (
            {"amount": usd(1500)},
            PASS,
            "$1,500.00/mo is within min $1,000.00, max $2,000.00",
        ),
        ({"amount": usd(2500)}, FAIL, "$2,500.00/mo is above max $2,000.00"),
        ({"amount": usd(900)}, FAIL, "$900.00/mo is below min $1,000.00"),
        # A quoted range passes when it overlaps the wanted range.
        (
            {"amount": usd(1800), "amount_max": usd(2400)},
            PASS,
            "$1,800.00-$2,400.00/mo is within min $1,000.00, max $2,000.00",
        ),
        (
            {"amount": usd(500), "amount_max": usd(900)},
            FAIL,
            "$500.00-$900.00/mo is below min $1,000.00",
        ),
        # $60/night is $1,825/mo (x 365/12).
        (
            {"amount": usd(60), "period": "nightly"},
            PASS,
            "$1,825.00/mo (quoted nightly) is within min $1,000.00, max $2,000.00",
        ),
        (None, UNKNOWN, "price unknown"),
    ],
)
def test_price(price: dict[str, object] | None, status: RuleStatus, reason: str) -> None:
    result = check({"price": price}, {"price_min": usd(1000), "price_max": usd(2000)})
    assert (result.rule, result.status, result.reason) == (FilterRule.PRICE, status, reason)


def test_price_with_one_bound() -> None:
    result = check({"price": {"amount": usd(1500)}}, {"price_max": usd(2000)})
    assert (result.status, result.reason) == (PASS, "$1,500.00/mo is within max $2,000.00")
    result = check({"price": {"amount": usd(1500)}}, {"price_min": usd(2000)})
    assert (result.status, result.reason) == (FAIL, "$1,500.00/mo is below min $2,000.00")


def test_price_in_another_currency_is_unknown() -> None:
    register_currency(Currency(code="EUR", exponent=2, symbol="€"))
    result = check(
        {"price": {"amount": {"amount_minor": 100000, "currency": "EUR"}}},
        {"price_max": usd(2000)},
    )
    assert (result.status, result.reason) == (UNKNOWN, "price in EUR, criteria in USD")


# --- property type ----------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "rules", "status", "reason"),
    [
        ("condo", {"property_types_deny": ["condo"]}, FAIL, "condo is denied"),
        ("apartment", {"property_types_deny": ["condo"]}, PASS, "apartment is not denied"),
        ("apartment", {"property_types_allow": ["apartment"]}, PASS, "apartment is allowed"),
        (
            "condo",
            {"property_types_allow": ["apartment"]},
            FAIL,
            "condo is not in the allowed types",
        ),
        (
            "yurt",
            {"property_types_allow": ["apartment"]},
            UNKNOWN,
            "property type unrecognized",
        ),
        ("yurt", {"property_types_deny": ["condo"]}, UNKNOWN, "property type unrecognized"),
        ("yurt", {"property_types_deny": ["other"]}, FAIL, "other is denied"),
        ("yurt", {"property_types_allow": ["other"]}, PASS, "other is allowed"),
    ],
)
def test_property_type(
    kind: str, rules: dict[str, object], status: RuleStatus, reason: str
) -> None:
    result = check({"property_type": kind}, rules)
    assert (result.rule, result.status, result.reason) == (FilterRule.PROPERTY_TYPE, status, reason)


# --- owner type -------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "deny", "status", "reason"),
    [
        ("agent", ["agent"], FAIL, "agent is denied"),
        ("property_manager", ["agent", "property_manager"], FAIL, "property_manager is denied"),
        ("individual", ["agent"], PASS, "individual is not denied"),
        ("unknown", ["agent"], UNKNOWN, "owner type unknown"),
        ("unknown", ["unknown"], FAIL, "unknown is denied"),
    ],
)
def test_owner_type(kind: str, deny: list[str], status: RuleStatus, reason: str) -> None:
    result = check({"owner_type": kind}, {"owner_types_deny": deny})
    assert (result.rule, result.status, result.reason) == (FilterRule.OWNER_TYPE, status, reason)


def test_an_unknown_owner_type_blocks_only_when_asked() -> None:
    fields = {"owner_types_deny": ["agent"], "unknown_fails": ["owner_type"]}
    lenient = evaluate_hard_filters(
        listing(owner_type="unknown"), criteria(owner_types_deny=["agent"])
    )
    strict = evaluate_hard_filters(listing(owner_type="unknown"), criteria(**fields))
    assert (lenient.passed, strict.passed) == (True, False)


# --- size -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "rules", "rule", "status", "reason"),
    [
        ({"beds": 2}, {"min_beds": 1}, FilterRule.MIN_BEDS, PASS, "2 beds meets minimum 1"),
        ({"beds": 1}, {"min_beds": 1}, FilterRule.MIN_BEDS, PASS, "1 bed meets minimum 1"),
        ({"beds": 0}, {"min_beds": 1}, FilterRule.MIN_BEDS, FAIL, "studio is below minimum 1"),
        ({}, {"min_beds": 1}, FilterRule.MIN_BEDS, UNKNOWN, "beds unknown"),
        ({"baths": 1.5}, {"min_baths": 1}, FilterRule.MIN_BATHS, PASS, "1.5 baths meets minimum 1"),
        (
            {"baths": 1},
            {"min_baths": 1.5},
            FilterRule.MIN_BATHS,
            FAIL,
            "1 bath is below minimum 1.5",
        ),
        ({}, {"min_baths": 1}, FilterRule.MIN_BATHS, UNKNOWN, "baths unknown"),
        ({"sqft": 700}, {"min_sqft": 600}, FilterRule.MIN_SQFT, PASS, "700 sqft meets minimum 600"),
        (
            {"sqft": 500},
            {"min_sqft": 600},
            FilterRule.MIN_SQFT,
            FAIL,
            "500 sqft is below minimum 600",
        ),
        ({}, {"min_sqft": 600}, FilterRule.MIN_SQFT, UNKNOWN, "square footage unknown"),
    ],
)
def test_minimums(
    fields: dict[str, object],
    rules: dict[str, object],
    rule: FilterRule,
    status: RuleStatus,
    reason: str,
) -> None:
    result = check(fields, rules)
    assert (result.rule, result.status, result.reason) == (rule, status, reason)


# --- furnished --------------------------------------------------------------


@pytest.mark.parametrize(
    ("furnished", "status", "reason"),
    [
        (True, PASS, "furnished"),
        (False, FAIL, "unfurnished"),
        (None, UNKNOWN, "furnished status unknown"),
    ],
)
def test_furnished(furnished: bool | None, status: RuleStatus, reason: str) -> None:
    result = check({"furnished": furnished}, {"furnished_required": True})
    assert (result.rule, result.status, result.reason) == (FilterRule.FURNISHED, status, reason)


# --- move-in ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("available", "status", "reason"),
    [
        (date(2026, 11, 30), PASS, "available from 2026-11-30, within the move-in window"),
        # Free before the window opens means free during it.
        (date(2026, 10, 1), PASS, "available from 2026-10-01, before the move-in window opens"),
        (date(2026, 12, 8), PASS, "available from 2026-12-08, within the move-in window"),
        (
            date(2026, 12, 15),
            FAIL,
            "available from 2026-12-15, after latest move-in 2026-12-08",
        ),
        (None, UNKNOWN, "availability date unknown"),
    ],
)
def test_move_in(available: date | None, status: RuleStatus, reason: str) -> None:
    result = check(
        {"available_from": available},
        {"move_in": date(2026, 12, 1), "move_in_flex_days": 7},
    )
    assert (result.rule, result.status, result.reason) == (FilterRule.MOVE_IN, status, reason)


# --- lease ---

# Stay of Nov 1 to about Mar 1, give or take 40 days: lease_months_range() is (2, 6).
_STAY_2_TO_6 = {
    "move_in": date(2026, 11, 1),
    "move_in_flex_days": 0,
    "move_out": date(2027, 3, 1),
    "move_out_flex_days": 40,
}


@pytest.mark.parametrize(
    ("lease", "status", "reason"),
    [
        ({"min_lease_months": 1, "max_lease_months": 12}, PASS, "lease terms fit"),
        ({"min_lease_months": 3, "max_lease_months": 3}, PASS, "lease terms fit"),
        ({"min_lease_months": 12}, FAIL, "minimum lease 12 months exceeds maximum 6"),
        ({"max_lease_months": 1}, FAIL, "maximum lease 1 month is below minimum 2"),
        ({"min_lease_months": 1}, UNKNOWN, "lease terms unknown"),
        ({}, UNKNOWN, "lease terms unknown"),
    ],
)
def test_lease(lease: dict[str, object], status: RuleStatus, reason: str) -> None:
    evaluated = evaluate_hard_filters(listing(**lease), criteria(**_STAY_2_TO_6))
    (result,) = (r for r in evaluated.rules if r.rule is FilterRule.LEASE)
    assert (result.rule, result.status, result.reason) == (FilterRule.LEASE, status, reason)


def test_lease_needs_both_a_move_in_and_a_move_out() -> None:
    evaluated = evaluate_hard_filters(listing(), criteria(move_in=date(2026, 11, 1)))
    assert [r.rule for r in evaluated.rules] == [FilterRule.MOVE_IN]


def test_listing_available_days_after_target_with_six_month_minimum_passes() -> None:
    target = {
        "move_in": date(2026, 11, 8),
        "move_in_flex_days": 7,
        "move_out": date(2027, 5, 8),
        "move_out_flex_days": 90,
    }
    fields = {"available_from": date(2026, 11, 11), "min_lease_months": 6}
    result = evaluate_hard_filters(listing(**fields), criteria(**target))
    assert result.passed


# --- amenities --------------------------------------------------------------


def test_must_have_amenities_from_extractor_result() -> None:
    wanted = {"must_have_amenities": ["ac", "pets_allowed"]}
    has_all = AmenityResult(present=frozenset({Amenity.AC, Amenity.PETS_ALLOWED}))
    result = check({}, wanted, amenities=has_all)
    assert (result.rule, result.status, result.reason) == (
        FilterRule.MUST_HAVE_AMENITIES,
        PASS,
        "has ac, pets_allowed",
    )

    no_pets = AmenityResult(
        present=frozenset({Amenity.AC}), absent=frozenset({Amenity.PETS_ALLOWED})
    )
    result = check({}, wanted, amenities=no_pets)
    assert (result.status, result.reason) == (FAIL, "ruled out: pets_allowed")

    silent = AmenityResult(present=frozenset({Amenity.AC}))
    result = check({}, wanted, amenities=silent)
    assert (result.status, result.reason) == (UNKNOWN, "not stated: pets_allowed")


def test_must_have_amenities_fall_back_to_listing_tags() -> None:
    result = check({"amenities": ["AC", "hot_tub"]}, {"must_have_amenities": ["ac"]})
    assert result.status is PASS
    # An unknown tag can never be confirmed, and the reason says why.
    result = check({"amenities": ["hot_tub"]}, {"must_have_amenities": ["hot_tub"]})
    assert (result.status, result.reason) == (
        UNKNOWN,
        "not stated: hot_tub (not a known amenity tag)",
    )


# --- keywords ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "description", "status", "reason"),
    [
        ("Cozy 1BR", "Finished basement unit", FAIL, "mentions denied basement"),
        ("BASEMENT studio", "", FAIL, "mentions denied basement"),
        ("Cozy 1BR", "No  Pets please", FAIL, "mentions denied no pets"),
        ("Cozy 1BR", "New carpet throughout", PASS, "no denied keywords"),
    ],
)
def test_keywords_deny(title: str, description: str, status: RuleStatus, reason: str) -> None:
    result = check(
        {"title": title, "description": description},
        {"keywords_deny": ["basement", "no pets", "pet"]},
    )
    assert (result.rule, result.status, result.reason) == (FilterRule.KEYWORDS_DENY, status, reason)


def test_keywords_allow() -> None:
    rules = {"keywords_allow": ["quiet", "walkable"]}
    result = check({"title": "Quiet street", "description": "Very walkable."}, rules)
    assert (result.rule, result.status, result.reason) == (
        FilterRule.KEYWORDS_ALLOW,
        PASS,
        "mentions quiet, walkable",
    )
    result = check({"title": "Unquietly loud", "description": ""}, rules)
    assert (result.status, result.reason) == (FAIL, "mentions none of: quiet, walkable")


# --- combined ---------------------------------------------------------------


def test_empty_criteria_checks_only_furnished() -> None:
    result = evaluate_hard_filters(listing(furnished=True), Criteria())
    assert [r.rule for r in result.rules] == [FilterRule.FURNISHED]
    assert result.passed
    assert result.failed == ()
    assert result.unknown == ()


def test_unconfigured_rules_are_omitted() -> None:
    assert evaluate_hard_filters(listing(), criteria()).rules == ()


def full_criteria(**fields: object) -> Criteria:
    return Criteria.model_validate(
        {
            "zip_codes": ["00001"],
            "price_max": usd(2000),
            "property_types_allow": ["apartment"],
            "owner_types_deny": ["agent"],
            "min_beds": 1,
            "min_baths": 1,
            "min_sqft": 400,
            "move_in": date(2026, 12, 1),
            "move_in_flex_days": 0,
            "move_out": date(2027, 3, 1),
            "move_out_flex_days": 0,
            "must_have_amenities": ["ac"],
            "keywords_allow": ["quiet"],
            "keywords_deny": ["basement"],
            **fields,
        }
    )


def full_listing(**fields: object) -> NormalizedListing:
    return listing(
        **{
            "title": "Quiet furnished apartment",
            "address": {"postal_code": "00001"},
            "price": {"amount": usd(1800)},
            "property_type": "apartment",
            "owner_type": "individual",
            "beds": 1,
            "baths": 1,
            "sqft": 600,
            "furnished": True,
            "available_from": date(2026, 11, 1),
            "min_lease_months": 1,
            "max_lease_months": 12,
            "amenities": ["ac"],
            **fields,
        }
    )


def test_every_rule_passes_in_declaration_order() -> None:
    result = evaluate_hard_filters(full_listing(), full_criteria())
    assert [r.rule for r in result.rules] == list(FilterRule)
    assert all(r.status is PASS for r in result.rules)
    assert result.passed


def test_one_failure_rejects_the_listing() -> None:
    result = evaluate_hard_filters(full_listing(beds=0), full_criteria())
    assert not result.passed
    assert [(r.rule, r.reason) for r in result.failed] == [
        (FilterRule.MIN_BEDS, "studio is below minimum 1")
    ]


def test_unknown_passes_unless_the_rule_is_in_unknown_fails() -> None:
    sparse = full_listing(sqft=None, furnished=None)
    lenient = evaluate_hard_filters(sparse, full_criteria())
    assert lenient.passed
    assert [r.rule for r in lenient.unknown] == [FilterRule.MIN_SQFT, FilterRule.FURNISHED]
    assert not any(r.blocking for r in lenient.rules)

    strict = evaluate_hard_filters(sparse, full_criteria(unknown_fails=["furnished"]))
    assert not strict.passed
    assert [(r.rule, r.status) for r in strict.failed] == [(FilterRule.FURNISHED, UNKNOWN)]
    assert [r.rule for r in strict.unknown] == [FilterRule.MIN_SQFT, FilterRule.FURNISHED]
