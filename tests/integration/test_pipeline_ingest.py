"""ListingIngestor: one source run from search through storage to matching."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import SourcesSettings
from furnest.db.criteria_store import CriteriaStore
from furnest.db.listing_store import RunCounts, SqlListingStore
from furnest.db.models import SourceRunStatus
from furnest.domain import Amenity, AmenityResult, Criteria, NormalizedListing
from furnest.matching import MatchResult
from furnest.matching.amenities import KeywordAmenityExtractor
from furnest.pipeline.ingest import ListingIngestor
from furnest.sources.base import AdapterConfig, SourceAdapter

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def listing(source_id: str, beds: int = 2, description: str = "") -> NormalizedListing:
    return NormalizedListing(
        source="fake",
        source_id=source_id,
        url="https://example.com/" + source_id,  # type: ignore[arg-type]
        title="Furnished place",
        description=description,
        beds=beds,
    )


class FakeAdapter(SourceAdapter):
    name: ClassVar[str] = "fake"
    display_name: ClassVar[str] = "Fake"

    def __init__(self) -> None:
        super().__init__(
            AdapterConfig(
                name="fake",
                enabled=True,
                enabled_pinned=False,
                interval_minutes=60,
                jitter_seconds=0,
                requests_per_minute=6,
                user_agent="furnest-test",
                request_timeout_seconds=5,
            )
        )
        self.results: list[NormalizedListing] = []
        self.error: Exception | None = None
        self.searched_with: list[Criteria] = []

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        self.searched_with.append(criteria)
        if self.error is not None:
            raise self.error
        return list(self.results)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class Recorder:
    def __init__(self) -> None:
        self.results: list[tuple[str, MatchResult]] = []

    def __call__(self, listing: NormalizedListing, result: MatchResult) -> None:
        self.results.append((listing.source_id, result))


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def ingestor(
    session_factory: sessionmaker[Session], clock: Clock, recorder: Recorder
) -> Iterator[ListingIngestor]:
    CriteriaStore(session_factory).save(
        Criteria(min_beds=2, furnished_required=False), created_by="cli"
    )
    yield ListingIngestor(
        session_factory,
        extractor=KeywordAmenityExtractor(),
        settings=SourcesSettings(remove_after_missed_runs=1, remove_after_hours=1),
        record=recorder,
        clock=clock,
    )


def test_new_and_changed_listings_are_stored_and_matched(
    ingestor: ListingIngestor,
    adapter: FakeAdapter,
    recorder: Recorder,
    clock: Clock,
    session_factory: sessionmaker[Session],
) -> None:
    adapter.results = [listing("a", beds=3, description="Has wifi"), listing("b", beds=1)]

    first = ingestor.run(adapter)

    assert first is not None
    assert first.counts == RunCounts(found=2, new=2, updated=0, removed=0)
    assert first.matched == 2
    assert [(sid, r.passed, r.criteria_version) for sid, r in recorder.results] == [
        ("a", True, 1),
        ("b", False, 1),
    ]
    assert adapter.searched_with == [Criteria(min_beds=2, furnished_required=False)]
    store = SqlListingStore(session_factory)
    stored = store.find("fake", "a")
    assert stored is not None
    assert stored.amenities == AmenityResult(present=frozenset({Amenity.WIFI}))

    # Second run: "a" unchanged, "b" changed, "c" new.
    recorder.results.clear()
    clock.now = T0 + timedelta(minutes=10)
    adapter.results = [
        listing("a", beds=3, description="Has wifi"),
        listing("b", beds=2),
        listing("c"),
    ]

    second = ingestor.run(adapter)

    assert second is not None
    assert second.counts == RunCounts(found=3, new=1, updated=1, removed=0)
    assert sorted(sid for sid, _ in recorder.results) == ["b", "c"]
    (run2, run1) = store.runs("fake")
    assert (run1.id, run1.status, run1.counts) == (
        first.run_id,
        SourceRunStatus.SUCCESS,
        first.counts,
    )
    assert (run2.status, run2.finished_at) == (SourceRunStatus.SUCCESS, clock.now)


def test_disappeared_listings_are_reported_removed(
    ingestor: ListingIngestor, adapter: FakeAdapter, clock: Clock
) -> None:
    adapter.results = [listing("a"), listing("b")]
    ingestor.run(adapter)
    clock.now = T0 + timedelta(hours=2)
    adapter.results = [listing("b")]

    summary = ingestor.run(adapter)

    assert summary is not None
    assert summary.counts == RunCounts(found=1, new=0, updated=0, removed=1)
    assert len(summary.removed_ids) == 1


def test_no_active_criteria_means_no_run(
    session_factory: sessionmaker[Session], adapter: FakeAdapter
) -> None:
    ingestor = ListingIngestor(
        session_factory, extractor=KeywordAmenityExtractor(), settings=SourcesSettings()
    )

    assert ingestor.run(adapter) is None
    assert adapter.searched_with == []
    assert SqlListingStore(session_factory).runs() == []


def test_failed_search_records_the_error_and_removes_nothing(
    ingestor: ListingIngestor,
    adapter: FakeAdapter,
    clock: Clock,
    session_factory: sessionmaker[Session],
) -> None:
    adapter.results = [listing("a")]
    ingestor.run(adapter)
    clock.now = T0 + timedelta(hours=2)
    adapter.error = RuntimeError("blocked")

    with pytest.raises(RuntimeError, match="blocked"):
        ingestor.run(adapter)

    store = SqlListingStore(session_factory)
    failed = store.runs("fake")[0]
    assert (failed.status, failed.error, failed.finished_at) == (
        SourceRunStatus.FAILED,
        "RuntimeError: blocked",
        clock.now,
    )
    stored = store.find("fake", "a")
    assert stored is not None
    assert (stored.missed_runs, stored.removed_at) == (0, None)


class ExplodingExtractor:
    def extract(self, listing: NormalizedListing) -> AmenityResult:
        if listing.source_id == "bad":
            raise ValueError("boom")
        return AmenityResult()


def test_one_failing_listing_does_not_fail_the_run(
    session_factory: sessionmaker[Session], adapter: FakeAdapter, recorder: Recorder
) -> None:
    CriteriaStore(session_factory).save(Criteria(furnished_required=False), created_by="cli")
    ingestor = ListingIngestor(
        session_factory,
        extractor=ExplodingExtractor(),
        settings=SourcesSettings(),
        record=recorder,
    )
    adapter.results = [listing("bad"), listing("good")]

    summary = ingestor.run(adapter)

    assert summary is not None
    assert summary.counts.new == 2
    assert summary.matched == 1
    assert [sid for sid, _ in recorder.results] == ["good"]
    assert SqlListingStore(session_factory).runs()[0].status is SourceRunStatus.SUCCESS
