"""The pure scam/risk signals: price against comparables, photo reuse, payment language."""

import pytest

from furnest.config import RiskSettings
from furnest.enrichment.risk import (
    HIGH,
    MEDIUM,
    OFF_PLATFORM_PAYMENT,
    PRICE_BELOW_MARKET,
    payment_flag,
    price_flag,
    reused_photos,
)

SETTINGS = RiskSettings(price_below_median_pct=40, min_comparables=3)
COMPARABLES = [200_000, 210_000, 190_000, 200_000]  # median 200_000


def test_price_far_below_the_median_is_flagged() -> None:
    flag = price_flag(100_000, COMPARABLES, SETTINGS)

    assert flag is not None
    assert flag.kind == PRICE_BELOW_MARKET
    assert flag.severity == MEDIUM
    assert flag.detail["median_minor"] == 200_000
    assert flag.detail["percent_below"] == 50.0
    assert flag.detail["comparables"] == 4


def test_price_far_below_twice_the_threshold_is_high() -> None:
    flag = price_flag(30_000, COMPARABLES, SETTINGS)

    assert flag is not None
    assert flag.severity == HIGH


def test_price_just_inside_the_threshold_is_not_flagged() -> None:
    assert price_flag(130_000, COMPARABLES, SETTINGS) is None


def test_price_at_the_threshold_is_flagged() -> None:
    assert price_flag(120_000, COMPARABLES, SETTINGS) is not None


def test_price_threshold_is_configurable() -> None:
    strict = RiskSettings(price_below_median_pct=10, min_comparables=3)

    assert price_flag(170_000, COMPARABLES, strict) is not None
    assert price_flag(170_000, COMPARABLES, SETTINGS) is None


def test_price_needs_enough_comparables() -> None:
    assert price_flag(10_000, COMPARABLES[:2], SETTINGS) is None


def test_price_above_the_median_is_not_flagged() -> None:
    assert price_flag(300_000, COMPARABLES, SETTINGS) is None


def test_reused_photos_matches_near_identical_hashes_per_listing() -> None:
    own = ["ee9d91626a159595", "0000000000000000"]
    others = [
        (1, "ee9d91626a159594"),  # one bit away from the first photo
        (2, "f5989e69c93230c7"),  # unrelated
        (3, "ee9d91626a159595"),
        (3, "0000000000000001"),
    ]

    assert reused_photos(own, others, max_distance=4) == {1: 1, 3: 2}


def test_reused_photos_respects_the_distance() -> None:
    assert reused_photos(["ee9d91626a159595"], [(1, "ee9d91626a159594")], max_distance=0) == {}


def test_reused_photos_with_nothing_to_compare() -> None:
    assert reused_photos([], [(1, "ee9d91626a159595")], max_distance=4) == {}
    assert reused_photos(["ee9d91626a159595"], [], max_distance=4) == {}


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("Please wire transfer the deposit to hold the unit", "wire transfer"),
        ("We only accept Western Union", "wire transfer"),
        ("Pay with gift cards", "gift card"),
        ("Rent payable in Bitcoin", "cryptocurrency"),
        ("I am abroad, the keys will be mailed to you", "keys sent by mail"),
    ],
)
def test_unrecoverable_payment_language_is_high(text: str, label: str) -> None:
    flag = payment_flag(text)

    assert flag is not None
    assert flag.kind == OFF_PLATFORM_PAYMENT
    assert flag.severity == HIGH
    assert label in flag.detail["matches"]


@pytest.mark.parametrize(
    "text",
    [
        "Send the deposit by Zelle before viewing",
        "Before viewing the apartment, pay via Zelle",
        "Security deposit required prior to any showing",
    ],
)
def test_payment_before_viewing_is_medium(text: str) -> None:
    flag = payment_flag(text)

    assert flag is not None
    assert flag.severity == MEDIUM


def test_several_matches_are_all_listed_and_the_worst_severity_wins() -> None:
    flag = payment_flag("Zelle before viewing, or buy gift cards")

    assert flag is not None
    assert flag.severity == HIGH
    assert flag.detail["matches"] == ["gift card", "payment app before viewing"]


@pytest.mark.parametrize(
    "text",
    [
        "Sunny furnished 1BR near downtown. Rent is paid by check or ACH after the lease.",
        "Tour the unit any weekday; pay the deposit when signing the lease.",
        "Spare keys are kept in the lobby.",
    ],
)
def test_ordinary_text_is_not_flagged(text: str) -> None:
    assert payment_flag(text) is None
