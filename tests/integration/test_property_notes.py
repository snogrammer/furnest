"""Notes on a property, and denying with a reason saved as a note."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.listing_store import SqlListingStore
from furnest.db.models import AuditEvent, Note, Property
from furnest.db.property_store import (
    MAX_NOTE_LENGTH,
    PropertyNotFoundError,
    SqlPropertyStore,
)
from furnest.db.session import session_scope
from furnest.domain import Actor, InvalidTransitionError, NormalizedListing, PropertyStatus

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlPropertyStore:
    return SqlPropertyStore(session_factory)


@pytest.fixture
def property_id(session_factory: sessionmaker[Session], store: SqlPropertyStore) -> int:
    listing = NormalizedListing.model_validate(
        {"source": "test", "source_id": "a", "url": "https://example.com/a", "title": "A"}
    )
    (listing_id,) = (
        SqlListingStore(session_factory).upsert_batch("test", [listing], seen_at=T0).new_ids
    )
    return store.ensure_for_listing(listing_id, at=T0).id


def count(session_factory: sessionmaker[Session], model: type[Note] | type[AuditEvent]) -> int:
    with session_scope(session_factory) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def set_status(session_factory: sessionmaker[Session], property_id: int, status: S) -> None:
    with session_scope(session_factory) as session:
        session.execute(update(Property).where(Property.id == property_id).values(status=status))


def test_notes_are_added_and_listed_oldest_first(store: SqlPropertyStore, property_id: int) -> None:
    first = store.add_note(property_id, "  **first**\n", actor=Actor.DASHBOARD, at=T0)
    store.add_note(property_id, "second", actor=Actor.DISCORD, at=T0 + HOUR, detail="someone")

    notes = store.notes(property_id)

    assert [n.body_md for n in notes] == ["**first**", "second"]
    assert notes[0] == first
    assert (notes[1].actor, notes[1].actor_detail) == (Actor.DISCORD, "someone")
    assert notes[1].created_at == T0 + HOUR
    assert store.detail(property_id).notes == notes  # type: ignore[union-attr]


@pytest.mark.parametrize("body", ["", "   \n\t", "x" * (MAX_NOTE_LENGTH + 1)])
def test_empty_or_too_long_notes_are_rejected(
    store: SqlPropertyStore, property_id: int, body: str
) -> None:
    with pytest.raises(ValueError, match="note"):
        store.add_note(property_id, body, actor=Actor.DASHBOARD, at=T0)

    assert store.notes(property_id) == []


def test_note_at_the_limit_is_accepted(store: SqlPropertyStore, property_id: int) -> None:
    note = store.add_note(property_id, "x" * MAX_NOTE_LENGTH + "\n", actor=Actor.DASHBOARD, at=T0)

    assert len(note.body_md) == MAX_NOTE_LENGTH


def test_note_on_unknown_property(store: SqlPropertyStore) -> None:
    with pytest.raises(PropertyNotFoundError):
        store.add_note(999, "hi", actor=Actor.DASHBOARD, at=T0)


def test_notes_go_with_their_property(
    store: SqlPropertyStore, property_id: int, session_factory: sessionmaker[Session]
) -> None:
    store.add_note(property_id, "hi", actor=Actor.DASHBOARD, at=T0)

    with session_scope(session_factory) as session:
        session.execute(delete(Property).where(Property.id == property_id))

    assert count(session_factory, Note) == 0


def test_deny_with_reason_saves_it_as_a_note(store: SqlPropertyStore, property_id: int) -> None:
    state = store.deny(
        property_id, actor=Actor.DASHBOARD, at=T0 + HOUR, reason=" Too far from *work* "
    )

    assert state.status is S.DENIED
    (note,) = store.notes(property_id)
    assert note.body_md == "Too far from *work*"
    assert (note.actor, note.created_at) == (Actor.DASHBOARD, T0 + HOUR)
    event = store.audit_events(property_id)[-1]
    assert (event.from_status, event.to_status) == (S.NEW, S.DENIED)
    assert event.actor is Actor.DASHBOARD
    assert event.payload == {"note_id": note.id}


@pytest.mark.parametrize("reason", [None, "", "  \n"])
def test_deny_without_reason_writes_no_note(
    store: SqlPropertyStore, property_id: int, reason: str | None
) -> None:
    state = store.deny(property_id, actor=Actor.DISCORD, at=T0, reason=reason, detail="someone")

    assert state.status is S.DENIED
    assert store.notes(property_id) == []
    event = store.audit_events(property_id)[-1]
    assert (event.payload, event.actor_detail) == (None, "someone")


def test_forbidden_deny_writes_nothing(
    store: SqlPropertyStore, property_id: int, session_factory: sessionmaker[Session]
) -> None:
    set_status(session_factory, property_id, S.WON)
    events = count(session_factory, AuditEvent)

    with pytest.raises(InvalidTransitionError):
        store.deny(property_id, actor=Actor.DASHBOARD, at=T0, reason="nope")

    assert store.get(property_id).status is S.WON  # type: ignore[union-attr]
    assert count(session_factory, Note) == 0
    assert count(session_factory, AuditEvent) == events


def test_deny_with_too_long_reason_writes_nothing(
    store: SqlPropertyStore, property_id: int
) -> None:
    with pytest.raises(ValueError, match="longer"):
        store.deny(property_id, actor=Actor.DASHBOARD, at=T0, reason="x" * (MAX_NOTE_LENGTH + 1))

    assert store.get(property_id).status is S.NEW  # type: ignore[union-attr]


def test_deny_unknown_property(store: SqlPropertyStore) -> None:
    with pytest.raises(PropertyNotFoundError):
        store.deny(999, actor=Actor.DASHBOARD, at=T0, reason="x")
