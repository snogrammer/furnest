"""The system's side of the property status pipeline.

:class:`PropertyPipeline` connects ingestion and criteria re-runs to
:class:`~furnest.db.property_store.SqlPropertyStore`. Its methods have the shapes of
the hooks :class:`~furnest.pipeline.ingest.ListingIngestor` and
:class:`~furnest.pipeline.rerun.RerunCriteria` take:

* :meth:`~PropertyPipeline.record` stores each match. A passing listing moves its
  ``new`` property to ``suggested`` and queues a :data:`NOTIFY` job. A property in
  any other state only gets the new score, so a re-run never regresses a decision.
* :meth:`~PropertyPipeline.regroup` groups listings again once their photos are
  hashed (``GROUPING_*``; see :mod:`furnest.db.grouping`).
* :meth:`~PropertyPipeline.removed` moves a property to ``unavailable`` once all of
  its listings are gone, and :meth:`~PropertyPipeline.relisted` moves it back.
* :meth:`~PropertyPipeline.snooze` hides a property for ``PIPELINE_SNOOZE_HOURS``,
  and :class:`UnsnoozeExpired`, a scheduled worker job, brings it back afterwards.

``notify`` jobs are consumed by :class:`furnest.notify.post.NotifyPost`, which posts
the property to Discord.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker

from furnest.config import GroupingSettings, PipelineSettings
from furnest.db.base import utcnow
from furnest.db.listing_store import StoredListing
from furnest.db.property_store import NOTIFY, PropertyState, SqlPropertyStore
from furnest.domain import Actor
from furnest.logging import get_logger
from furnest.matching import MatchResult

if TYPE_CHECKING:
    from furnest.pipeline.schedule import Scheduler
    from furnest.worker.execution import JobContext

__all__ = [
    "NOTIFY",
    "UNSNOOZE_EXPIRED",
    "PropertyPipeline",
    "UnsnoozeExpired",
    "schedule_unsnooze",
]

UNSNOOZE_EXPIRED = "unsnooze_expired"

log = get_logger(__name__)


class PropertyPipeline:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: PipelineSettings | None = None,
        grouping: GroupingSettings | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = SqlPropertyStore(session_factory, grouping=grouping)
        self._settings = settings or PipelineSettings()
        self._clock = clock

    def record(self, stored: StoredListing, result: MatchResult) -> None:
        self.store.record_match(stored.id, result, at=self._clock())

    def regroup(self, listing_ids: Sequence[int]) -> None:
        merged = self.store.regroup(listing_ids, at=self._clock())
        if merged:
            log.info("properties_merged", property_ids=merged)

    def removed(self, listing_ids: Sequence[int]) -> None:
        moved = self.store.mark_gone(listing_ids, at=self._clock())
        if moved:
            log.info("properties_unavailable", property_ids=moved)

    def relisted(self, listing_ids: Sequence[int]) -> None:
        moved = self.store.restore_relisted(listing_ids, at=self._clock())
        if moved:
            log.info("properties_restored", property_ids=moved)

    def snooze(self, property_id: int, *, actor: Actor, detail: str | None = None) -> PropertyState:
        """Snooze the property for ``PIPELINE_SNOOZE_HOURS`` from now."""
        now = self._clock()
        until = now + timedelta(hours=self._settings.snooze_hours)
        return self.store.snooze(property_id, until=until, actor=actor, at=now, detail=detail)

    def unsnooze_expired(self) -> list[int]:
        cleared = self.store.unsnooze_expired(now=self._clock())
        if cleared:
            log.info("snoozes_expired", property_ids=cleared)
        return cleared


class UnsnoozeExpired:
    """Scheduled job: clear expired snoozes and notify again where a decision is due."""

    def __init__(self, pipeline: PropertyPipeline) -> None:
        self._pipeline = pipeline

    def __call__(self, _ctx: JobContext) -> None:
        self._pipeline.unsnooze_expired()


def schedule_unsnooze(
    worker: Scheduler, pipeline: PropertyPipeline, settings: PipelineSettings
) -> None:
    worker.schedule(
        UNSNOOZE_EXPIRED,
        UnsnoozeExpired(pipeline),
        interval=timedelta(minutes=settings.unsnooze_interval_minutes),
    )
