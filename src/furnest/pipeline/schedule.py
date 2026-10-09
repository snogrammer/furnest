"""Per-source scheduling: each source runs on its own interval in the worker.

:func:`schedule_sources` runs once, when the worker starts. It makes sure every
registered adapter has a ``sources`` row, stores why any of them can't run, and
registers one scheduled job, ``source:<name>``, per source that may run. Each job
gets its source's interval, jitter and run timeout (``SOURCES_*``, overridable per
adapter in ``SOURCES_ADAPTERS``, and the interval on the dashboard too).

:meth:`furnest.worker.runtime.Worker.schedule` provides the rest. Runs of a source
never overlap, in this worker or any other, because each run holds the DB lease
``scheduled:source:<name>``. A run that passes its timeout is abandoned, and every
source has its own job, so a slow or broken source never holds up the others.

One run (:class:`SourceRun`):

1. resolves the source's config again with what the dashboard has *now* (toggle,
   settings, credentials) and skips the run if the source is disabled. Sources
   toggled off, and sources still missing their API token, are scheduled all the
   same for this reason: turning one on or adding the token needs no worker
   restart. Sources pinned off in ``SOURCES_ADAPTERS``, stubs, and sources that
   need a browser the worker doesn't have are never scheduled; changing those
   means changing the environment or the image, which restarts the worker anyway;
2. hands a fresh adapter to :class:`~furnest.pipeline.ingest.ListingIngestor`,
   passing the job's ``cancelled`` event so a timed-out run stores nothing late;
3. records success on the ``sources`` row, which resets ``consecutive_failures``.

A job's interval is fixed when it is scheduled, so a separate job,
``sources:refresh`` (:class:`RefreshSchedules`), checks every
:data:`SCHEDULE_REFRESH_INTERVAL` whether a source's resolved interval has changed
(the dashboard's setting) and reschedules it through
:meth:`~furnest.worker.runtime.Worker.reschedule`.

``alert_after`` (``DISCORD_ALERT_AFTER_FAILURES``) goes to the store with every
outcome, which queues the Discord alert when a source starts or stops failing
(see :mod:`furnest.db.source_store`).

Failures aren't caught here. The worker reports them to Sentry with the ``source``
tag and calls :meth:`SourceRun.on_failure`, which bumps the failure counter and
stores ``last_error``. It does the same for a timeout, so a stuck source shows up
as failing even while its thread is still stuck.

"Run now" on the source health page queues a ``run_source`` job instead
(:class:`RunSourceNow`, registered by :func:`register_run_now`). It takes the same
lease as the scheduled job, so it never overlaps a scheduled run; if the source is
already running, that run is the one the user asked for and the job does nothing.
It runs once (``max_attempts=1``): a manual run against a blocked site shouldn't
keep retrying. A run that times out stores nothing, because the ingestor checks
``cancelled``, and the ``sources`` row records the failure once ``search`` returns.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.orm import Session, sessionmaker

from furnest.config import SourcesSettings
from furnest.db.base import utcnow
from furnest.db.source_store import SqlSourceStore
from furnest.logging import error_text, get_logger
from furnest.sources.base import AdapterConfig, Blocker, SourceAdapter
from furnest.sources.config import resolve_config
from furnest.worker import locks
from furnest.worker.runtime import LEASE_GRACE

if TYPE_CHECKING:
    from furnest.pipeline.ingest import ListingIngestor
    from furnest.sources.registry import SourceRegistry
    from furnest.worker.execution import Handler, JobContext, JobRegistry

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

    def reschedule(self, name: str, *, interval: timedelta, jitter: timedelta = ...) -> None: ...


# Queue job kind for "run now"; its payload is ``{"source": <name>}``.
RUN_SOURCE = "run_source"
# The scheduled job that applies interval changes made on the dashboard.
REFRESH_JOB = "sources:refresh"
SCHEDULE_REFRESH_INTERVAL = timedelta(minutes=5)


def job_name(source: str) -> str:
    return f"source:{source}"


def lease_name(source: str) -> str:
    """The lease held while a source runs, scheduled or on demand."""
    return f"scheduled:{job_name(source)}"


class SourceRun:
    """The scheduled job for one source."""

    def __init__(
        self,
        adapter: type[SourceAdapter],
        *,
        settings: SourcesSettings,
        store: SqlSourceStore,
        ingestor: ListingIngestor,
        alert_after: int = 0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._adapter = adapter
        self._settings = settings
        self._store = store
        self._ingestor = ingestor
        self._alert_after = alert_after
        self._clock = clock

    @property
    def name(self) -> str:
        return self._adapter.name

    def __call__(self, ctx: JobContext) -> None:
        config = self.resolve()
        if not config.enabled:
            reason = config.disabled_reason or "turned off"
            log.info("source_run_skipped", source=self.name, reason=reason)
            return
        summary = self._ingestor.run(self._adapter(config), cancelled=ctx.cancelled)
        if summary is None or ctx.cancelled.is_set():
            # No criteria to search with, or the worker already recorded a timeout.
            return
        self._store.record_success(
            self.name,
            listings_found=summary.counts.found,
            at=self._clock(),
            alert_after=self._alert_after,
        )

    def on_failure(self, exc: BaseException) -> None:
        self._store.record_failure(
            self.name,
            error=error_text(exc),
            at=self._clock(),
            alert_after=self._alert_after,
        )

    def resolve(self) -> AdapterConfig:
        """The source's config with what the dashboard has stored right now."""
        credential = self._adapter.credential
        stored = self._store.stored([credential.key] if credential is not None else ())
        return resolve_config(
            self._adapter,
            self._settings,
            stored.sources.get(self.name),
            credentials=stored.credentials,
        )


