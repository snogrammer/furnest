"""The rerun_criteria job handler: scores listings against the queued criteria version."""

import pytest
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.base import utcnow
from furnest.db.criteria_store import CriteriaStore, CriteriaVersionNotFoundError
from furnest.domain import Criteria, NormalizedListing
from furnest.matching import MatchResult
from furnest.pipeline.rerun import RERUN_CRITERIA, RerunCriteria
from furnest.worker.execution import JobContext

pytestmark = pytest.mark.integration


def listing(source_id: str, beds: int) -> NormalizedListing:
    return NormalizedListing(
        source="test",
        source_id=source_id,
        url="https://example.com/" + source_id,  # type: ignore[arg-type]
        title="Furnished place",
        beds=beds,
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
        record=lambda item, result: recorded.append((item.source_id, result)),
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

    def record(item: NormalizedListing, _result: MatchResult) -> None:
        recorded.append(item.source_id)
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
