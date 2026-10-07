"""Cross-source grouping through SqlPropertyStore: joining, merging and regrouping."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import GroupingSettings, Settings
from furnest.db.criteria_store import CriteriaStore
from furnest.db.email_store import draft_email
from furnest.db.grouping import resolve
from furnest.db.image_store import SqlImageStore
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import Email, Job, Listing, Note, Property
from furnest.db.property_store import (
    MESSAGE_SYNC_JOB,
    NOTIFY,
    PropertyMergedError,
    SqlPropertyStore,
)
from furnest.db.session import session_scope
from furnest.domain import Actor, Criteria, NormalizedListing, PropertyStatus
from furnest.matching import MatchResult
from furnest.pipeline.properties import PropertyPipeline

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
LATER = T0 + timedelta(hours=1)

ADDRESS = {"line1": "123 N Main St", "city": "Springfield", "region": "IL", "postal_code": "62701"}
LAT, LNG = 39.7817, -89.6501

AddListing = Callable[..., int]


def passing(score: int = 80) -> MatchResult:
    return MatchResult(passed=True, score=score, criteria_version=1)


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlPropertyStore:
    CriteriaStore(session_factory).save(Criteria(), created_by="cli")
    return SqlPropertyStore(session_factory, grouping=GroupingSettings())


@pytest.fixture
def add_listing(session_factory: sessionmaker[Session]) -> AddListing:
    listings = SqlListingStore(session_factory)

    def add(source: str, source_id: str = "1", **fields: Any) -> int:  # noqa: ANN401
        data: dict[str, Any] = {
            "source": source,
            "source_id": source_id,
            "url": f"https://example.com/{source}/{source_id}",
            "title": "Furnished 2BR near downtown, free parking",
            "beds": 2,
            "address": ADDRESS,
            "lat": LAT,
            "lng": LNG,
            **fields,
        }
        listing = NormalizedListing.model_validate(data)
        (listing_id,) = listings.upsert_batch(source, [listing], seen_at=T0).new_ids
        return listing_id

    return add


def jobs(session_factory: sessionmaker[Session], kind: str) -> list[dict[str, Any]]:
    with session_scope(session_factory) as session:
        return [
            dict(job.payload)
            for job in session.scalars(select(Job).where(Job.kind == kind).order_by(Job.id))
        ]


def property_of(session_factory: sessionmaker[Session], listing_id: int) -> int | None:
    with session_scope(session_factory) as session:
        return session.get_one(Listing, listing_id).property_id


def set_message(session_factory: sessionmaker[Session], property_id: int, message: str) -> None:
    with session_scope(session_factory) as session:
        session.execute(
            update(Property)
            .where(Property.id == property_id)
            .values(discord_channel_id="c1", discord_message_id=message)
        )


def hash_photos(session_factory: sessionmaker[Session], phashes: dict[str, str]) -> None:
    images = SqlImageStore(session_factory)
    for url, phash in phashes.items():
        images.record(url, phash=phash, at=T0)


# --- Joining an existing property ---------------------------------------------------


def test_a_listing_of_the_same_unit_joins_the_existing_property(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    first = add_listing("zillow", title="Furnished 2BR near downtown, free parking")
    state = store.record_match(first, passing(), at=T0)
    assert state.status is S.SUGGESTED

    second = add_listing("craigslist", title="2 bed furnished apt - downtown - free parking")
    joined = store.record_match(second, passing(90), at=LATER)

    assert joined.id == state.id
    assert property_of(session_factory, second) == state.id
    # Suggested once; the second source doesn't notify again.
    assert jobs(session_factory, NOTIFY) == [{"property_id": state.id}]
    grouped = [e for e in store.audit_events(state.id) if e.actor_detail == "grouped"]
    assert len(grouped) == 1
    assert grouped[0].actor is Actor.SYSTEM
    assert grouped[0].payload is not None
    assert grouped[0].payload["listing_id"] == second
    assert grouped[0].payload["signals"]["address"] == 1.0
    detail = store.detail(state.id)
    assert detail is not None
    assert [listing.summary.id for listing in detail.listings] == [first, second]


def test_a_decided_property_stays_decided_when_a_duplicate_arrives(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    first = add_listing("zillow")
    state = store.record_match(first, passing(), at=T0)
    store.deny(state.id, actor=Actor.DASHBOARD, at=T0, reason="Too far")

    second = add_listing("craigslist")
    joined = store.record_match(second, passing(), at=LATER)

    assert joined.id == state.id
    assert joined.status is S.DENIED
    assert jobs(session_factory, NOTIFY) == [{"property_id": state.id}]


def test_a_different_unit_gets_its_own_property(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    first = store.record_match(add_listing("zillow"), passing(), at=T0)
    other = add_listing("zillow", "2", beds=3)
    assert store.record_match(other, passing(), at=T0).id != first.id

    far = add_listing("craigslist", lat=LAT + 0.01, address={"city": "Springfield"})
    assert store.record_match(far, passing(), at=T0).id != first.id


def test_without_grouping_every_listing_gets_its_own_property(
    session_factory: sessionmaker[Session], add_listing: AddListing
) -> None:
    CriteriaStore(session_factory).save(Criteria(), created_by="cli")
    for store in (
        SqlPropertyStore(session_factory),
        SqlPropertyStore(session_factory, grouping=GroupingSettings(enabled=False)),
    ):
        source = f"s{id(store)}"
        a = store.record_match(add_listing(source, "a"), passing(), at=T0)
        b = store.record_match(add_listing(source, "b"), passing(), at=T0)
        assert a.id != b.id


def test_grouping_stores_the_listing_address_keys(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    listing_id = add_listing("zillow", address={**ADDRESS, "line2": "Apt 4B"})
    store.ensure_for_listing(listing_id, at=T0)
    with session_scope(session_factory) as session:
        row = session.get_one(Listing, listing_id)
        assert row.address_key == "123 n main st #4b, springfield, il 62701, us"
        assert row.street_key == "123 n main st, 62701"


def test_a_relisted_unit_restores_its_unavailable_property(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    first = add_listing("zillow")
    state = store.record_match(first, passing(), at=T0)
    store.transition(state.id, S.APPROVED, actor=Actor.DASHBOARD, at=T0)
    with session_scope(session_factory) as session:
        session.execute(update(Listing).where(Listing.id == first).values(removed_at=T0))
    assert store.mark_gone([first], at=T0) == [state.id]

    joined = store.record_match(add_listing("craigslist"), passing(), at=LATER)

    assert joined.id == state.id
    assert joined.status is S.APPROVED


# --- The best match -----------------------------------------------------------------


def test_the_property_keeps_its_best_listings_match(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    first = add_listing("zillow")
    state = store.record_match(first, passing(70), at=T0)
    second = add_listing("craigslist")

    assert store.record_match(second, passing(60), at=T0).score == 70
    assert store.record_match(second, passing(85), at=T0).score == 85
    # The same listing's newer result replaces its own, even when lower.
    assert store.record_match(second, passing(65), at=T0).score == 65
    # Another listing only takes over with a better one.
    assert store.record_match(first, passing(64), at=T0).score == 65
    assert store.get(state.id) is not None


# --- Merging properties -------------------------------------------------------------


def two_properties(
    store: SqlPropertyStore,
    add_listing: AddListing,
    session_factory: sessionmaker[Session],
) -> tuple[int, int, int, int]:
    """Two properties of one unit that grouping couldn't tell apart yet: no address,
    and photos not hashed. Returns (listing a, property a, listing b, property b)."""
    a = add_listing(
        "zillow", address={"city": "Springfield"}, images=["https://example.com/p/a1.jpg"]
    )
    b = add_listing(
        "craigslist",
        address={"city": "Springfield"},
        images=["https://example.com/p/b1.jpg"],
        lat=LAT + 0.0001,
    )
    pa = store.record_match(a, passing(70), at=T0).id
    pb = store.record_match(b, passing(85), at=T0).id
    assert pa != pb
    hash_photos(
        session_factory,
        {
            "https://example.com/p/a1.jpg": "00000000000000ff",
            "https://example.com/p/b1.jpg": "00000000000001ff",
        },
    )
    return a, pa, b, pb


def test_regrouping_after_photos_are_hashed_merges_the_properties(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    a, pa, b, pb = two_properties(store, add_listing, session_factory)
    store.transition(pb, S.APPROVED, actor=Actor.DASHBOARD, at=T0)
    store.add_note(pa, "Looks nice", actor=Actor.DASHBOARD, at=T0)
    with session_scope(session_factory) as session:
        draft_email(session, Settings().smtp, to=None, subject="Hi", body="Hello", property_id=pa)

    assert store.regroup([a, b], at=LATER) == [pa]

    # The approved property survives with its status; everything moves to it.
    survivor = store.get(pb)
    assert survivor is not None
    assert survivor.status is S.APPROVED
    assert survivor.score == 85
    absorbed = store.get(pa)
    assert absorbed is not None
    assert absorbed.merged_into_id == pb
    assert absorbed.status is S.SUGGESTED
    assert property_of(session_factory, a) == pb
    with session_scope(session_factory) as session:
        assert {n.property_id for n in session.scalars(select(Note))} == {pb}
        assert {e.property_id for e in session.scalars(select(Email))} == {pb}
        assert resolve(session, pa) is not None
        assert resolve(session, pa).id == pb  # type: ignore[union-attr]
    detail = store.detail(pb)
    assert detail is not None
    assert [listing.summary.id for listing in detail.listings] == [a, b]
    # The survivor's timeline includes the merged property's events.
    assert {e.property_id for e in detail.audit} == {pa, pb}
    merged = [e for e in detail.audit if e.actor_detail == "merged"]
    assert {e.property_id for e in merged} == {pa, pb}
    into = next(e for e in merged if e.property_id == pa)
    assert into.payload is not None
    assert into.payload["merged_into"] == pb
    from_ = next(e for e in merged if e.property_id == pb)
    assert from_.payload is not None
    assert from_.payload["merged_from"] == pa
    assert from_.payload["merged_status"] == "suggested"
    assert from_.payload["signals"]["photos"] == 1.0

    # Nothing left to group: regrouping again is a no-op.
    assert store.regroup([a, b], at=LATER) == []


def test_a_merged_property_leaves_the_views_and_refuses_changes(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    _a, pa, b, pb = two_properties(store, add_listing, session_factory)
    store.regroup([b], at=LATER)

    assert [s.id for s in store.query(now=LATER)] == [pa]
    page = store.search(now=LATER, limit=10)
    assert [row.property.id for row in page.rows] == [pa]
    assert page.total == 1
    assert sum(store.status_counts(now=LATER).values()) == 1
    tombstone = store.detail(pb)
    assert tombstone is not None
    assert tombstone.property.merged_into_id == pa
    assert tombstone.listings == []
    with pytest.raises(PropertyMergedError) as e:
        store.transition(pb, S.APPROVED, actor=Actor.DASHBOARD, at=LATER)
    assert e.value.merged_into == pa
    with pytest.raises(PropertyMergedError):
        store.add_note(pb, "x", actor=Actor.DASHBOARD, at=LATER)
    with pytest.raises(PropertyMergedError):
        store.deny(pb, actor=Actor.DASHBOARD, at=LATER)
    with pytest.raises(PropertyMergedError):
        store.snooze(pb, until=LATER + timedelta(days=1), actor=Actor.DASHBOARD, at=LATER)


def test_merging_syncs_both_messages(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    a, pa, _b, pb = two_properties(store, add_listing, session_factory)
    set_message(session_factory, pa, "m-a")
    set_message(session_factory, pb, "m-b")

    store.regroup([a], at=LATER)

    synced = {
        job["message_id"]: job["property_id"] for job in jobs(session_factory, MESSAGE_SYNC_JOB)
    }
    assert synced == {"m-a": pa, "m-b": pb}


def test_the_survivor_takes_over_the_merged_propertys_message(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    a, pa, _b, pb = two_properties(store, add_listing, session_factory)
    set_message(session_factory, pb, "m-b")

    store.regroup([a], at=LATER)

    survivor, absorbed = store.get(pa), store.get(pb)
    assert survivor is not None
    assert absorbed is not None
    assert survivor.discord_message_id == "m-b"
    assert absorbed.discord_message_id is None
    assert [job["property_id"] for job in jobs(session_factory, MESSAGE_SYNC_JOB)] == [pa]


def test_a_suggested_survivor_without_a_message_is_notified(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    a, pa, _b, _pb = two_properties(store, add_listing, session_factory)
    with session_scope(session_factory) as session:
        session.execute(update(Job).values(status="succeeded"))

    store.regroup([a], at=LATER)

    assert jobs(session_factory, NOTIFY)[-1] == {"property_id": pa}


def test_a_listing_matching_two_properties_merges_them(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    a, pa, b, pb = two_properties(store, add_listing, session_factory)
    c = add_listing(
        "furnishedfinder",
        address={"city": "Springfield"},
        images=["https://example.com/p/c1.jpg", "https://example.com/p/b1.jpg"],
    )
    hash_photos(session_factory, {"https://example.com/p/c1.jpg": "00000000000000ff"})

    pc = store.record_match(c, passing(), at=LATER)

    assert pc.id == pa
    absorbed = store.get(pb)
    assert absorbed is not None
    assert absorbed.merged_into_id == pa
    assert {property_of(session_factory, x) for x in (a, b, c)} == {pa}


def test_regroup_skips_unknown_and_unmatched_listings(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    unmatched = add_listing("zillow")
    assert store.regroup([unmatched, 999], at=T0) == []


def test_regroup_is_off_without_grouping(
    session_factory: sessionmaker[Session], add_listing: AddListing
) -> None:
    store = SqlPropertyStore(session_factory)
    assert store.regroup([add_listing("zillow")], at=T0) == []


def test_the_pipeline_regroups_hashed_listings(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    a, pa, b, pb = two_properties(store, add_listing, session_factory)
    pipeline = PropertyPipeline(session_factory, grouping=GroupingSettings(), clock=lambda: LATER)

    pipeline.regroup([a, b])

    absorbed = store.get(pb)
    assert absorbed is not None
    assert absorbed.merged_into_id == pa
