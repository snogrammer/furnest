"""PropertyPipeline: snooze length, the unsnooze job and the notify placeholder."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import PipelineSettings
from furnest.db.base import utcnow
from furnest.db.listing_store import SqlListingStore
from furnest.domain import Actor, NormalizedListing
from furnest.matching import MatchResult
from furnest.pipeline.properties import (
    NOTIFY,
    UNSNOOZE_EXPIRED,
    PropertyPipeline,
    UnsnoozeExpired,
    notify_not_configured,
    schedule_unsnooze,
)
from furnest.worker.execution import Handler, JobContext

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, object]] = {}

    def schedule(self, name: str, handler: Handler, **options: object) -> None:
        self.jobs[name] = {"handler": handler, **options}


def ctx(kind: str, payload: dict[str, object] | None = None) -> JobContext:
    return JobContext(job_id=1, kind=kind, payload=payload or {}, attempt=1, deadline=utcnow())


def suggested_property(pipeline: PropertyPipeline, session_factory: sessionmaker[Session]) -> int:
    listing = NormalizedListing.model_validate(
        {"source": "test", "source_id": "a", "url": "https://example.com/a", "title": "Place"}
    )
    (listing_id,) = (
        SqlListingStore(session_factory).upsert_batch("test", [listing], seen_at=T0).new_ids
    )
    return pipeline.store.record_match(listing_id, MatchResult(passed=True, score=70), at=T0).id


def test_snooze_lasts_the_configured_hours_and_the_job_brings_it_back(
    session_factory: sessionmaker[Session],
) -> None:
    clock = Clock()
    pipeline = PropertyPipeline(
        session_factory, settings=PipelineSettings(snooze_hours=24), clock=clock
    )
    property_id = suggested_property(pipeline, session_factory)

    state = pipeline.snooze(property_id, actor=Actor.DISCORD, detail="u1")

    assert state.snoozed_until == T0 + timedelta(hours=24)
    job = UnsnoozeExpired(pipeline)
    clock.now = T0 + timedelta(hours=23)
    job(ctx(UNSNOOZE_EXPIRED))
    assert pipeline.store.query(now=clock.now) == []
    clock.now = T0 + timedelta(hours=24)
    job(ctx(UNSNOOZE_EXPIRED))
    after = pipeline.store.get(property_id)
    assert after is not None
    assert after.snoozed_until is None


def test_unsnooze_is_scheduled_at_the_configured_interval(
    session_factory: sessionmaker[Session],
) -> None:
    scheduler = FakeScheduler()
    pipeline = PropertyPipeline(session_factory)

    schedule_unsnooze(scheduler, pipeline, PipelineSettings(unsnooze_interval_minutes=5))

    job = scheduler.jobs[UNSNOOZE_EXPIRED]
    assert isinstance(job["handler"], UnsnoozeExpired)
    assert job["interval"] == timedelta(minutes=5)


def test_notifications_are_consumed_until_discord_exists() -> None:
    notify_not_configured(ctx(NOTIFY, {"property_id": 1}))
