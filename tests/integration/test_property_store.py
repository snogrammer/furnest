"""SqlPropertyStore on a migrated database: creation, transitions and the audit trail."""

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.listing_store import SqlListingStore
from furnest.db.models import AuditEvent, Listing, Property
from furnest.db.property_store import (
    ListingNotFoundError,
    PropertyNotFoundError,
    SqlPropertyStore,
)
from furnest.db.session import session_scope
from furnest.domain import Actor, InvalidTransitionError, NormalizedListing, PropertyStatus

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)

AddListing = Callable[..., int]  # (source_id="a", **listing fields) -> listing id


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlPropertyStore:
    return SqlPropertyStore(session_factory)


@pytest.fixture
def add_listing(session_factory: sessionmaker[Session]) -> AddListing:
    listings = SqlListingStore(session_factory)

    def add(source_id: str = "a", **fields: object) -> int:
        listing = NormalizedListing.model_validate(
            {
                "source": "test",
                "source_id": source_id,
                "url": f"https://example.com/{source_id}",
                "title": "Furnished place",
                **fields,
            }
        )
        (listing_id,) = listings.upsert_batch("test", [listing], seen_at=T0).new_ids
        return listing_id

    return add


def test_ensure_creates_a_new_property_linked_to_the_listing(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    listing_id = add_listing(
        lat=40.0,
        lng=-105.0,
        address={
            "line1": "123 North Main Street Apt 4B",
            "city": "Springfield",
            "region": "IL",
            "postal_code": "62701",
        },
    )

    state = store.ensure_for_listing(listing_id, at=T0)

    assert state.status is S.NEW
    assert (state.lat, state.lng) == (40.0, -105.0)
    assert state.address == "123 n main st #4b, springfield, il 62701, us"
    assert state.status_changed_at == state.created_at == T0
    assert store.for_listing(listing_id) == state
    assert store.get(state.id) == state


def test_ensure_is_idempotent(store: SqlPropertyStore, add_listing: AddListing) -> None:
    listing_id = add_listing()

    first = store.ensure_for_listing(listing_id, at=T0)
    second = store.ensure_for_listing(listing_id, at=T0 + MINUTE)

    assert first == second
    assert len(store.audit_events(first.id)) == 1


def test_one_property_per_listing(store: SqlPropertyStore, add_listing: AddListing) -> None:
    a = store.ensure_for_listing(add_listing("a"), at=T0)
    b = store.ensure_for_listing(add_listing("b"), at=T0)

    assert a.id != b.id


def test_listing_without_an_address_has_none(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    state = store.ensure_for_listing(add_listing(), at=T0)

    assert state.address is None


def test_creation_is_the_first_audit_event(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    listing_id = add_listing()
    state = store.ensure_for_listing(listing_id, at=T0)

    (event,) = store.audit_events(state.id)

    assert event.from_status is None
    assert event.to_status is S.NEW
    assert event.actor is Actor.SYSTEM
    assert event.payload == {"listing_id": listing_id}
    assert event.created_at == T0


def test_ensure_for_a_missing_listing_raises(store: SqlPropertyStore) -> None:
    with pytest.raises(ListingNotFoundError):
        store.ensure_for_listing(999, at=T0)


def test_transition_writes_status_and_exactly_one_audit_event(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    prop = store.ensure_for_listing(add_listing(), at=T0)

    moved = store.transition(prop.id, S.SUGGESTED, actor=Actor.SYSTEM, at=T0 + MINUTE)
    approved = store.transition(
        prop.id,
        S.APPROVED,
        actor=Actor.DISCORD,
        detail="user 1234",
        payload={"interaction": "abc"},
        at=T0 + 2 * MINUTE,
    )

    assert moved.status is S.SUGGESTED
    assert approved.status is S.APPROVED
    assert approved.status_changed_at == T0 + 2 * MINUTE
    assert store.get(prop.id) == approved
    _, suggested, last = store.audit_events(prop.id)
    assert (suggested.from_status, suggested.to_status) == (S.NEW, S.SUGGESTED)
    assert (last.from_status, last.to_status) == (S.SUGGESTED, S.APPROVED)
    assert last.actor is Actor.DISCORD
    assert last.actor_detail == "user 1234"
    assert last.payload == {"interaction": "abc"}
    assert last.created_at == T0 + 2 * MINUTE


def test_disallowed_transition_changes_nothing(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    prop = store.ensure_for_listing(add_listing(), at=T0)

    with pytest.raises(InvalidTransitionError):
        store.transition(prop.id, S.WON, actor=Actor.DASHBOARD, at=T0 + MINUTE)

    assert store.get(prop.id) == prop
    assert len(store.audit_events(prop.id)) == 1


def test_transition_of_a_missing_property_raises(store: SqlPropertyStore) -> None:
    with pytest.raises(PropertyNotFoundError):
        store.transition(999, S.SUGGESTED, actor=Actor.SYSTEM, at=T0)


def test_relisted_property_goes_back_to_where_it_was(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    prop = store.ensure_for_listing(add_listing(), at=T0)
    for i, status in enumerate([S.SUGGESTED, S.APPROVED, S.UNAVAILABLE], start=1):
        store.transition(prop.id, status, actor=Actor.SYSTEM, at=T0 + i * MINUTE)

    with pytest.raises(InvalidTransitionError):
        store.transition(prop.id, S.NEW, actor=Actor.SYSTEM, at=T0 + 4 * MINUTE)
    restored = store.transition(prop.id, S.APPROVED, actor=Actor.SYSTEM, at=T0 + 4 * MINUTE)

    assert restored.status is S.APPROVED


def test_restore_uses_the_latest_disappearance(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    prop = store.ensure_for_listing(add_listing(), at=T0)
    path = [S.UNAVAILABLE, S.NEW, S.SUGGESTED, S.UNAVAILABLE]
    for i, status in enumerate(path, start=1):
        store.transition(prop.id, status, actor=Actor.SYSTEM, at=T0 + i * MINUTE)

    with pytest.raises(InvalidTransitionError):
        store.transition(prop.id, S.NEW, actor=Actor.SYSTEM, at=T0 + 9 * MINUTE)
    restored = store.transition(prop.id, S.SUGGESTED, actor=Actor.SYSTEM, at=T0 + 9 * MINUTE)

    assert restored.status is S.SUGGESTED


def test_full_happy_path_is_audited(store: SqlPropertyStore, add_listing: AddListing) -> None:
    prop = store.ensure_for_listing(add_listing(), at=T0)
    path = [
        (S.SUGGESTED, Actor.SYSTEM),
        (S.APPROVED, Actor.DISCORD),
        (S.INQUIRY_DRAFTED, Actor.SYSTEM),
        (S.INQUIRY_SENT, Actor.DASHBOARD),
        (S.IN_PROGRESS, Actor.DASHBOARD),
        (S.RESPONDED, Actor.SYSTEM),
        (S.TOURING, Actor.DASHBOARD),
        (S.APPLIED, Actor.DASHBOARD),
        (S.WON, Actor.DASHBOARD),
        (S.ARCHIVED, Actor.DASHBOARD),
    ]

    for i, (status, actor) in enumerate(path, start=1):
        store.transition(prop.id, status, actor=actor, at=T0 + i * MINUTE)

    events = store.audit_events(prop.id)
    assert [(e.to_status, e.actor) for e in events[1:]] == path
    assert [e.from_status for e in events] == [None, S.NEW, *(status for status, _ in path[:-1])]


def test_concurrent_clicks_apply_once(
    session_factory: sessionmaker[Session], add_listing: AddListing
) -> None:
    store = SqlPropertyStore(session_factory)
    prop = store.ensure_for_listing(add_listing(), at=T0)
    store.transition(prop.id, S.SUGGESTED, actor=Actor.SYSTEM, at=T0)
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def click() -> None:
        start.wait()
        try:
            store.transition(prop.id, S.APPROVED, actor=Actor.DISCORD, at=T0 + MINUTE)
            outcomes.append("applied")
        except InvalidTransitionError:
            outcomes.append("rejected")

    threads = [threading.Thread(target=click) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["applied", "rejected"]
    assert [e.to_status for e in store.audit_events(prop.id)].count(S.APPROVED) == 1


def test_deleting_a_property_drops_its_audit_and_unlinks_listings(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, add_listing: AddListing
) -> None:
    listing_id = add_listing()
    prop = store.ensure_for_listing(listing_id, at=T0)

    with session_scope(session_factory) as session:
        session.execute(delete(Property).where(Property.id == prop.id))

    with session_scope(session_factory) as session:
        assert session.get_one(Listing, listing_id).property_id is None
        assert session.scalars(select(AuditEvent)).all() == []
    assert store.for_listing(listing_id) is None
    assert store.get(prop.id) is None
