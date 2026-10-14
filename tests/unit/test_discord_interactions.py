"""Interaction signatures, timestamp freshness, parsing and response bodies."""

from typing import Any

import pytest
from nacl.signing import SigningKey

from furnest.notify.custom_ids import OPEN_CUSTOM_ID, DiscordAction
from furnest.notify.interactions import (
    EPHEMERAL,
    MAX_TEXT_INPUT,
    Interaction,
    InteractionType,
    ResponseType,
    deferred_update,
    ephemeral,
    ephemeral_message,
    is_fresh,
    modal,
    paragraph_input,
    pong,
    update_message,
    verify_signature,
)
from furnest.web.discord_dispatch import InteractionDispatcher

pytestmark = pytest.mark.unit

KEY = SigningKey(bytes(range(32)))
PUBLIC_KEY = KEY.verify_key.encode().hex()
BODY = b'{"type":1}'
TIMESTAMP = "1790000000"


def sign(timestamp: str, body: bytes, key: SigningKey = KEY) -> str:
    return key.sign(timestamp.encode() + body).signature.hex()


def test_valid_signature() -> None:
    assert verify_signature(PUBLIC_KEY, sign(TIMESTAMP, BODY), TIMESTAMP, BODY)


def test_tampered_body_fails() -> None:
    assert not verify_signature(PUBLIC_KEY, sign(TIMESTAMP, BODY), TIMESTAMP, b'{"type":3}')


def test_tampered_timestamp_fails() -> None:
    assert not verify_signature(PUBLIC_KEY, sign(TIMESTAMP, BODY), "1790000001", BODY)


def test_other_key_fails() -> None:
    other = SigningKey(bytes(range(1, 33)))
    assert not verify_signature(PUBLIC_KEY, sign(TIMESTAMP, BODY, other), TIMESTAMP, BODY)


@pytest.mark.parametrize("signature", ["", "zz", "ab" * 10, "ab" * 64 + "a"])
def test_malformed_signature_fails(signature: str) -> None:
    assert not verify_signature(PUBLIC_KEY, signature, TIMESTAMP, BODY)


@pytest.mark.parametrize("public_key", ["", "zz" * 32, "ab" * 31])
def test_malformed_public_key_fails(public_key: str) -> None:
    assert not verify_signature(public_key, sign(TIMESTAMP, BODY), TIMESTAMP, BODY)


@pytest.mark.parametrize(
    ("timestamp", "fresh"),
    [
        ("1000", True),
        ("700", True),  # exactly max_age old
        ("699", False),
        ("1300", True),  # exactly max_age ahead
        ("1301", False),
        ("", False),
        ("-5", False),
        ("1000.5", False),
        ("\uff11\uff10\uff10\uff10", False),  # fullwidth digits: isdigit() but not ASCII
    ],
)
def test_is_fresh(timestamp: str, fresh: bool) -> None:
    assert is_fresh(timestamp, now=1000.0, max_age=300) is fresh


def interaction_json(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    data: dict[str, Any] = {
        "id": "1100000000000000001",
        "type": 3,
        "token": "interaction-token",
        "application_id": "1000000000000000000",
        "channel_id": "1200000000000000000",
        "message": {"id": "1300000000000000000"},
        "data": {"custom_id": "furnest:approve:42", "component_type": 2},
        "member": {"user": {"id": "1400000000000000000", "username": "renter"}},
    }
    data.update(overrides)
    return data


def test_parses_a_server_button_click() -> None:
    interaction = Interaction.from_json(interaction_json())

    assert interaction.type is InteractionType.MESSAGE_COMPONENT
    assert interaction.action is DiscordAction.APPROVE
    assert interaction.property_id == 42
    assert interaction.user_id == "1400000000000000000"
    assert interaction.user_name == "renter"
    assert interaction.channel_id == "1200000000000000000"
    assert interaction.message_id == "1300000000000000000"


def test_dm_click_uses_user_and_prefers_global_name() -> None:
    data = interaction_json(user={"id": "7", "username": "renter", "global_name": "Renter"})
    del data["member"]

    interaction = Interaction.from_json(data)

    assert interaction.user_id == "7"
    assert interaction.user_name == "Renter"


def test_foreign_custom_id_has_no_action() -> None:
    interaction = Interaction.from_json(interaction_json(data={"custom_id": "otherbot:x"}))

    assert interaction.custom_id == "otherbot:x"
    assert interaction.action is None
    assert interaction.property_id is None


def test_modal_submit_collects_text_inputs() -> None:
    data = interaction_json(
        type=5,
        data={
            "custom_id": "furnest:deny:42",
            "components": [
                {"type": 1, "components": [{"type": 4, "custom_id": "reason", "value": "Too far"}]},
                {"type": 18, "component": {"type": 4, "custom_id": "extra", "value": ""}},
            ],
        },
    )

    interaction = Interaction.from_json(data)

    assert interaction.type is InteractionType.MODAL_SUBMIT
    assert interaction.action is DiscordAction.DENY
    assert interaction.fields == {"reason": "Too far", "extra": ""}


def test_ping_needs_no_data() -> None:
    interaction = Interaction.from_json({"id": "1", "type": 1, "token": "t", "application_id": "2"})

    assert interaction.type is InteractionType.PING
    assert interaction.custom_id is None


@pytest.mark.parametrize(
    "data",
    [
        [],
        "x",
        interaction_json(type=99),
        interaction_json(type="3"),
        interaction_json(type=True),
        interaction_json(id=None),
        interaction_json(token=""),
        interaction_json(application_id=5),
    ],
)
def test_malformed_payload_raises(data: object) -> None:
    with pytest.raises(ValueError):
        Interaction.from_json(data)


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ({"id": "7", "username": "renter"}, "renter (7)"),
        ({"id": "7", "username": "renter", "global_name": "Renter"}, "Renter (7)"),
        ({"id": "7"}, "7"),
        ({}, None),
    ],
)
def test_actor_detail_names_the_clicking_user(user: dict[str, str], expected: str | None) -> None:
    interaction = Interaction.from_json(interaction_json(member={"user": user}))

    assert interaction.actor_detail == expected


