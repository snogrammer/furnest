"""Per-source scheduling: each source runs on its own interval in the worker.

:func:`schedule_sources` runs once, when the worker starts. It makes sure every
registered adapter has a ``sources`` row, stores why any of them can't run, and
registers one scheduled job, ``source:<name>``, per source that may run. Each job
gets its source's interval, jitter and run timeout (``SOURCES_*``, overridable per
adapter in ``SOURCES_ADAPTERS``).

:meth:`furnest.worker.runtime.Worker.schedule` provides the rest. Runs of a source
never overlap, in this worker or any other, because each run holds the DB lease
``scheduled:source:<name>``. A run that passes its timeout is abandoned, and every
source has its own job, so a slow or broken source never holds up the others.

One run (:class:`SourceRun`):

1. resolves the source's config again with the *current* dashboard toggle and
   skips the run if the source is now disabled. Sources toggled off are still
   scheduled for this reason, so turning one back on needs no worker restart.
   Sources pinned off in ``SOURCES_ADAPTERS`` or unable to run (``disabled_reason``)
   are never scheduled; changing those means changing the environment, which
   restarts the worker anyway;
2. hands a fresh adapter to :class:`~furnest.pipeline.ingest.ListingIngestor`,
   passing the job's ``cancelled`` event so a timed-out run stores nothing late;
3. records success on the ``sources`` row, which resets ``consecutive_failures``.

Failures aren't caught here. The worker reports them to Sentry with the ``source``
tag and calls :meth:`SourceRun.on_failure`, which bumps the failure counter and
stores ``last_error``. It does the same for a timeout, so a stuck source shows up
as failing even while its thread is still stuck.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.orm import Session, sessionmaker

from furnest.config import SourcesSettings
from furnest.db.base import utcnow
from furnest.db.source_store import SqlSourceStore
from furnest.logging import get_logger
from furnest.sources.base import SourceAdapter
from furnest.sources.config import resolve_config

if TYPE_CHECKING:
    from furnest.pipeline.ingest import ListingIngestor
    from furnest.sources.registry import SourceRegistry
    from furnest.worker.execution import Handler, JobContext

log = get_logger(__name__)


class Scheduler(Protocol):
    """The part of :class:`furnest.worker.runtime.Worker` this module needs."""

    def schedule(
        self,
        name: str,
        handler: Handler,
        *,
        interval: timedelta,
        jitter: timedelta = ...,
        timeout: timedelta | None = ...,
        source: str | None = ...,
        on_failure: Callable[[BaseException], None] | None = ...,
    ) -> None: ...


def job_name(source: str) -> str:
    return f"source:{source}"


class SourceRun:
    """The scheduled job for one source."""

    def __init__(
        self,
        adapter: type[SourceAdapter],
        *,
        settings: SourcesSettings,
        store: SqlSourceStore,
        ingestor: ListingIngestor,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._adapter = adapter
        self._settings = settings
        self._store = store
        self._ingestor = ingestor
        self._clock = clock

    @property
    def name(self) -> str:
        return self._adapter.name

    def __call__(self, ctx: JobContext) -> None:
        state = self._store.get(self.name)
        config = resolve_config(
            self._adapter, self._settings, state.enabled if state is not None else None
        )
        if not config.enabled:
            log.info("source_run_skipped", source=self.name, reason="disabled")
            return
        summary = self._ingestor.run(self._adapter(config), cancelled=ctx.cancelled)
        if summary is None or ctx.cancelled.is_set():
            # No criteria to search with, or the worker already recorded a timeout.
            return
        self._store.record_success(self.name, listings_found=summary.counts.found, at=self._clock())

    def on_failure(self, exc: BaseException) -> None:
        self._store.record_failure(
            self.name, error=f"{type(exc).__name__}: {exc}", at=self._clock()
        )


def schedule_sources(
    worker: Scheduler,
    registry: SourceRegistry,
    settings: SourcesSettings,
    session_factory: sessionmaker[Session],
    ingestor: ListingIngestor,
    *,
    clock: Callable[[], datetime] = utcnow,
) -> list[str]:
    """Schedule every source that may run; returns their names.

    Raises :class:`furnest.sources.config.SourceConfigError` when ``SOURCES_ADAPTERS``
    names an unknown adapter.
    """
    store = SqlSourceStore(session_factory)
    store.ensure(registry.names())
    configured = registry.configured(settings, store.stored_enabled())
    store.set_disabled_reasons({s.config.name: s.config.disabled_reason for s in configured})

    scheduled: list[str] = []
    for source in configured:
        config = source.config
        if config.disabled_reason is not None or (config.enabled_pinned and not config.enabled):
            continue
        run = SourceRun(
            source.adapter, settings=settings, store=store, ingestor=ingestor, clock=clock
        )
        worker.schedule(
            job_name(config.name),
            run,
            interval=timedelta(minutes=config.interval_minutes),
            jitter=timedelta(seconds=config.jitter_seconds),
            timeout=timedelta(seconds=config.run_timeout_seconds),
            source=config.name,
            on_failure=run.on_failure,
        )
        scheduled.append(config.name)
    log.info("sources_scheduled", sources=scheduled)
    return scheduled
