"""The rerun_criteria job handler: scores listings against the queued criteria version."""

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.base import utcnow
from furnest.db.criteria_store import CriteriaStore, CriteriaVersionNotFoundError
from furnest.db.listing_store import SqlListingStore, StoredListing
from furnest.domain import (
    Actor,
    Amenity,
    AmenityResult,
    Criteria,
    NormalizedListing,
    PropertyStatus,
)
from furnest.matching import MatchResult
from furnest.pipeline.properties import PropertyPipeline
from furnest.pipeline.rerun import RERUN_CRITERIA, RerunCriteria
from furnest.worker.execution import JobContext, default_registry

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def listing(source_id: str, beds: int, amenities: AmenityResult | None = None) -> StoredListing:
    return StoredListing(
        id=ord(source_id),
        listing=NormalizedListing(
            source="test",
            source_id=source_id,
            url="https://example.com/" + source_id,  # type: ignore[arg-type]
            title="Furnished place",
            beds=beds,
        ),
        property_id=None,
        missed_runs=0,
        removed_at=None,
        amenities=amenities,
    )


def ctx(version: int) -> JobContext:
    return JobContext(
        job_id=1,
        kind=RERUN_CRITERIA,
        payload={"criteria_version": version},
        attempt=1,
        deadline=utcnow(),
    )


def test_scores_every_listing_against_the_queued_version(
    session_factory: sessionmaker[Session],
) -> None:
    store = CriteriaStore(session_factory)
    store.save(Criteria(min_beds=2, furnished_required=False), created_by="cli")
    store.save(Criteria(min_beds=9), created_by="cli")  # active now, but not the one queued
    recorded: list[tuple[str, MatchResult]] = []
    handler = RerunCriteria(
        session_factory,
        listings=lambda: [listing("a", 3), listing("b", 1)],
        record=lambda item, result: recorded.append((item.listing.source_id, result)),
    )

    handler(ctx(1))

    assert [(sid, r.passed, r.criteria_version) for sid, r in recorded] == [
        ("a", True, 1),
        ("b", False, 1),
    ]


def test_stops_when_cancelled(session_factory: sessionmaker[Session]) -> None:
    CriteriaStore(session_factory).save(Criteria(), created_by="cli")
    recorded: list[str] = []
    job = ctx(1)

    def record(item: StoredListing, _result: MatchResult) -> None:
        recorded.append(item.listing.source_id)
        job.cancelled.set()

    RerunCriteria(
        session_factory, listings=lambda: [listing("a", 1), listing("b", 1)], record=record
    )(job)

    assert recorded == ["a"]


def test_default_source_scores_nothing(session_factory: sessionmaker[Session]) -> None:
    CriteriaStore(session_factory).save(Criteria(), created_by="cli")
    RerunCriteria(session_factory)(ctx(1))


def test_unknown_version_fails_the_job(session_factory: sessionmaker[Session]) -> None:
    with pytest.raises(CriteriaVersionNotFoundError):
        RerunCriteria(session_factory)(ctx(3))


def test_uses_the_amenities_stored_at_ingest(session_factory: sessionmaker[Session]) -> None:
    CriteriaStore(session_factory).save(
        Criteria(must_have_amenities=("ac",), furnished_required=False), created_by="cli"
    )
    recorded: list[tuple[str, bool]] = []
    handler = RerunCriteria(
        session_factory,
        listings=lambda: [
            listing("a", 1, AmenityResult(present=frozenset({Amenity.AC}))),
            # Ruled out by the extractor, which keyword matching alone can't know.
            listing("b", 1, AmenityResult(absent=frozenset({Amenity.AC}))),
        ],
        record=lambda item, result: recorded.append((item.listing.source_id, result.passed)),
    )

    handler(ctx(1))

    assert recorded == [("a", True), ("b", False)]


def test_the_worker_rerun_rescores_stored_listings_without_undoing_decisions(
    session_factory: sessionmaker[Session],
) -> None:
    criteria = CriteriaStore(session_factory)
    criteria.save(Criteria(min_beds=2, furnished_required=False), created_by="cli")
    listings = SqlListingStore(session_factory)
    found = listings.upsert_batch(
        "test", [item.listing for item in (listing("a", 3), listing("b", 3))], seen_at=T0
    )
    a_id, b_id = found.new_ids
    pipeline = PropertyPipeline(session_factory, clock=lambda: T0)
    a = pipeline.store.record_match(a_id, MatchResult(passed=True, score=50), at=T0)
    pipeline.store.transition(a.id, PropertyStatus.DENIED, actor=Actor.DISCORD, at=T0)
    criteria.save(Criteria(min_beds=3, furnished_required=False), created_by="cli")
    handler = default_registry(session_factory, pipeline).get(RERUN_CRITERIA)
    assert handler is not None

    handler.handler(ctx(2))

    a_after = pipeline.store.for_listing(a_id)
    b_after = pipeline.store.for_listing(b_id)
    assert a_after is not None
    assert b_after is not None
    assert (a_after.status, b_after.status) == (PropertyStatus.DENIED, PropertyStatus.SUGGESTED)
    assert a_after.score == b_after.score
    assert a_after.score != 50


def test_scores_only_the_listings_the_payload_names(
    session_factory: sessionmaker[Session],
) -> None:
    CriteriaStore(session_factory).save(Criteria(), created_by="cli")
    asked: list[list[int]] = []
    recorded: list[str] = []

    def by_id(ids: Sequence[int]) -> list[StoredListing]:
        asked.append(list(ids))
        return [listing("b", 2)]

    handler = RerunCriteria(
        session_factory,
        listings=lambda: [listing("a", 3), listing("b", 1)],
        by_id=by_id,
        record=lambda item, _result: recorded.append(item.listing.source_id),
    )
    job = ctx(1)
    handler(
        JobContext(
            job_id=job.job_id,
            kind=job.kind,
            payload={"criteria_version": 1, "listing_ids": [5, "6"]},
            attempt=job.attempt,
            deadline=job.deadline,
        )
    )

    assert asked == [[5, 6]]
    assert recorded == ["b"]
