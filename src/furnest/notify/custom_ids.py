"""The ``custom_id`` on a match message's buttons: which action, on which property.

Discord hands a button's ``custom_id`` back verbatim when someone clicks it, so it is
the only state a click carries. The format is ``furnest:<action>:<property id>``,
e.g. ``furnest:approve:42``: the prefix keeps our ids apart from any other bot's
components, and the whole thing stays far below Discord's 100-character limit.

:func:`parse_custom_id` is strict. The interactions endpoint gets ids from the
network, so anything that isn't exactly this shape reads as ``None``, not an error.
"""

from __future__ import annotations

import re
from enum import StrEnum

PREFIX = "furnest"
# Discord rejects a component whose custom_id is longer than this.
MAX_CUSTOM_ID_LENGTH = 100

_PATTERN = re.compile(rf"{PREFIX}:([a-z]+):([1-9][0-9]{{0,17}})")


class DiscordAction(StrEnum):
    """The buttons on a match message, in the order they appear."""

    APPROVE = "approve"
    DENY = "deny"
    INQUIRE = "inquire"
    SNOOZE = "snooze"


def encode_custom_id(action: DiscordAction, property_id: int) -> str:
    if property_id < 1:
        msg = f"property id must be positive, got {property_id}"
        raise ValueError(msg)
    return f"{PREFIX}:{action.value}:{property_id}"


def parse_custom_id(custom_id: str) -> tuple[DiscordAction, int] | None:
    """The action and property id in ``custom_id``, or ``None`` if it isn't one of ours."""
    match = _PATTERN.fullmatch(custom_id)
    if match is None:
        return None
    try:
        action = DiscordAction(match.group(1))
    except ValueError:
        return None
    return action, int(match.group(2))
