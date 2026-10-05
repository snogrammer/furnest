"""The property status pipeline as an explicit state machine.

A property moves through review like this::

    new → suggested → approved | denied
    approved → inquiry_drafted → inquiry_sent → in_progress
    → responded / touring / applied → won | lost | archived

and becomes ``unavailable`` when its listings disappear. :data:`TRANSITIONS` lists
every allowed move. Beyond the spec's happy path it also allows these:

* ``archived`` from almost anywhere, for the dashboard's bulk archive. It is terminal.
* ``denied`` from any state before an inquiry is sent, and ``denied → approved``
  for a change of mind.
* ``inquiry_drafted → approved`` to throw a draft away.
* ``inquiry_sent → responded`` when a reply arrives before anything else happened
  (IMAP reply detection), and ``lost`` from any state after the inquiry was sent.

``unavailable`` is the system's to set: only the ``system`` actor moves a property
into it, and out of it to anything but ``archived``. A relisted property goes back
to the state it left (``restore_to``, which the store reads from the audit trail),
so a disappearance never erases a human decision or skips one.

:func:`check_transition` applies the table and these guards. The store
(:mod:`furnest.db.property_store`) calls it inside the transaction that writes the
new status and its audit event.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class PropertyStatus(StrEnum):
    NEW = "new"
    SUGGESTED = "suggested"
    APPROVED = "approved"
    DENIED = "denied"
    INQUIRY_DRAFTED = "inquiry_drafted"
    INQUIRY_SENT = "inquiry_sent"
    IN_PROGRESS = "in_progress"
    RESPONDED = "responded"
    TOURING = "touring"
    APPLIED = "applied"
    WON = "won"
    LOST = "lost"
    ARCHIVED = "archived"
    UNAVAILABLE = "unavailable"


class Actor(StrEnum):
    """Who triggered a transition."""

    DASHBOARD = "dashboard"
    DISCORD = "discord"
    SYSTEM = "system"


S = PropertyStatus

_FORWARD: dict[PropertyStatus, frozenset[PropertyStatus]] = {
    S.NEW: frozenset({S.SUGGESTED, S.DENIED, S.ARCHIVED, S.UNAVAILABLE}),
    S.SUGGESTED: frozenset({S.APPROVED, S.DENIED, S.ARCHIVED, S.UNAVAILABLE}),
    S.APPROVED: frozenset({S.INQUIRY_DRAFTED, S.DENIED, S.ARCHIVED, S.UNAVAILABLE}),
    S.INQUIRY_DRAFTED: frozenset({S.INQUIRY_SENT, S.APPROVED, S.ARCHIVED, S.UNAVAILABLE}),
    S.INQUIRY_SENT: frozenset({S.IN_PROGRESS, S.RESPONDED, S.LOST, S.ARCHIVED, S.UNAVAILABLE}),
    S.IN_PROGRESS: frozenset(
        {S.RESPONDED, S.TOURING, S.APPLIED, S.LOST, S.ARCHIVED, S.UNAVAILABLE}
    ),
    S.RESPONDED: frozenset({S.TOURING, S.APPLIED, S.WON, S.LOST, S.ARCHIVED, S.UNAVAILABLE}),
    S.TOURING: frozenset({S.APPLIED, S.WON, S.LOST, S.ARCHIVED, S.UNAVAILABLE}),
    S.APPLIED: frozenset({S.WON, S.LOST, S.ARCHIVED, S.UNAVAILABLE}),
    S.DENIED: frozenset({S.APPROVED, S.ARCHIVED}),
    S.WON: frozenset({S.ARCHIVED}),
    S.LOST: frozenset({S.ARCHIVED}),
    S.ARCHIVED: frozenset(),
}

# Out of unavailable: archived, or back to any state that can become unavailable.
# check_transition narrows the second kind to the one the property actually left.
_RESTORABLE = frozenset(status for status, targets in _FORWARD.items() if S.UNAVAILABLE in targets)

TRANSITIONS: Mapping[PropertyStatus, frozenset[PropertyStatus]] = MappingProxyType(
    {**_FORWARD, S.UNAVAILABLE: _RESTORABLE | {S.ARCHIVED}}
)


class InvalidTransitionError(ValueError):
    def __init__(self, current: PropertyStatus, to: PropertyStatus, reason: str) -> None:
        super().__init__(f"can't move a property from {current} to {to}: {reason}")
        self.current = current
        self.to = to
        self.reason = reason


def check_transition(
    current: PropertyStatus,
    to: PropertyStatus,
    actor: Actor,
    *,
    restore_to: PropertyStatus | None = None,
) -> None:
    """Raise :class:`InvalidTransitionError` unless ``actor`` may move ``current`` to ``to``.

    ``restore_to`` is the state the property was in before it became unavailable;
    it only matters when ``current`` is ``unavailable``.
    """
    if to == current:
        raise InvalidTransitionError(current, to, "it is already there")
    if to not in TRANSITIONS[current]:
        raise InvalidTransitionError(current, to, "not an allowed transition")
    if to is S.UNAVAILABLE and actor is not Actor.SYSTEM:
        raise InvalidTransitionError(current, to, "only the system marks a property unavailable")
    if current is S.UNAVAILABLE and to is not S.ARCHIVED:
        if actor is not Actor.SYSTEM:
            raise InvalidTransitionError(
                current, to, "only the system restores a relisted property"
            )
        if to != restore_to:
            raise InvalidTransitionError(
                current,
                to,
                f"a relisted property goes back to {restore_to or 'its previous state'}",
            )
