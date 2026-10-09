"""Owner types: label normalisation and inference from adapter hints and listing text."""

import pytest

from furnest.domain import OwnerType, infer_owner_type, normalize_owner_type, owner_type_label


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("individual", OwnerType.INDIVIDUAL),
        ("Private Landlord", OwnerType.INDIVIDUAL),
        ("by-owner", OwnerType.INDIVIDUAL),
        ("FRBO", OwnerType.INDIVIDUAL),
        ("property_manager", OwnerType.PROPERTY_MANAGER),
        ("Property Management Company", OwnerType.PROPERTY_MANAGER),
        ("corporation", OwnerType.PROPERTY_MANAGER),
        ("  Complex ", OwnerType.PROPERTY_MANAGER),
        ("agent", OwnerType.AGENT),
        ("Realtor", OwnerType.AGENT),
        ("real-estate agent", OwnerType.AGENT),
        ("unknown", OwnerType.UNKNOWN),
        ("tenant", OwnerType.UNKNOWN),
        ("", OwnerType.UNKNOWN),
        (None, OwnerType.UNKNOWN),
        (OwnerType.AGENT, OwnerType.AGENT),
    ],
)
def test_normalize_owner_type(raw: str | OwnerType | None, expected: OwnerType) -> None:
    assert normalize_owner_type(raw) is expected


def test_every_enum_value_maps_to_itself() -> None:
    for member in OwnerType:
        assert normalize_owner_type(member.value) is member


def test_every_enum_value_has_a_label() -> None:
    labels = {owner_type_label(member) for member in OwnerType}
    assert len(labels) == len(OwnerType)
    assert owner_type_label(OwnerType.AGENT) == "Agent / broker"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Individuals.
        ("Rented directly by the private landlord.", OwnerType.INDIVIDUAL),
        ("Furnished condo for rent BY OWNER", OwnerType.INDIVIDUAL),
        ("I am the owner and live next door.", OwnerType.INDIVIDUAL),
        ("No brokers please.", OwnerType.INDIVIDUAL),
        ("Owner-occupied duplex, upstairs unit.", OwnerType.INDIVIDUAL),
        ("Renting out my condo while abroad.", OwnerType.INDIVIDUAL),
        ("We live downstairs and keep the garden.", OwnerType.INDIVIDUAL),
        # Agents.
        ("Listed by a licensed broker.", OwnerType.AGENT),
        ("Contact the listing agent for a tour.", OwnerType.AGENT),
        ("One month broker's fee applies.", OwnerType.AGENT),
        ("Realtors welcome.", OwnerType.AGENT),
        ("Example Brokerage presents a bright one-bed.", OwnerType.AGENT),
        # Managers.
        ("Managed by a property management firm.", OwnerType.PROPERTY_MANAGER),
        ("Call our leasing office today.", OwnerType.PROPERTY_MANAGER),
        ("Apply online in minutes.", OwnerType.PROPERTY_MANAGER),
        ("Application fee: $45 per adult.", OwnerType.PROPERTY_MANAGER),
        ("Our pet-friendly community welcomes you.", OwnerType.PROPERTY_MANAGER),
        ("Pay rent through the resident portal.", OwnerType.PROPERTY_MANAGER),
        ("Professional On-Site Management.", OwnerType.PROPERTY_MANAGER),
        ("Professionally managed building.", OwnerType.PROPERTY_MANAGER),
        ("Office Hours: Mon-Fri 9-5.", OwnerType.PROPERTY_MANAGER),
        ("Example Properties LLC", OwnerType.PROPERTY_MANAGER),
        ("Example Housing Inc. is pleased to offer", OwnerType.PROPERTY_MANAGER),
        ("Brought to you by Example Properties", OwnerType.PROPERTY_MANAGER),
        # Nothing to go on.
        ("Sunny 2BR with all the essentials. Properties available now.", OwnerType.UNKNOWN),
        ("The agents of change in this neighbourhood are its cafes.", OwnerType.UNKNOWN),
        ("llc is lower-case and not a company marker", OwnerType.UNKNOWN),
        ("", OwnerType.UNKNOWN),
    ],
)
def test_infer_from_text(text: str, expected: OwnerType) -> None:
    assert infer_owner_type("", text) is expected


def test_title_counts_too() -> None:
    assert infer_owner_type("Example Realty broker listing", "") is OwnerType.AGENT


def test_individual_phrases_outrank_agent_and_manager_phrases() -> None:
    text = "No brokers. Application fee $30. I am the owner."
    assert infer_owner_type("", text) is OwnerType.INDIVIDUAL


def test_agent_phrases_outrank_manager_phrases() -> None:
    text = "Broker fee one month; apply online through our leasing office."
    assert infer_owner_type("", text) is OwnerType.AGENT


@pytest.mark.parametrize(
    "hint", [OwnerType.INDIVIDUAL, OwnerType.PROPERTY_MANAGER, OwnerType.AGENT]
)
def test_a_hint_wins_over_the_text(hint: OwnerType) -> None:
    assert infer_owner_type("", "No brokers, apply online, broker fee", hint=hint) is hint


@pytest.mark.parametrize("hint", [None, OwnerType.UNKNOWN])
def test_an_unknown_hint_falls_back_to_the_text(hint: OwnerType | None) -> None:
    assert infer_owner_type("", "Call our leasing office", hint=hint) is OwnerType.PROPERTY_MANAGER
    assert infer_owner_type("", "Nothing to see here", hint=hint) is OwnerType.UNKNOWN