class RefreshSchedules:
    """The ``sources:refresh`` job: reschedule sources whose interval changed."""

    def __init__(self, worker: Scheduler, runs: Iterable[SourceRun]) -> None:
        self._worker = worker
        self._runs = {run.name: run for run in runs}
        self._intervals = {name: run.resolve().interval_minutes for name, run in self._runs.items()}

    @property
    def intervals(self) -> Mapping[str, int]:
        """Each scheduled source's interval in minutes, as last applied."""
        return dict(self._intervals)

    def __call__(self, ctx: JobContext) -> None:
        for name, run in self._runs.items():
            config = run.resolve()
            if config.interval_minutes == self._intervals[name]:
                continue
            self._worker.reschedule(
                job_name(name),
                interval=timedelta(minutes=config.interval_minutes),
                jitter=timedelta(seconds=config.jitter_seconds),
            )
            self._intervals[name] = config.interval_minutes
            log.info("source_rescheduled", source=name, interval_minutes=config.interval_minutes)


def schedule_sources(
    worker: Scheduler,
    registry: SourceRegistry,
    settings: SourcesSettings,
    session_factory: sessionmaker[Session],
    ingestor: ListingIngestor,
    *,
    alert_after: int = 0,
    clock: Callable[[], datetime] = utcnow,
) -> list[str]:
    """Schedule every source that may run; returns their names.

    Only the browser reason is stored on the ``sources`` rows: the web process can't
    probe for a browser, but works out the other reasons itself, so a token added on
    the dashboard clears "needs the token" at once.

    Raises :class:`furnest.sources.config.SourceConfigError` when ``SOURCES_ADAPTERS``
    names an unknown adapter.
    """
    store = SqlSourceStore(session_factory)
    store.ensure(registry.names())
    configured = registry.configured(settings, store.stored(registry.credential_keys()))
    store.set_disabled_reasons(
        {
            s.config.name: s.config.disabled_reason
            if s.config.blocker is Blocker.NO_BROWSER
            else None
            for s in configured
        }
    )

    scheduled: list[str] = []
    runs: list[SourceRun] = []
    for source in configured:
        config = source.config
        if config.blocker in (Blocker.NOT_BUILT, Blocker.NO_BROWSER) or (
            config.enabled_pinned and not config.enabled
        ):
            continue
        run = SourceRun(
            source.adapter,
            settings=settings,
            store=store,
            ingestor=ingestor,
            alert_after=alert_after,
            clock=clock,
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
        runs.append(run)
    if runs:
        worker.schedule(
            REFRESH_JOB,
            RefreshSchedules(worker, runs),
            interval=SCHEDULE_REFRESH_INTERVAL,
            timeout=timedelta(minutes=1),
        )
    log.info("sources_scheduled", sources=scheduled)
    return scheduled


class RunSourceNow:
    """The ``run_source`` queue job: run one source now, outside its schedule."""

    def __init__(
        self,
        registry: SourceRegistry,
        *,
        settings: SourcesSettings,
        session_factory: sessionmaker[Session],
        ingestor: ListingIngestor,
        alert_after: int = 0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._factory = session_factory
        self._store = SqlSourceStore(session_factory)
        self._ingestor = ingestor
        self._alert_after = alert_after
        self._clock = clock

    def __call__(self, ctx: JobContext) -> None:
        name = str(ctx.payload["source"])
        adapter = self._registry.get(name)
        if adapter is None:
            msg = f"no source named {name!r}"
            raise LookupError(msg)
        lease, owner = lease_name(name), f"run-now:{ctx.job_id}"
        ttl = max(ctx.deadline - self._clock(), timedelta(0)) + LEASE_GRACE
        if not locks.acquire(self._factory, lease, owner, ttl):
            log.info("source_run_now_skipped", source=name, reason="already running")
            return
        run = SourceRun(
            adapter,
            settings=self._settings,
            store=self._store,
            ingestor=self._ingestor,
            alert_after=self._alert_after,
            clock=self._clock,
        )
        try:
            run(ctx)
        except Exception as exc:
            run.on_failure(exc)
            raise
        finally:
            locks.release(self._factory, lease, owner)


def register_run_now(
    jobs: JobRegistry,
    registry: SourceRegistry,
    settings: SourcesSettings,
    session_factory: sessionmaker[Session],
    ingestor: ListingIngestor,
    *,
    alert_after: int = 0,
) -> None:
    """Register :class:`RunSourceNow` with a timeout that fits the slowest source."""
    adapters = (registry.get(name) for name in registry.names())
    timeouts = [resolve_config(a, settings).run_timeout_seconds for a in adapters if a is not None]
    jobs.register(
        RUN_SOURCE,
        RunSourceNow(
            registry,
            settings=settings,
            session_factory=session_factory,
            ingestor=ingestor,
            alert_after=alert_after,
        ),
        timeout=timedelta(seconds=max(timeouts, default=settings.run_timeout_seconds)),
    )
