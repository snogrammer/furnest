"""Button custom_ids: ``furnest:<action>:<property id>`` round-trips; anything else is None."""

import pytest

from furnest.notify.custom_ids import (
    MAX_CUSTOM_ID_LENGTH,
    DiscordAction,
    encode_custom_id,
    parse_custom_id,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("action", list(DiscordAction))
@pytest.mark.parametrize("property_id", [1, 42, 10**17])
def test_round_trip(action: DiscordAction, property_id: int) -> None:
    custom_id = encode_custom_id(action, property_id)

    assert len(custom_id) <= MAX_CUSTOM_ID_LENGTH
    assert parse_custom_id(custom_id) == (action, property_id)


def test_format() -> None:
    assert encode_custom_id(DiscordAction.APPROVE, 42) == "furnest:approve:42"


@pytest.mark.parametrize("property_id", [0, -1])
def test_encode_rejects_non_positive_ids(property_id: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        encode_custom_id(DiscordAction.DENY, property_id)


@pytest.mark.parametrize(
    "custom_id",
    [
        "",
        "furnest:approve",
        "furnest:approve:",
        "furnest:approve:0",
        "furnest:approve:042",
        "furnest:approve:-1",
        "furnest:approve:4x",
        "furnest:approve:42:extra",
        "furnest:archive:42",
        "furnest:APPROVE:42",
        "other:approve:42",
        " furnest:approve:42",
        "furnest:approve:42\n",
        "furnest:approve:" + "9" * 19,
    ],
)
def test_parse_rejects_anything_else(custom_id: str) -> None:
    assert parse_custom_id(custom_id) is None
