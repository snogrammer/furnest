"""SqlPropertyStore's pipeline side: matches, disappearance, relisting and snooze."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.criteria_store import CriteriaStore
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import CriteriaVersion, Job, JobStatus, Listing, Property
from furnest.db.property_store import (
    MESSAGE_SYNC_JOB,
    NOTIFY,
    PropertyNotFoundError,
    SnoozeError,
    SqlPropertyStore,
)
from furnest.db.session import session_scope
from furnest.domain import Actor, Criteria, NormalizedListing, PropertyStatus
from furnest.matching import MatchResult
from furnest.worker import queue

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)

AddListing = Callable[..., int]  # (source_id="a") -> listing id


def passing(score: int = 80, version: int = 1) -> MatchResult:
    return MatchResult(passed=True, score=score, criteria_version=version)


def failing(score: int = 20, version: int = 1) -> MatchResult:
    return MatchResult(passed=False, score=score, criteria_version=version)


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlPropertyStore:
    # Matches name criteria versions 1 and 2, which must exist for the foreign key.
    criteria = CriteriaStore(session_factory)
    criteria.save(Criteria(), created_by="cli")
    criteria.save(Criteria(min_beds=2), created_by="cli")
    return SqlPropertyStore(session_factory)


@pytest.fixture
def add_listing(session_factory: sessionmaker[Session]) -> AddListing:
    listings = SqlListingStore(session_factory)

    def add(source_id: str = "a") -> int:
        listing = NormalizedListing.model_validate(
            {
                "source": "test",
                "source_id": source_id,
                "url": f"https://example.com/{source_id}",
                "title": "Furnished place",
            }
        )
        (listing_id,) = listings.upsert_batch("test", [listing], seen_at=T0).new_ids
        return listing_id

    return add


def notify_jobs(session_factory: sessionmaker[Session]) -> list[int]:
    """The property id of every queued notify job, oldest first."""
    with session_scope(session_factory) as session:
        jobs = session.scalars(select(Job).where(Job.kind == NOTIFY).order_by(Job.id))
        return [job.payload["property_id"] for job in jobs]


def finish_jobs(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        session.execute(update(Job).values(status=JobStatus.SUCCEEDED))


def set_removed(
    session_factory: sessionmaker[Session], *listing_ids: int, at: datetime | None = T0
) -> None:
    with session_scope(session_factory) as session:
        session.execute(update(Listing).where(Listing.id.in_(listing_ids)).values(removed_at=at))


def link(session_factory: sessionmaker[Session], listing_id: int, property_id: int) -> None:
    """Group a listing under a property by hand, as P7 will."""
    with session_scope(session_factory) as session:
        session.execute(
            update(Listing).where(Listing.id == listing_id).values(property_id=property_id)
        )


def criteria_id(session_factory: sessionmaker[Session], version: int) -> int:
    with session_scope(session_factory) as session:
        return session.scalars(
            select(CriteriaVersion.id).where(CriteriaVersion.version == version)
        ).one()


# --- Matches ---------------------------------------------------------------------


def test_a_passing_match_suggests_the_property_and_queues_a_notification(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    listing_id = add_listing()

    state = store.record_match(listing_id, passing(score=87), at=T0)

    assert state.status is S.SUGGESTED
    assert state.score == 87
    assert state.criteria_version_id == criteria_id(session_factory, 1)
    assert store.for_listing(listing_id) == state
    events = store.audit_events(state.id)
    assert [(e.from_status, e.to_status, e.actor) for e in events] == [
        (None, S.NEW, Actor.SYSTEM),
        (S.NEW, S.SUGGESTED, Actor.SYSTEM),
    ]
    assert events[1].actor_detail == "match"
    assert events[1].payload == {"score": 87, "criteria_version": 1}
    assert notify_jobs(session_factory) == [state.id]


def test_the_match_result_is_stored_as_json(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    result = passing()
    state = store.record_match(add_listing(), result, at=T0)

    with session_scope(session_factory) as session:
        stored = session.get_one(Property, state.id).match_result
    assert MatchResult.model_validate(stored) == result


def test_a_failing_match_only_scores_a_new_property(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    state = store.record_match(add_listing(), failing(score=12), at=T0)

    assert state.status is S.NEW
    assert state.score == 12
    assert [e.to_status for e in store.audit_events(state.id)] == [S.NEW]
    assert notify_jobs(session_factory) == []


def test_a_new_property_passing_on_a_later_run_is_suggested_then(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    listing_id = add_listing()
    store.record_match(listing_id, failing(), at=T0)

    state = store.record_match(listing_id, passing(version=2), at=T0 + HOUR)

    assert state.status is S.SUGGESTED
    assert state.criteria_version_id == criteria_id(session_factory, 2)
    assert notify_jobs(session_factory) == [state.id]


@pytest.mark.parametrize(
    ("path", "result"),
    [
        ((), passing(score=95, version=2)),  # already suggested: no second notification
        ((S.APPROVED,), failing(score=5, version=2)),
        ((S.DENIED,), passing(score=99, version=2)),
        ((S.APPROVED, S.INQUIRY_DRAFTED), failing(score=5, version=2)),
        ((S.ARCHIVED,), passing(score=99, version=2)),
    ],
)
def test_a_rerun_updates_the_score_without_moving_a_decided_property(
    store: SqlPropertyStore,
    add_listing: AddListing,
    session_factory: sessionmaker[Session],
    path: tuple[PropertyStatus, ...],
    result: MatchResult,
) -> None:
    listing_id = add_listing()
    prop = store.record_match(listing_id, passing(), at=T0)
    for i, status in enumerate(path, start=1):
        store.transition(prop.id, status, actor=Actor.DASHBOARD, at=T0 + i * MINUTE)
    before = store.get(prop.id)
    events_before = store.audit_events(prop.id)
    assert before is not None

    state = store.record_match(listing_id, result, at=T0 + HOUR)

    assert state.status is before.status
    assert state.status_changed_at == before.status_changed_at
    assert state.score == result.score
    assert state.criteria_version_id == criteria_id(session_factory, 2)
    assert store.audit_events(prop.id) == events_before
    assert notify_jobs(session_factory) == [prop.id]


def test_a_match_on_an_unknown_criteria_version_stores_no_version(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    state = store.record_match(add_listing(), failing(version=99), at=T0)

    assert state.criteria_version_id is None
    assert state.score == 20


def test_a_failed_notification_enqueue_leaves_nothing_behind(
    store: SqlPropertyStore,
    add_listing: AddListing,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing_id = add_listing()

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("queue is broken")

    monkeypatch.setattr(queue, "enqueue", broken)
    with pytest.raises(RuntimeError, match="queue is broken"):
        store.record_match(listing_id, passing(), at=T0)

    assert store.for_listing(listing_id) is None
    assert notify_jobs(session_factory) == []


# --- Disappearance and relisting -------------------------------------------------


def test_a_property_becomes_unavailable_when_its_listing_is_gone(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    listing_id = add_listing()
    prop = store.record_match(listing_id, passing(), at=T0)
    set_removed(session_factory, listing_id)

    assert store.mark_gone([listing_id], at=T0 + HOUR) == [prop.id]

    state = store.get(prop.id)
    assert state is not None
    assert state.status is S.UNAVAILABLE
    last = store.audit_events(prop.id)[-1]
    assert (last.from_status, last.actor, last.actor_detail) == (
        S.SUGGESTED,
        Actor.SYSTEM,
        "listings removed",
    )


def test_a_property_stays_while_any_of_its_listings_is_active(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    first, second = add_listing("a"), add_listing("b")
    prop = store.record_match(first, passing(), at=T0)
    link(session_factory, second, prop.id)

    set_removed(session_factory, first)
    assert store.mark_gone([first], at=T0 + HOUR) == []
    assert store.get(prop.id).status is S.SUGGESTED  # type: ignore[union-attr]

    set_removed(session_factory, second)
    assert store.mark_gone([second], at=T0 + 2 * HOUR) == [prop.id]
    assert store.get(prop.id).status is S.UNAVAILABLE  # type: ignore[union-attr]


@pytest.mark.parametrize("path", [(S.DENIED,), (S.ARCHIVED,), (S.DENIED, S.ARCHIVED)])
def test_a_property_that_cannot_become_unavailable_is_left_alone(
    store: SqlPropertyStore,
    add_listing: AddListing,
    session_factory: sessionmaker[Session],
    path: tuple[PropertyStatus, ...],
) -> None:
    listing_id = add_listing()
    prop = store.ensure_for_listing(listing_id, at=T0)
    for i, status in enumerate(path, start=1):
        store.transition(prop.id, status, actor=Actor.DASHBOARD, at=T0 + i * MINUTE)
    set_removed(session_factory, listing_id)

    assert store.mark_gone([listing_id], at=T0 + HOUR) == []
    assert store.get(prop.id).status is path[-1]  # type: ignore[union-attr]


def test_mark_gone_twice_and_without_properties_is_harmless(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    listing_id, orphan = add_listing("a"), add_listing("b")
    store.ensure_for_listing(listing_id, at=T0)
    set_removed(session_factory, listing_id, orphan)

    assert len(store.mark_gone([listing_id, orphan], at=T0 + HOUR)) == 1
    assert store.mark_gone([listing_id, orphan], at=T0 + 2 * HOUR) == []
    assert store.mark_gone([], at=T0 + 2 * HOUR) == []


def test_a_relisted_property_goes_back_to_where_it_was(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    listing_id = add_listing()
    prop = store.record_match(listing_id, passing(), at=T0)
    store.transition(prop.id, S.APPROVED, actor=Actor.DISCORD, at=T0 + MINUTE)
    set_removed(session_factory, listing_id)
    store.mark_gone([listing_id], at=T0 + HOUR)
    set_removed(session_factory, listing_id, at=None)

    assert store.restore_relisted([listing_id], at=T0 + 2 * HOUR) == [prop.id]

    assert store.get(prop.id).status is S.APPROVED  # type: ignore[union-attr]
    assert store.audit_events(prop.id)[-1].actor_detail == "relisted"
    # Only properties that are unavailable move.
    assert store.restore_relisted([listing_id], at=T0 + 3 * HOUR) == []


def test_a_snooze_while_unavailable_does_not_hide_where_to_restore_to(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    listing_id = add_listing()
    prop = store.record_match(listing_id, passing(), at=T0)
    set_removed(session_factory, listing_id)
    store.mark_gone([listing_id], at=T0 + HOUR)
    store.snooze(prop.id, until=T0 + 9 * HOUR, actor=Actor.DISCORD, at=T0 + 2 * HOUR)

    assert store.restore_relisted([listing_id], at=T0 + 3 * HOUR) == [prop.id]
    assert store.get(prop.id).status is S.SUGGESTED  # type: ignore[union-attr]


# --- Snooze ----------------------------------------------------------------------


def test_snooze_sets_the_time_and_records_who_did_it(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    prop = store.record_match(add_listing(), passing(), at=T0)
    until = T0 + 72 * HOUR

    state = store.snooze(prop.id, until=until, actor=Actor.DISCORD, at=T0 + MINUTE, detail="u1")

    assert state.snoozed_until == until
    assert state.status is S.SUGGESTED
    assert state.status_changed_at == prop.status_changed_at
    last = store.audit_events(prop.id)[-1]
    assert (last.from_status, last.to_status, last.actor, last.actor_detail) == (
        S.SUGGESTED,
        S.SUGGESTED,
        Actor.DISCORD,
        "u1",
    )
    assert last.payload == {"snoozed_until": until.isoformat()}


def test_snooze_rejects_a_time_that_is_not_in_the_future(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    prop = store.ensure_for_listing(add_listing(), at=T0)

    with pytest.raises(SnoozeError, match="in the future"):
        store.snooze(prop.id, until=T0, actor=Actor.DASHBOARD, at=T0)
    assert len(store.audit_events(prop.id)) == 1


@pytest.mark.parametrize("final", [S.ARCHIVED, S.LOST])
def test_snooze_rejects_a_finished_property(
    store: SqlPropertyStore, add_listing: AddListing, final: PropertyStatus
) -> None:
    prop = store.ensure_for_listing(add_listing(), at=T0)
    path = [S.ARCHIVED] if final is S.ARCHIVED else [S.SUGGESTED, S.APPROVED]
    if final is S.LOST:
        path += [S.INQUIRY_DRAFTED, S.INQUIRY_SENT, S.LOST]
    for i, status in enumerate(path, start=1):
        store.transition(prop.id, status, actor=Actor.SYSTEM, at=T0 + i * MINUTE)

    with pytest.raises(SnoozeError, match=str(final)):
        store.snooze(prop.id, until=T0 + HOUR, actor=Actor.DASHBOARD, at=T0 + 10 * MINUTE)


def test_snooze_of_a_missing_property_raises(store: SqlPropertyStore) -> None:
    with pytest.raises(PropertyNotFoundError):
        store.snooze(404, until=T0 + HOUR, actor=Actor.DASHBOARD, at=T0)


def test_the_default_view_hides_snoozed_properties_until_they_expire(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    shown = store.record_match(add_listing("a"), passing(), at=T0)
    snoozed = store.record_match(add_listing("b"), passing(), at=T0 + MINUTE)
    store.snooze(snoozed.id, until=T0 + HOUR, actor=Actor.DISCORD, at=T0 + MINUTE)

    assert [p.id for p in store.query(now=T0 + 30 * MINUTE)] == [shown.id]
    assert [p.id for p in store.query(now=T0 + 30 * MINUTE, include_snoozed=True)] == [
        snoozed.id,
        shown.id,
    ]
    assert [p.id for p in store.query(now=T0 + HOUR)] == [snoozed.id, shown.id]


def test_the_view_filters_by_status(store: SqlPropertyStore, add_listing: AddListing) -> None:
    suggested = store.record_match(add_listing("a"), passing(), at=T0)
    store.record_match(add_listing("b"), failing(), at=T0)

    assert [p.id for p in store.query(now=T0, statuses=[S.SUGGESTED])] == [suggested.id]
    assert store.query(now=T0, statuses=[]) == []


def test_expired_snoozes_are_cleared_and_suggested_ones_notified_again(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    waiting = store.record_match(add_listing("a"), passing(), at=T0)
    decided = store.record_match(add_listing("b"), passing(), at=T0)
    store.transition(decided.id, S.APPROVED, actor=Actor.DISCORD, at=T0)
    later = store.record_match(add_listing("c"), passing(), at=T0)
    store.snooze(waiting.id, until=T0 + HOUR, actor=Actor.DISCORD, at=T0)
    store.snooze(decided.id, until=T0 + HOUR, actor=Actor.DISCORD, at=T0)
    store.snooze(later.id, until=T0 + 5 * HOUR, actor=Actor.DISCORD, at=T0)
    finish_jobs(session_factory)  # the first notifications went out

    assert store.unsnooze_expired(now=T0 + HOUR) == [waiting.id, decided.id]

    assert store.get(waiting.id).snoozed_until is None  # type: ignore[union-attr]
    assert store.get(decided.id).snoozed_until is None  # type: ignore[union-attr]
    assert store.get(later.id).snoozed_until == T0 + 5 * HOUR  # type: ignore[union-attr]
    assert notify_jobs(session_factory) == [waiting.id, decided.id, later.id, waiting.id]
    last = store.audit_events(waiting.id)[-1]
    assert (last.actor, last.payload) == (Actor.SYSTEM, {"unsnoozed": True})
    assert store.unsnooze_expired(now=T0 + 2 * HOUR) == []


def test_a_notification_still_waiting_to_run_is_not_queued_twice(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    waiting = store.record_match(add_listing("a"), passing(), at=T0)
    store.snooze(waiting.id, until=T0 + HOUR, actor=Actor.DISCORD, at=T0)

    store.unsnooze_expired(now=T0 + HOUR)

    assert notify_jobs(session_factory) == [waiting.id]


def test_a_running_notification_does_not_stop_the_next_one(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    waiting = store.record_match(add_listing("a"), passing(), at=T0)
    store.snooze(waiting.id, until=T0 + HOUR, actor=Actor.DISCORD, at=T0)
    with session_scope(session_factory) as session:
        session.execute(update(Job).values(status=JobStatus.RUNNING))

    store.unsnooze_expired(now=T0 + HOUR)

    assert notify_jobs(session_factory) == [waiting.id, waiting.id]


def test_an_unavailable_property_with_no_audit_of_it_is_not_restored(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    listing_id = add_listing()
    prop = store.ensure_for_listing(listing_id, at=T0)
    with session_scope(session_factory) as session:  # written outside the store
        session.execute(update(Property).where(Property.id == prop.id).values(status=S.UNAVAILABLE))

    assert store.restore_relisted([listing_id], at=T0 + HOUR) == []


# --- Manual contact -------------------------------------------------------------


def _listing(source_id: str, email: str | None) -> NormalizedListing:
    return NormalizedListing.model_validate(
        {
            "source": "test",
            "source_id": source_id,
            "url": f"https://example.com/{source_id}",
            "title": "Furnished place",
            "contact": {"email": email, "platform_only": email is None},
        }
    )


def _sync_jobs(session_factory: sessionmaker[Session]) -> int:
    with session_scope(session_factory) as session:
        jobs = session.scalars(select(Job).where(Job.kind == MESSAGE_SYNC_JOB))
        return len(list(jobs))


@pytest.mark.parametrize(("email", "manual"), [(None, True), ("lister@example.com", False)])
def test_a_property_needs_manual_contact_when_its_listing_has_no_email(
    session_factory: sessionmaker[Session],
    store: SqlPropertyStore,
    email: str | None,
    manual: bool,
) -> None:
    (listing_id,) = (
        SqlListingStore(session_factory)
        .upsert_batch("test", [_listing("a", email)], seen_at=T0)
        .new_ids
    )

    created = store.ensure_for_listing(listing_id, at=T0)
    matched = store.record_match(listing_id, passing(), at=T0)

    assert (created.manual_contact_required, matched.manual_contact_required) == (manual, manual)


def test_the_flag_follows_the_listing_and_redraws_the_message(
    session_factory: sessionmaker[Session], store: SqlPropertyStore
) -> None:
    listings = SqlListingStore(session_factory)
    (listing_id,) = listings.upsert_batch("test", [_listing("a", None)], seen_at=T0).new_ids
    state = store.record_match(listing_id, passing(), at=T0)
    store.attach_message(state.id, channel_id="1", message_id="2", rendered=state)
    synced = _sync_jobs(session_factory)

    listings.upsert_batch("test", [_listing("a", "lister@example.com")], seen_at=T0 + HOUR)
    after = store.record_match(listing_id, passing(), at=T0 + HOUR)

    assert after.manual_contact_required is False
    assert _sync_jobs(session_factory) == synced + 1
    store.record_match(listing_id, passing(), at=T0 + 2 * HOUR)
    assert _sync_jobs(session_factory) == synced + 1