def test_response_bodies() -> None:
    assert pong() == {"type": ResponseType.PONG}
    assert deferred_update() == {"type": ResponseType.DEFERRED_UPDATE_MESSAGE}
    reply = ephemeral("nope")
    assert reply["type"] == ResponseType.CHANNEL_MESSAGE_WITH_SOURCE
    assert reply["data"] == {
        "content": "nope",
        "flags": EPHEMERAL,
        "allowed_mentions": {"parse": []},
    }


def test_modal_wraps_each_input_in_an_action_row() -> None:
    reason = paragraph_input("reason", "Why?", placeholder="…")
    body = modal("furnest:deny:7", "x" * 60, [reason])

    assert body["type"] == ResponseType.MODAL
    assert body["data"]["custom_id"] == "furnest:deny:7"
    assert len(body["data"]["title"]) == 45
    assert body["data"]["components"] == [{"type": 1, "components": [reason]}]
    assert reason == {
        "type": 4,
        "custom_id": "reason",
        "style": 2,
        "label": "Why?",
        "required": False,
        "max_length": MAX_TEXT_INPUT,
        "placeholder": "…",
    }


def test_dispatcher_refuses_a_second_handler_for_the_same_key() -> None:
    dispatcher = InteractionDispatcher()
    dispatcher.register(InteractionType.MESSAGE_COMPONENT, DiscordAction.DENY, lambda _s, _i: {})

    with pytest.raises(ValueError, match="already registered"):
        dispatcher.register(
            InteractionType.MESSAGE_COMPONENT, DiscordAction.DENY, lambda _s, _i: {}
        )


def test_parses_a_pick_from_a_list_menu() -> None:
    data = {"custom_id": OPEN_CUSTOM_ID, "component_type": 3, "values": ["42"]}

    interaction = Interaction.from_json(interaction_json(data=data))

    assert interaction.action is DiscordAction.OPEN
    assert interaction.property_id == 42
    assert not interaction.message_ephemeral


@pytest.mark.parametrize(
    "values",
    [None, [], ["1", "2"], [42], ["0"], ["-1"], ["\uff14\uff12"], ["abc"], ["1" * 19]],
)
def test_a_malformed_pick_has_no_action(values: object) -> None:
    data = {"custom_id": OPEN_CUSTOM_ID, "component_type": 3, "values": values}

    interaction = Interaction.from_json(interaction_json(data=data))

    assert interaction.action is None
    assert interaction.property_id is None


@pytest.mark.parametrize(
    ("flags", "ephemeral_message"),
    [(EPHEMERAL, True), (EPHEMERAL | 1, True), (0, False), ("64", False), (None, False)],
)
def test_parses_whether_the_clicked_message_is_ephemeral(
    flags: object, ephemeral_message: bool
) -> None:
    data = interaction_json(message={"id": "1300000000000000000", "flags": flags})

    assert Interaction.from_json(data).message_ephemeral is ephemeral_message


def test_message_response_bodies() -> None:
    payload = {"embeds": [{"title": "Loft"}], "allowed_mentions": {"parse": []}}

    assert update_message(payload) == {"type": ResponseType.UPDATE_MESSAGE, "data": payload}
    reply = ephemeral_message(payload)
    assert reply["type"] == ResponseType.CHANNEL_MESSAGE_WITH_SOURCE
    assert reply["data"] == {**payload, "flags": EPHEMERAL}
