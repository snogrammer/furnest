"""Incoming Discord interactions: signature checks, parsing and response bodies.

Discord delivers button clicks and modal submits to ``POST /discord/interactions``
(:mod:`furnest.web.routes.discord`). Anyone can call that URL, so every request is
checked before it is trusted:

* **Signature.** Discord signs ``timestamp + raw body`` with the application's
  Ed25519 key and sends the result in ``X-Signature-Ed25519`` and
  ``X-Signature-Timestamp``. :func:`verify_signature` checks it against
  ``DISCORD_PUBLIC_KEY``.
* **Freshness.** A valid signature never expires, so a captured request could be sent
  again later. :func:`is_fresh` rejects a timestamp further than
  ``DISCORD_INTERACTION_MAX_AGE_SECONDS`` from our clock. Inside that window the
  ``discord_interactions`` table catches a repeat by its interaction id.

Discord waits only 3 seconds for the response. The endpoint therefore answers with
one of the small bodies built here (usually :func:`deferred_update`, or
:func:`modal` for Deny) and leaves slow work, such as editing the message, to the
worker (:mod:`furnest.notify.sync`).

Parsing is defensive. :meth:`Interaction.from_json` raises ``ValueError`` for
anything that doesn't have the fields we rely on, and the route turns that into a 400.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from furnest.notify.custom_ids import OPEN_CUSTOM_ID, DiscordAction, parse_custom_id

JsonObject = dict[str, Any]

# Message flag that shows a reply to the clicking user only.
EPHEMERAL = 1 << 6
# Component types and the text input style a modal uses.
ACTION_ROW = 1
TEXT_INPUT = 4
PARAGRAPH = 2
# Discord's caps on a modal's title and a text input's value, in characters.
MAX_MODAL_TITLE = 45
MAX_TEXT_INPUT = 4000


class InteractionType(IntEnum):
    PING = 1
    APPLICATION_COMMAND = 2
    MESSAGE_COMPONENT = 3
    APPLICATION_COMMAND_AUTOCOMPLETE = 4
    MODAL_SUBMIT = 5


class ResponseType(IntEnum):
    PONG = 1
    CHANNEL_MESSAGE_WITH_SOURCE = 4
    DEFERRED_UPDATE_MESSAGE = 6
    UPDATE_MESSAGE = 7
    MODAL = 9


def verify_signature(public_key: str, signature: str, timestamp: str, body: bytes) -> bool:
    """Whether ``signature`` (hex) is Discord's signature of ``timestamp + body``."""
    try:
        key = VerifyKey(bytes.fromhex(public_key))
        key.verify(timestamp.encode() + body, bytes.fromhex(signature))
    # Bad hex and keys of the wrong length raise ValueError (or a subclass of it).
    except (ValueError, BadSignatureError):
        return False
    return True


def is_fresh(timestamp: str, *, now: float, max_age: float) -> bool:
    """Whether ``timestamp`` (unix seconds) is within ``max_age`` of ``now``, either way."""
    if not timestamp.isascii() or not timestamp.isdigit():
        return False
    return abs(now - int(timestamp)) <= max_age


@dataclass(frozen=True)
class Interaction:
    """The parts of an interaction furnest uses."""

    id: str
    type: InteractionType
    token: str
    application_id: str
    custom_id: str | None = None
    # ``None`` unless ``custom_id`` is one of ours (``furnest:<action>:<id>``).
    action: DiscordAction | None = None
    property_id: int | None = None
    user_id: str | None = None
    user_name: str | None = None
    channel_id: str | None = None
    message_id: str | None = None
    # The clicked message is one only the clicker sees (a card opened from a list's
    # menu). The bot token can't edit it; the response has to.
    message_ephemeral: bool = False
    # Text inputs of a submitted modal, by their custom_id.
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def actor_detail(self) -> str | None:
        """Who clicked, for the audit trail: ``"name (id)"``, or whichever part is known."""
        if self.user_name and self.user_id:
            return f"{self.user_name} ({self.user_id})"
        return self.user_name or self.user_id

    @classmethod
    def from_json(cls, data: object) -> Interaction:
        if not isinstance(data, dict):
            msg = "interaction is not a JSON object"
            raise ValueError(msg)
        raw_type = data.get("type")
        if type(raw_type) is not int or raw_type not in set(InteractionType):
            msg = f"unknown interaction type {raw_type!r}"
            raise ValueError(msg)
        kind = InteractionType(raw_type)
        interaction_id = _str(data, "id")
        token = _str(data, "token")
        application_id = _str(data, "application_id")
        if interaction_id is None or token is None or application_id is None:
            msg = "interaction lacks id, token or application_id"
            raise ValueError(msg)

        payload = _obj(data, "data")
        custom_id = _str(payload, "custom_id")
        parsed = parse_custom_id(custom_id) if custom_id is not None else None
        if custom_id == OPEN_CUSTOM_ID:
            parsed = _picked(payload.get("values"))
        message = _obj(data, "message")
        flags = message.get("flags")
        # A click in a server carries ``member.user``; one in a DM carries ``user``.
        user = _obj(_obj(data, "member"), "user") or _obj(data, "user")
        return cls(
            id=interaction_id,
            type=kind,
            token=token,
            application_id=application_id,
            custom_id=custom_id,
            action=parsed[0] if parsed else None,
            property_id=parsed[1] if parsed else None,
            user_id=_str(user, "id"),
            user_name=_str(user, "global_name") or _str(user, "username"),
            channel_id=_str(data, "channel_id"),
            message_id=_str(message, "id"),
            message_ephemeral=type(flags) is int and bool(flags & EPHEMERAL),
            fields=_text_inputs(payload.get("components")),
        )


