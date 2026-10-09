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
from furnest.db.models import Email, Job, Listing, ListingSeparation, Note, Property
from furnest.db.property_store import (
    MESSAGE_SYNC_JOB,
    NOTIFY,
    GroupingError,
    ListingNotFoundError,
    PropertyMergedError,
    PropertyNotFoundError,
    SqlPropertyStore,
)
from furnest.db.session import session_scope
from furnest.domain import Actor, Criteria, NormalizedListing, OwnerType, PropertyStatus
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

    def add(
        source: str,
        source_id: str = "1",
        seen_at: datetime = T0,
        **fields: Any,  # noqa: ANN401
    ) -> int:
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
        (listing_id,) = listings.upsert_batch(source, [listing], seen_at=seen_at).new_ids
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

    second = add_listing("craigslist", seen_at=LATER)
    joined = store.record_match(second, passing(), at=LATER)

    assert joined.id == state.id
    assert joined.status is S.APPROVED
    events = store.audit_events(state.id)
    assert [e.actor_detail for e in events][-2:] == ["grouped", "relisted"]
    relisted = events[-1]
    assert (relisted.from_status, relisted.to_status) == (S.UNAVAILABLE, S.APPROVED)
    assert relisted.payload is not None
    assert relisted.payload["listing_id"] == second
    assert relisted.payload["gone_since"] == T0.isoformat()
    # Restored once: the relisting, not the grouping, moved it.
    assert sum(e.to_status is S.APPROVED for e in events) == 2


