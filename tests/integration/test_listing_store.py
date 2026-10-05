"""SqlListingStore on a migrated database: upsert, price history, disappearance, runs."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.listing_store import (
    MAX_ERROR_LENGTH,
    RunCounts,
    SqlListingStore,
    content_hash,
)
from furnest.db.models import Listing, SourceRunStatus
from furnest.db.session import session_scope
from furnest.domain import Amenity, AmenityResult, NormalizedListing
from furnest.domain.money import Money
from furnest.domain.periods import RentPeriod
from furnest.domain.price import Price

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def price(dollars: int, period: RentPeriod = RentPeriod.MONTHLY) -> Price:
    return Price(amount=Money.from_major(dollars), period=period)


def listing(source_id: str = "a", *, source: str = "test", **fields: object) -> NormalizedListing:
    values: dict[str, Any] = {
        "source": source,
        "source_id": source_id,
        "url": f"https://example.com/{source_id}",
        "title": "Furnished place",
        "price": price(2000),
        "images": ["https://example.com/1.jpg", "https://example.com/2.jpg"],
    }
    values.update(fields)
    return NormalizedListing.model_validate(values)


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlListingStore:
    return SqlListingStore(session_factory)


def test_insert_stores_listing_images_and_first_price(store: SqlListingStore) -> None:
    item = listing(amenities=["wifi", "ac"], beds=2, lat=1.5, lng=2.5)

    result = store.upsert_batch("test", [item], seen_at=T0)

    assert len(result.new_ids) == 1
    assert result.updated_ids == result.relisted_ids == ()
    assert result.price_changes == 1
    (listing_id,) = result.new_ids
    assert result.seen_ids == {listing_id}
    assert result.changed_ids == (listing_id,)
    stored = store.get(listing_id)
    assert stored is not None
    assert stored.listing == item.model_copy(update={"first_seen": T0, "last_seen": T0})
    assert stored.property_id is None
    assert stored.missed_runs == 0
    assert stored.removed_at is None
    assert stored.amenities is None
    assert store.images(listing_id) == ["https://example.com/1.jpg", "https://example.com/2.jpg"]
    assert [p.price for p in store.price_history(listing_id)] == [price(2000)]
    assert store.find("test", "a") == stored
    assert store.find("test", "missing") is None
    assert store.get(listing_id + 100) is None


def test_reseen_unchanged_listing_only_moves_last_seen(store: SqlListingStore) -> None:
    (listing_id,) = store.upsert_batch("test", [listing()], seen_at=T0).new_ids

    result = store.upsert_batch(
        "test",
        [listing(raw={"noise": 1}, first_seen=T0, last_seen=T0 + 9 * HOUR)],
        seen_at=T0 + HOUR,
    )

    assert result.seen_ids == {listing_id}
    assert result.new_ids == result.updated_ids == result.changed_ids == ()
    assert result.price_changes == 0
    stored = store.get(listing_id)
    assert stored is not None
    assert (stored.listing.first_seen, stored.listing.last_seen) == (T0, T0 + HOUR)
    assert len(store.price_history(listing_id)) == 1


def test_changed_listing_is_rewritten_and_images_replaced(store: SqlListingStore) -> None:
    (listing_id,) = store.upsert_batch("test", [listing()], seen_at=T0).new_ids

    changed = listing(title="Now with a view", images=["https://example.com/3.jpg"])
    result = store.upsert_batch("test", [changed], seen_at=T0 + HOUR)

    assert result.updated_ids == (listing_id,)
    assert result.changed_ids == (listing_id,)
    assert result.price_changes == 0
    stored = store.get(listing_id)
    assert stored is not None
    assert stored.listing.title == "Now with a view"
    assert stored.listing.first_seen == T0
    assert store.images(listing_id) == ["https://example.com/3.jpg"]


def test_price_change_adds_history_rows_only_when_it_differs(store: SqlListingStore) -> None:
    (listing_id,) = store.upsert_batch("test", [listing()], seen_at=T0).new_ids

    store.upsert_batch("test", [listing(price=price(1800))], seen_at=T0 + HOUR)
    store.upsert_batch("test", [listing(price=None)], seen_at=T0 + 2 * HOUR)
    store.upsert_batch("test", [listing(price=price(1800))], seen_at=T0 + 3 * HOUR)
    store.upsert_batch(
        "test", [listing(price=price(60, RentPeriod.NIGHTLY))], seen_at=T0 + 4 * HOUR
    )

    history = store.price_history(listing_id)
    assert [(p.price, p.observed_at) for p in history] == [
        (price(2000), T0),
        (price(1800), T0 + HOUR),
        (price(60, RentPeriod.NIGHTLY), T0 + 4 * HOUR),
    ]


def test_price_range_round_trips(store: SqlListingStore) -> None:
    quoted = Price(amount=Money.from_major(1200), amount_max=Money.from_major(1500))
    (listing_id,) = store.upsert_batch("test", [listing(price=quoted)], seen_at=T0).new_ids

    assert [p.price for p in store.price_history(listing_id)] == [quoted]


def test_listing_without_price_has_no_history(store: SqlListingStore) -> None:
    result = store.upsert_batch("test", [listing(price=None)], seen_at=T0)

    assert result.price_changes == 0
    assert store.price_history(result.new_ids[0]) == []


def test_missing_listing_is_removed_only_after_runs_and_hours(store: SqlListingStore) -> None:
    gone, kept = store.upsert_batch("test", [listing("a"), listing("b")], seen_at=T0).new_ids

    def run(at: datetime) -> list[int]:
        seen = store.upsert_batch("test", [listing("b")], seen_at=at).seen_ids
        return store.mark_missing("test", seen, at=at, after_runs=3, after_hours=24)

    # Three missed runs within the hour: enough runs, too soon.
    assert [run(T0 + n * timedelta(minutes=10)) for n in (1, 2, 3)] == [[], [], []]
    stored = store.get(gone)
    assert stored is not None
    assert (stored.missed_runs, stored.removed_at) == (3, None)

    # A day later the next missed run removes it.
    removed_at = T0 + 25 * HOUR
    assert run(removed_at) == [gone]
    stored = store.get(gone)
    assert stored is not None
    assert stored.removed_at == removed_at
    assert [s.id for s in store.active()] == [kept]
    # Already removed: not counted or returned again.
    assert run(removed_at + HOUR) == []


def test_long_absence_alone_does_not_remove(store: SqlListingStore) -> None:
    (listing_id,) = store.upsert_batch("test", [listing()], seen_at=T0).new_ids

    removed = store.mark_missing("test", [], at=T0 + 100 * HOUR, after_runs=3, after_hours=24)

    assert removed == []
    stored = store.get(listing_id)
    assert stored is not None
    assert stored.missed_runs == 1


def test_relisted_listing_is_active_again(store: SqlListingStore) -> None:
    (listing_id,) = store.upsert_batch("test", [listing()], seen_at=T0).new_ids
    store.mark_missing("test", [], at=T0 + 48 * HOUR, after_runs=1, after_hours=24)

    result = store.upsert_batch("test", [listing()], seen_at=T0 + 50 * HOUR)

    assert result.relisted_ids == (listing_id,)
    assert result.updated_ids == ()
    assert result.changed_ids == (listing_id,)
    stored = store.get(listing_id)
    assert stored is not None
    assert (stored.removed_at, stored.missed_runs) == (None, 0)


def test_relisted_and_changed_counts_once(store: SqlListingStore) -> None:
    (listing_id,) = store.upsert_batch("test", [listing()], seen_at=T0).new_ids
    store.mark_missing("test", [], at=T0 + 48 * HOUR, after_runs=1, after_hours=24)

    result = store.upsert_batch("test", [listing(title="Back")], seen_at=T0 + 50 * HOUR)

    assert result.relisted_ids == (listing_id,)
    assert result.updated_ids == ()
    assert result.changed_ids == (listing_id,)


def test_other_sources_are_untouched(store: SqlListingStore) -> None:
    (other,) = store.upsert_batch("other", [listing(source="other")], seen_at=T0).new_ids
    (mine,) = store.upsert_batch("test", [listing()], seen_at=T0).new_ids

    assert other != mine
    store.mark_missing("test", [], at=T0 + 48 * HOUR, after_runs=1, after_hours=24)

    assert [s.id for s in store.active()] == [other]
    assert [s.id for s in store.active("other")] == [other]
    assert store.active("test") == []


def test_batch_rejects_listings_from_another_source(store: SqlListingStore) -> None:
    with pytest.raises(ValueError, match="'other'"):
        store.upsert_batch("test", [listing(source="other")], seen_at=T0)


def test_repeated_source_id_in_a_batch_keeps_the_last(store: SqlListingStore) -> None:
    result = store.upsert_batch(
        "test", [listing(title="First"), listing(title="Second")], seen_at=T0
    )

    stored = store.get(result.new_ids[0])
    assert len(result.new_ids) == 1
    assert stored is not None
    assert stored.listing.title == "Second"


def test_empty_batch_does_nothing(store: SqlListingStore) -> None:
    result = store.upsert_batch("test", [], seen_at=T0)

    assert result.seen_ids == frozenset()
    assert result.changed_ids == ()


def test_source_and_source_id_are_unique(session_factory: sessionmaker[Session]) -> None:
    item = listing()

    def row() -> Listing:
        return Listing(
            source="test",
            source_id="a",
            url=str(item.url),
            title=item.title,
            data={},
            content_hash=content_hash(item),
            first_seen=T0,
            last_seen=T0,
        )

    with session_scope(session_factory) as session:
        session.add(row())
    with pytest.raises(IntegrityError), session_scope(session_factory) as session:
        session.add(row())


def test_content_hash_ignores_timestamps_raw_and_amenity_order() -> None:
    base = listing(amenities=["wifi", "ac"])

    same = listing(amenities=["ac", "wifi"], raw={"x": 1}, first_seen=T0, last_seen=T0 + HOUR)

    assert content_hash(same) == content_hash(base)
    assert content_hash(listing(title="Other")) != content_hash(base)


def test_set_amenities_round_trips(store: SqlListingStore) -> None:
    (listing_id,) = store.upsert_batch("test", [listing()], seen_at=T0).new_ids
    result = AmenityResult(
        present=frozenset({Amenity.WIFI}), absent=frozenset({Amenity.FREE_PARKING})
    )

    store.set_amenities(listing_id, result)

    stored = store.get(listing_id)
    assert stored is not None
    assert stored.amenities == result


def test_source_runs_start_and_finish(store: SqlListingStore) -> None:
    ok = store.start_run("test", at=T0)
    failed = store.start_run("test", at=T0 + HOUR)
    store.start_run("other", at=T0 + HOUR)

    running = store.runs("test")[0]
    assert (running.id, running.status, running.finished_at) == (
        failed,
        SourceRunStatus.RUNNING,
        None,
    )

    store.finish_run(ok, status=SourceRunStatus.SUCCESS, at=T0 + HOUR, counts=RunCounts(5, 2, 1, 1))
    store.finish_run(failed, status=SourceRunStatus.FAILED, at=T0 + 2 * HOUR, error="x" * 5000)

    newest, oldest = store.runs("test")
    assert (oldest.id, oldest.status, oldest.counts) == (
        ok,
        SourceRunStatus.SUCCESS,
        RunCounts(found=5, new=2, updated=1, removed=1),
    )
    assert (newest.status, newest.finished_at) == (SourceRunStatus.FAILED, T0 + 2 * HOUR)
    assert newest.error == "x" * MAX_ERROR_LENGTH
    assert len(store.runs()) == 3
    assert len(store.runs(limit=1)) == 1


def test_failure_counts_count_failed_runs_per_source(store: SqlListingStore) -> None:
    for source, status in [
        ("a", SourceRunStatus.FAILED),
        ("a", SourceRunStatus.FAILED),
        ("a", SourceRunStatus.SUCCESS),
        ("b", SourceRunStatus.SUCCESS),
        ("c", SourceRunStatus.FAILED),
    ]:
        store.finish_run(store.start_run(source, at=T0), status=status, at=T0)
    store.start_run("d", at=T0)  # still running

    assert store.failure_counts() == {"a": 2, "c": 1}