def refusal(
    interaction: Interaction, *, channel_id: str | None, allowed_user_ids: Sequence[str]
) -> str | None:
    """Why ``interaction`` may not act, or ``None`` when it may.

    A valid signature only proves Discord sent the click, not who made it. The click
    must come from the configured channel, and, when ``allowed_user_ids`` is set,
    from one of those users.
    """
    if channel_id is not None and interaction.channel_id != channel_id:
        return "wrong channel"
    if allowed_user_ids and interaction.user_id not in allowed_user_ids:
        return "user not allowed"
    return None


def pong() -> JsonObject:
    return {"type": ResponseType.PONG}


def deferred_update() -> JsonObject:
    """Ack a click or modal submit now and leave the message as it is; edits come later."""
    return {"type": ResponseType.DEFERRED_UPDATE_MESSAGE}


def modal(custom_id: str, title: str, inputs: list[JsonObject]) -> JsonObject:
    """Open a form; its submit comes back as a MODAL_SUBMIT with the same ``custom_id``."""
    return {
        "type": ResponseType.MODAL,
        "data": {
            "custom_id": custom_id,
            "title": title[:MAX_MODAL_TITLE],
            "components": [{"type": ACTION_ROW, "components": [item]} for item in inputs],
        },
    }


def paragraph_input(
    custom_id: str, label: str, *, required: bool = False, placeholder: str | None = None
) -> JsonObject:
    """A multi-line text input for :func:`modal`."""
    item: JsonObject = {
        "type": TEXT_INPUT,
        "custom_id": custom_id,
        "style": PARAGRAPH,
        "label": label,
        "required": required,
        "max_length": MAX_TEXT_INPUT,
    }
    if placeholder is not None:
        item["placeholder"] = placeholder
    return item


def update_message(payload: JsonObject) -> JsonObject:
    """Redraw the clicked message with ``payload`` as the response itself."""
    return {"type": ResponseType.UPDATE_MESSAGE, "data": payload}


def ephemeral_message(payload: JsonObject) -> JsonObject:
    """Reply with the message ``payload`` (embeds, buttons), shown only to the clicker."""
    return {
        "type": ResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
        "data": {**payload, "flags": EPHEMERAL},
    }


def ephemeral(content: str) -> JsonObject:
    """A reply only the clicking user sees, which can't ping anyone."""
    return {
        "type": ResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
        "data": {"content": content, "flags": EPHEMERAL, "allowed_mentions": {"parse": []}},
    }


def _picked(values: object) -> tuple[DiscordAction, int] | None:
    """The property picked in a list message's menu, from the interaction's ``values``."""
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        return None
    value = values[0]
    if not value.isascii() or not value.isdigit() or len(value) > 18 or int(value) < 1:
        return None
    return DiscordAction.OPEN, int(value)


def _obj(data: object, key: str) -> JsonObject:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, dict) else {}


def _str(data: JsonObject, key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _text_inputs(components: object) -> dict[str, str]:
    """Every ``custom_id``/``value`` pair in a modal submit's component tree.

    Inputs arrive nested in action rows (``components``) or, in newer layouts, in
    labels (``component``), so this walks both.
    """
    found: dict[str, str] = {}
    stack: list[object] = [components]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            custom_id, value = node.get("custom_id"), node.get("value")
            if isinstance(custom_id, str) and isinstance(value, str):
                found[custom_id] = value
            stack.extend(node[key] for key in ("components", "component") if key in node)
    return found