def test_a_repost_of_a_denied_unit_is_recorded_as_relisted(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    price = {"amount": {"amount_minor": 240000}, "period": "monthly"}
    first = add_listing("craigslist", "1", price=price)
    state = store.record_match(first, passing(), at=T0)
    store.deny(state.id, actor=Actor.DASHBOARD, at=T0, reason="Too pricey")
    with session_scope(session_factory) as session:
        session.execute(update(Listing).where(Listing.id == first).values(removed_at=T0))
    assert store.mark_gone([first], at=T0) == []

    cheaper = {"amount": {"amount_minor": 210000}, "period": "monthly"}
    second = add_listing("craigslist", "2", seen_at=LATER, price=cheaper)
    joined = store.record_match(second, passing(), at=LATER)

    assert joined.id == state.id
    assert joined.status is S.DENIED
    events = store.audit_events(state.id)
    assert [e.actor_detail for e in events][-3:] == ["listings removed", "grouped", "relisted"]
    assert events[-1].from_status == events[-1].to_status == S.DENIED
    assert events[-1].payload == {
        "listing_id": second,
        "gone_since": T0.isoformat(),
        "price_now_minor": 210000,
        "currency": "USD",
        "price_before_minor": 240000,
    }


def test_a_listing_joining_an_active_unit_is_not_a_relisting(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    state = store.record_match(add_listing("zillow"), passing(), at=T0)
    store.record_match(add_listing("craigslist"), passing(), at=LATER)

    assert "relisted" not in [e.actor_detail for e in store.audit_events(state.id)]


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


# --- Fixing grouping by hand ---------------------------------------------------------


def separations(session_factory: sessionmaker[Session]) -> set[tuple[int, int]]:
    with session_scope(session_factory) as session:
        return {
            (row.listing_id, row.other_listing_id)
            for row in session.scalars(select(ListingSeparation))
        }


def grouped_pair(store: SqlPropertyStore, add_listing: AddListing) -> tuple[int, int, int]:
    """Two listings grouped under one property, the second with the better match.
    Returns (first listing, second listing, property)."""
    first = add_listing("zillow")
    second = add_listing("craigslist")
    prop = store.record_match(first, passing(70), at=T0).id
    assert store.record_match(second, passing(90), at=T0).id == prop
    return first, second, prop


def test_merging_by_hand_moves_everything_and_keeps_the_survivors_status(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    a, pa, b, pb = two_properties(store, add_listing, session_factory)
    store.transition(pb, S.APPROVED, actor=Actor.DASHBOARD, at=T0)
    store.add_note(pb, "Called them", actor=Actor.DASHBOARD, at=T0)

    state = store.merge_properties(pa, pb, actor=Actor.DASHBOARD, at=LATER)

    # The person chose pa: it survives in its own status, with the better score.
    assert state.id == pa
    assert state.status is S.SUGGESTED
    assert state.score == 85
    absorbed = store.get(pb)
    assert absorbed is not None
    assert absorbed.merged_into_id == pa
    assert absorbed.status is S.APPROVED
    assert {property_of(session_factory, x) for x in (a, b)} == {pa}
    with session_scope(session_factory) as session:
        assert {n.property_id for n in session.scalars(select(Note))} == {pa}
    merged = [e for e in store.audit_events(pa) if e.actor_detail == "merged"]
    assert {e.property_id for e in merged} == {pa, pb}
    assert {e.actor for e in merged} == {Actor.DASHBOARD}
    assert all(e.payload is not None and e.payload["manual"] is True for e in merged)
    survivor_event = next(e for e in merged if e.property_id == pa)
    assert survivor_event.payload is not None
    assert survivor_event.payload["merged_status"] == "approved"
    assert [s.id for s in store.query(now=LATER)] == [pa]


def test_a_manual_merge_is_not_undone_by_grouping(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    # No photos in common and different titles: grouping would never merge these.
    a = add_listing("zillow", address={"city": "Springfield"}, title="Loft")
    b = add_listing("craigslist", address={"city": "Springfield"}, title="Cabin", beds=None)
    pa = store.record_match(a, passing(), at=T0).id
    pb = store.record_match(b, passing(), at=T0).id
    assert pa != pb

    store.merge_properties(pa, pb, actor=Actor.DASHBOARD, at=LATER)

    assert store.regroup([a, b], at=LATER) == []
    assert {property_of(session_factory, x) for x in (a, b)} == {pa}


def test_merging_by_hand_refuses_what_cant_be_merged(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    _a, pa, _b, pb = two_properties(store, add_listing, session_factory)

    with pytest.raises(GroupingError, match="already this property"):
        store.merge_properties(pa, pa, actor=Actor.DASHBOARD, at=LATER)
    with pytest.raises(PropertyNotFoundError) as missing:
        store.merge_properties(pa, 999, actor=Actor.DASHBOARD, at=LATER)
    assert missing.value.property_id == 999
    with pytest.raises(PropertyNotFoundError):
        store.merge_properties(999, pa, actor=Actor.DASHBOARD, at=LATER)

    store.merge_properties(pa, pb, actor=Actor.DASHBOARD, at=LATER)
    # pb now stands for pa.
    with pytest.raises(GroupingError):
        store.merge_properties(pa, pb, actor=Actor.DASHBOARD, at=LATER)
    with pytest.raises(PropertyMergedError) as merged:
        store.merge_properties(pb, pa, actor=Actor.DASHBOARD, at=LATER)
    assert merged.value.merged_into == pa


def test_merging_into_an_unavailable_property_with_an_active_listing_restores_it(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    a, pa, _b, pb = two_properties(store, add_listing, session_factory)
    assert store.mark_gone([a], at=T0) == []  # still active
    with session_scope(session_factory) as session:
        session.execute(update(Listing).where(Listing.id == a).values(removed_at=T0))
    assert store.mark_gone([a], at=T0) == [pa]

    state = store.merge_properties(pa, pb, actor=Actor.DASHBOARD, at=LATER)

    assert state.status is S.SUGGESTED


def test_splitting_a_listing_off_gives_it_a_new_property(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    first, second, prop = grouped_pair(store, add_listing)
    assert store.get(prop).score == 90  # type: ignore[union-attr]

    new = store.split_listing(prop, second, actor=Actor.DASHBOARD, at=LATER)

    assert new.id != prop
    assert new.status is S.NEW
    assert new.score is None
    assert new.address == store.get(prop).address  # type: ignore[union-attr]
    assert property_of(session_factory, second) == new.id
    assert property_of(session_factory, first) == prop
    assert separations(session_factory) == {(first, second)}
    # The old property took its score from the split listing: dropped until rescored.
    old = store.get(prop)
    assert old is not None
    assert old.status is S.SUGGESTED
    assert old.score is None
    assert jobs(session_factory, "rerun_criteria") == [
        {"criteria_version": 1, "listing_ids": [second, first]}
    ]
    split = [e for e in store.audit_events(prop) if e.actor_detail == "split"]
    assert len(split) == 1
    assert split[0].actor is Actor.DASHBOARD
    assert split[0].payload == {"listing_id": second, "split_into": new.id}
    created = store.audit_events(new.id)[0]
    assert created.from_status is None
    assert created.actor is Actor.DASHBOARD
    assert created.payload == {"listing_id": second, "split_from": prop}

    # The queued rescore lands on each listing's own property.
    store.record_match(second, passing(90), at=LATER)
    store.record_match(first, passing(70), at=LATER)
    assert store.get(new.id).status is S.SUGGESTED  # type: ignore[union-attr]
    assert store.get(new.id).score == 90  # type: ignore[union-attr]
    assert store.get(prop).score == 70  # type: ignore[union-attr]
    stored = SqlListingStore(session_factory).many([second, first, 999])
    assert [item.id for item in stored] == [first, second]


def test_a_split_is_not_undone_by_grouping(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    first, second, prop = grouped_pair(store, add_listing)
    new = store.split_listing(prop, second, actor=Actor.DASHBOARD, at=LATER).id

    assert store.regroup([first, second], at=LATER) == []
    assert property_of(session_factory, first) == prop
    assert property_of(session_factory, second) == new

    # A third listing of the unit matches both halves, but joins only one of them.
    third = add_listing("furnishedfinder")
    joined = store.record_match(third, passing(), at=LATER).id
    assert joined in {prop, new}
    assert store.get(prop).merged_into_id is None  # type: ignore[union-attr]
    assert store.get(new).merged_into_id is None  # type: ignore[union-attr]
    assert property_of(session_factory, first) != property_of(session_factory, second)


def test_merging_by_hand_forgets_the_split(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    first, second, prop = grouped_pair(store, add_listing)
    new = store.split_listing(prop, second, actor=Actor.DASHBOARD, at=LATER).id

    store.merge_properties(prop, new, actor=Actor.DASHBOARD, at=LATER)

    assert separations(session_factory) == set()
    assert {property_of(session_factory, x) for x in (first, second)} == {prop}


def test_splitting_refuses_what_cant_be_split(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    a, pa, b, pb = two_properties(store, add_listing, session_factory)

    with pytest.raises(GroupingError, match="only listing"):
        store.split_listing(pa, a, actor=Actor.DASHBOARD, at=LATER)
    with pytest.raises(ListingNotFoundError):
        store.split_listing(pa, b, actor=Actor.DASHBOARD, at=LATER)
    with pytest.raises(ListingNotFoundError):
        store.split_listing(pa, 999, actor=Actor.DASHBOARD, at=LATER)
    with pytest.raises(PropertyNotFoundError):
        store.split_listing(999, a, actor=Actor.DASHBOARD, at=LATER)
    store.merge_properties(pa, pb, actor=Actor.DASHBOARD, at=LATER)
    with pytest.raises(PropertyMergedError):
        store.split_listing(pb, b, actor=Actor.DASHBOARD, at=LATER)
    assert separations(session_factory) == set()


def test_splitting_off_the_last_active_listing_makes_the_property_unavailable(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    first, second, prop = grouped_pair(store, add_listing)
    with session_scope(session_factory) as session:
        session.execute(update(Listing).where(Listing.id == first).values(removed_at=T0))

    store.split_listing(prop, second, actor=Actor.DASHBOARD, at=LATER)

    assert store.get(prop).status is S.UNAVAILABLE  # type: ignore[union-attr]


def test_splitting_without_criteria_queues_no_rescore(
    session_factory: sessionmaker[Session], add_listing: AddListing
) -> None:
    store = SqlPropertyStore(session_factory, grouping=GroupingSettings())
    _first, second, prop = grouped_pair(store, add_listing)

    store.split_listing(prop, second, actor=Actor.DASHBOARD, at=LATER)

    assert jobs(session_factory, "rerun_criteria") == []


# --- Owner type across merges and splits --------------------------------------------


def set_override(
    session_factory: sessionmaker[Session], property_id: int, kind: OwnerType | None
) -> None:
    with session_scope(session_factory) as session:
        session.execute(
            update(Property).where(Property.id == property_id).values(owner_type_override=kind)
        )


def test_grouping_keeps_the_owner_type_of_the_primary_listing(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    first = add_listing("zillow", owner_type="agent")
    second = add_listing("craigslist", owner_type="individual")

    prop = store.record_match(first, passing(70), at=T0)
    joined = store.record_match(second, passing(90), at=T0)

    # The first (lowest-id, active) listing stays primary, so its owner type stands.
    assert joined.id == prop.id
    assert joined.owner_type is OwnerType.AGENT


def test_a_merge_carries_the_override_to_a_survivor_without_one(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    _, pa, _, pb = two_properties(store, add_listing, session_factory)
    set_override(session_factory, pb, OwnerType.INDIVIDUAL)

    state = store.merge_properties(pa, pb, actor=Actor.DASHBOARD, at=LATER)

    assert state.owner_type_override is OwnerType.INDIVIDUAL
    assert state.owner_type is OwnerType.INDIVIDUAL


def test_a_merge_keeps_the_survivors_own_override(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    _, pa, _, pb = two_properties(store, add_listing, session_factory)
    set_override(session_factory, pa, OwnerType.AGENT)
    set_override(session_factory, pb, OwnerType.INDIVIDUAL)

    state = store.merge_properties(pa, pb, actor=Actor.DASHBOARD, at=LATER)

    assert state.owner_type_override is OwnerType.AGENT
    assert state.owner_type is OwnerType.AGENT


def test_a_split_listing_starts_from_its_own_owner_type(
    store: SqlPropertyStore, add_listing: AddListing, session_factory: sessionmaker[Session]
) -> None:
    first = add_listing("zillow", owner_type="property_manager")
    second = add_listing("craigslist", owner_type="individual")
    prop = store.record_match(first, passing(70), at=T0).id
    assert store.record_match(second, passing(90), at=T0).id == prop
    set_override(session_factory, prop, OwnerType.AGENT)

    new = store.split_listing(prop, second, actor=Actor.DASHBOARD, at=LATER)

    # The override was about the unit the person was looking at; the split listing is
    # a different unit, so it starts from what its own adapter said.
    assert (new.owner_type, new.owner_type_override) == (OwnerType.INDIVIDUAL, None)
    old = store.get(prop)
    assert old is not None
    assert (old.owner_type, old.owner_type_override) == (OwnerType.AGENT, OwnerType.AGENT)


def test_splitting_the_primary_listing_re_derives_the_old_property(
    store: SqlPropertyStore, add_listing: AddListing
) -> None:
    first = add_listing("zillow", owner_type="property_manager")
    second = add_listing("craigslist", owner_type="individual")
    prop = store.record_match(first, passing(70), at=T0).id
    assert store.record_match(second, passing(90), at=T0).id == prop
    assert store.get(prop).owner_type is OwnerType.PROPERTY_MANAGER  # type: ignore[union-attr]

    store.split_listing(prop, first, actor=Actor.DASHBOARD, at=LATER)

    assert store.get(prop).owner_type is OwnerType.INDIVIDUAL  # type: ignore[union-attr]
