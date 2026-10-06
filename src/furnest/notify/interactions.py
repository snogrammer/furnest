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
one of the small bodies built here (usually :func:`deferred_update`) and leaves slow
work to the worker. The worker gets the interaction as an :data:`INTERACTION_JOB`
job whose payload is :meth:`Interaction.to_payload`. The payload includes the
interaction token, which is good for 15 minutes of follow-up edits to the message.

Parsing is defensive. :meth:`Interaction.from_json` raises ``ValueError`` for
anything that doesn't have the fields we rely on, and the route turns that into a 400.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from furnest.logging import get_logger
from furnest.notify.custom_ids import DiscordAction, parse_custom_id

if TYPE_CHECKING:
    from furnest.worker.execution import JobContext

log = get_logger(__name__)

JsonObject = dict[str, Any]

# Job kind the worker runs for an interaction that needs more than an ack.
INTERACTION_JOB = "discord.interaction"
# Message flag that shows a reply to the clicking user only.
EPHEMERAL = 1 << 6


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
    # Text inputs of a submitted modal, by their custom_id.
    fields: dict[str, str] = field(default_factory=dict)

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
            message_id=_str(_obj(data, "message"), "id"),
            fields=_text_inputs(payload.get("components")),
        )

    def to_payload(self) -> JsonObject:
        """A JSON-safe dict for a queued job; :meth:`from_payload` reverses it."""
        return {
            "id": self.id,
            "type": int(self.type),
            "token": self.token,
            "application_id": self.application_id,
            "custom_id": self.custom_id,
            "action": self.action.value if self.action else None,
            "property_id": self.property_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "fields": dict(self.fields),
        }

    @classmethod
    def from_payload(cls, payload: JsonObject) -> Interaction:
        action = payload.get("action")
        return cls(
            id=payload["id"],
            type=InteractionType(payload["type"]),
            token=payload["token"],
            application_id=payload["application_id"],
            custom_id=payload.get("custom_id"),
            action=DiscordAction(action) if action else None,
            property_id=payload.get("property_id"),
            user_id=payload.get("user_id"),
            user_name=payload.get("user_name"),
            channel_id=payload.get("channel_id"),
            message_id=payload.get("message_id"),
            fields=dict(payload.get("fields") or {}),
        )


def interaction_not_handled(ctx: JobContext) -> None:
    """Placeholder :data:`INTERACTION_JOB` handler until the buttons do something (#36)."""
    interaction = Interaction.from_payload(ctx.payload)
    log.info(
        "discord_interaction_skipped",
        interaction_id=interaction.id,
        action=interaction.action.value if interaction.action else None,
        property_id=interaction.property_id,
    )


def pong() -> JsonObject:
    return {"type": ResponseType.PONG}


def deferred_update() -> JsonObject:
    """Ack a click or modal submit now and leave the message as it is; edits come later."""
    return {"type": ResponseType.DEFERRED_UPDATE_MESSAGE}


def ephemeral(content: str) -> JsonObject:
    """A reply only the clicking user sees, which can't ping anyone."""
    return {
        "type": ResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
        "data": {"content": content, "flags": EPHEMERAL, "allowed_mentions": {"parse": []}},
    }


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
