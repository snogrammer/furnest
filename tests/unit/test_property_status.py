"""The property state machine: every (from, to) pair, and the unavailable guards."""

import itertools

import pytest

from furnest.domain import (
    TRANSITIONS,
    Actor,
    InvalidTransitionError,
    PropertyStatus,
    check_transition,
)

S = PropertyStatus

# Written out by hand from the spec, independently of TRANSITIONS. Targets out of
# unavailable are listed separately because they also depend on restore_to.
ALLOWED: dict[PropertyStatus, set[PropertyStatus]] = {
    S.NEW: {S.SUGGESTED, S.DENIED, S.ARCHIVED, S.UNAVAILABLE},
    S.SUGGESTED: {S.APPROVED, S.DENIED, S.ARCHIVED, S.UNAVAILABLE},
    S.APPROVED: {S.INQUIRY_DRAFTED, S.DENIED, S.ARCHIVED, S.UNAVAILABLE},
    S.INQUIRY_DRAFTED: {S.INQUIRY_SENT, S.APPROVED, S.ARCHIVED, S.UNAVAILABLE},
    S.INQUIRY_SENT: {S.IN_PROGRESS, S.RESPONDED, S.LOST, S.ARCHIVED, S.UNAVAILABLE},
    S.IN_PROGRESS: {S.RESPONDED, S.TOURING, S.APPLIED, S.LOST, S.ARCHIVED, S.UNAVAILABLE},
    S.RESPONDED: {S.TOURING, S.APPLIED, S.WON, S.LOST, S.ARCHIVED, S.UNAVAILABLE},
    S.TOURING: {S.APPLIED, S.WON, S.LOST, S.ARCHIVED, S.UNAVAILABLE},
    S.APPLIED: {S.WON, S.LOST, S.ARCHIVED, S.UNAVAILABLE},
    S.DENIED: {S.APPROVED, S.ARCHIVED},
    S.WON: {S.ARCHIVED},
    S.LOST: {S.ARCHIVED},
    S.ARCHIVED: set(),
}
RESTORABLE = {
    S.NEW,
    S.SUGGESTED,
    S.APPROVED,
    S.INQUIRY_DRAFTED,
    S.INQUIRY_SENT,
    S.IN_PROGRESS,
    S.RESPONDED,
    S.TOURING,
    S.APPLIED,
}

FORWARD_PAIRS = [
    (current, to) for current, to in itertools.product(S, S) if current is not S.UNAVAILABLE
]


def test_the_table_covers_every_status() -> None:
    assert set(TRANSITIONS) == set(S)
    assert set(ALLOWED) | {S.UNAVAILABLE} == set(S)


def test_table_matches_the_spec_written_out() -> None:
    assert {k: set(v) for k, v in TRANSITIONS.items() if k is not S.UNAVAILABLE} == ALLOWED
    assert set(TRANSITIONS[S.UNAVAILABLE]) == RESTORABLE | {S.ARCHIVED}


@pytest.mark.parametrize(("current", "to"), FORWARD_PAIRS, ids=lambda s: str(s))
def test_every_forward_pair_is_allowed_or_rejected(
    current: PropertyStatus, to: PropertyStatus
) -> None:
    # The system may do anything the table allows, including unavailable.
    if to in ALLOWED[current]:
        check_transition(current, to, Actor.SYSTEM)
    else:
        with pytest.raises(InvalidTransitionError) as excinfo:
            check_transition(current, to, Actor.SYSTEM)
        assert (excinfo.value.current, excinfo.value.to) == (current, to)


@pytest.mark.parametrize(("current", "to"), FORWARD_PAIRS, ids=lambda s: str(s))
@pytest.mark.parametrize("actor", [Actor.DASHBOARD, Actor.DISCORD])
def test_people_can_do_everything_but_mark_unavailable(
    current: PropertyStatus, to: PropertyStatus, actor: Actor
) -> None:
    if to in ALLOWED[current] and to is not S.UNAVAILABLE:
        check_transition(current, to, actor)
    else:
        with pytest.raises(InvalidTransitionError):
            check_transition(current, to, actor)


@pytest.mark.parametrize("to", list(S), ids=str)
@pytest.mark.parametrize("restore_to", sorted(RESTORABLE), ids=str)
def test_leaving_unavailable_only_goes_back_or_to_archived(
    to: PropertyStatus, restore_to: PropertyStatus
) -> None:
    if to in {restore_to, S.ARCHIVED}:
        check_transition(S.UNAVAILABLE, to, Actor.SYSTEM, restore_to=restore_to)
    else:
        with pytest.raises(InvalidTransitionError):
            check_transition(S.UNAVAILABLE, to, Actor.SYSTEM, restore_to=restore_to)


@pytest.mark.parametrize("actor", [Actor.DASHBOARD, Actor.DISCORD])
def test_only_the_system_restores_but_anyone_archives(actor: Actor) -> None:
    with pytest.raises(InvalidTransitionError, match="only the system restores"):
        check_transition(S.UNAVAILABLE, S.APPROVED, actor, restore_to=S.APPROVED)

    check_transition(S.UNAVAILABLE, S.ARCHIVED, actor, restore_to=S.APPROVED)


def test_restoring_without_a_known_previous_state_is_rejected() -> None:
    with pytest.raises(InvalidTransitionError, match="its previous state"):
        check_transition(S.UNAVAILABLE, S.NEW, Actor.SYSTEM)


def test_unavailable_to_unavailable_is_rejected() -> None:
    with pytest.raises(InvalidTransitionError, match="already there"):
        check_transition(S.UNAVAILABLE, S.UNAVAILABLE, Actor.SYSTEM, restore_to=S.NEW)


@pytest.mark.parametrize("status", list(S), ids=str)
def test_same_state_is_rejected(status: PropertyStatus) -> None:
    with pytest.raises(InvalidTransitionError, match="already there"):
        check_transition(status, status, Actor.SYSTEM, restore_to=status)


@pytest.mark.parametrize("to", list(S), ids=str)
def test_archived_is_terminal(to: PropertyStatus) -> None:
    with pytest.raises(InvalidTransitionError):
        check_transition(S.ARCHIVED, to, Actor.SYSTEM, restore_to=to)


def test_marking_unavailable_by_hand_says_why() -> None:
    with pytest.raises(InvalidTransitionError, match="only the system marks") as excinfo:
        check_transition(S.APPROVED, S.UNAVAILABLE, Actor.DASHBOARD)

    assert excinfo.value.reason == "only the system marks a property unavailable"
    assert "approved to unavailable" in str(excinfo.value)
