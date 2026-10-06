"""The worker process: scheduled jobs, queue consumers, heartbeat, graceful shutdown.

Three kinds of thread do the work:

* **APScheduler** fires scheduled jobs (``Worker.schedule``) on an interval with
  jitter, and the heartbeat. Before a scheduled job runs, the worker takes the DB lease
  ``scheduled:<name>``. A job already running here (``max_instances=1``) or in
  another worker is skipped, never run twice at once.
* **Queue consumers** (``WORKER_CONCURRENCY`` of them) loop: claim the next due job,
  run its handler with a timeout, then record success, or failure with backoff.
* **Job threads**, one per run, so a timeout can stop the wait even when it can't
  stop the job (see :mod:`furnest.worker.execution`).

Shutdown (SIGTERM/SIGINT, or ``request_stop``) stops claiming and scheduling, then
waits up to ``WORKER_SHUTDOWN_TIMEOUT_SECONDS`` for running jobs. Queued jobs still
running after that are handed back to the queue without counting the attempt, and
the process exits. A second signal skips the wait.
"""

import datetime as dt
import logging
import random
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, timedelta
from types import FrameType

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import Engine

from furnest.config import Settings
from furnest.db.base import utcnow
from furnest.db.models import JobStatus
from furnest.db.session import make_session_factory
from furnest.logging import get_logger
from furnest.observability import capture_job_failure
from furnest.worker import heartbeat, locks, queue
from furnest.worker.execution import (
    Handler,
    JobContext,
    JobRegistry,
    JobTimeoutError,
    Outcome,
    OutcomeKind,
    run_with_timeout,
)
from furnest.worker.heartbeat import WorkerIdentity

log = get_logger(__name__)

# Leases outlive the job timeout by this much, so a job that finishes right at its
# timeout still releases its own lease before anyone else can take it.
LEASE_GRACE = timedelta(seconds=60)
# Heartbeat rows from processes not seen for this long are deleted at startup.
HEARTBEAT_RETENTION = timedelta(days=1)
# After abandoning, how long to wait for consumers to hand their jobs back.
_ABANDON_SETTLE_SECONDS = 5.0

# APScheduler logs every run at INFO; with a heartbeat every few seconds that drowns
# the useful lines. Its warnings and errors (missed runs, crashes) still come through.
logging.getLogger("apscheduler").setLevel(logging.WARNING)


FailureCallback = Callable[[BaseException], None]


@dataclass(frozen=True)
class _ScheduledRun:
    """Everything one scheduled job needs, as ``Worker.schedule`` was given it."""

    name: str
    handler: Handler
    timeout: timedelta
    source: str | None
    on_failure: FailureCallback | None


class Worker:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        registry: JobRegistry,
        *,
        identity: WorkerIdentity | None = None,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._config = settings.worker
        self._factory = make_session_factory(engine)
        self._registry = registry
        self._rng = rng
        self.identity = identity or WorkerIdentity()
        self._default_timeout = timedelta(seconds=self._config.job_timeout_seconds)

        # Guards ``_inflight`` and the stop flag together, so a consumer can't start a
        # job after shutdown has decided nothing is running. Reentrant because a signal
        # handler may run on the main thread while that thread holds it.
        self._cond = threading.Condition(threading.RLock())
        self._inflight: dict[int, str] = {}
        self._stop = threading.Event()
        self._abandon = threading.Event()

        self._scheduler = BackgroundScheduler(timezone=UTC)
        self._scheduler.add_job(
            self._beat,
            IntervalTrigger(seconds=self._config.heartbeat_interval_seconds),
            id="heartbeat",
            max_instances=1,
            coalesce=True,
        )

    # --- Public API -----------------------------------------------------------------

    def schedule(
        self,
        name: str,
        handler: Handler,
        *,
        interval: timedelta,
        jitter: timedelta = timedelta(0),
        timeout: timedelta | None = None,
        source: str | None = None,
        on_failure: FailureCallback | None = None,
    ) -> None:
        """Run ``handler`` every ``interval`` (plus up to ``jitter``), one run at a time
        across every worker sharing the database.

        ``source`` tags failure reports with the listing source the job runs.
        ``on_failure`` is called with the error after a run fails or times out, on the
        scheduler's thread, so it runs even while a timed-out handler is still stuck.
        """
        self._add_scheduled(
            _ScheduledRun(name, handler, timeout or self._default_timeout, source, on_failure),
            IntervalTrigger(
                seconds=interval.total_seconds(), jitter=jitter.total_seconds() or None
            ),
        )

    def schedule_daily(
        self,
        name: str,
        handler: Handler,
        *,
        at: dt.time,
        tz: dt.tzinfo,
        timeout: timedelta | None = None,
    ) -> None:
        """Run ``handler`` every day at ``at`` in ``tz``, with the same one-at-a-time
        guarantee as :meth:`schedule`. A day the worker is down at ``at`` is skipped.
        """
        self._add_scheduled(
            _ScheduledRun(name, handler, timeout or self._default_timeout, None, None),
            CronTrigger(hour=at.hour, minute=at.minute, second=at.second, timezone=tz),
        )

    def _add_scheduled(self, run: _ScheduledRun, trigger: BaseTrigger) -> None:
        self._scheduler.add_job(
            self._run_scheduled,
            trigger,
            args=(run,),
            id=run.name,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,
        )

    def run(self, *, install_signal_handlers: bool = True) -> None:
        """Run until stopped, then shut down gracefully. Blocks the calling thread."""
        if install_signal_handlers:
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, self._on_signal)
        heartbeat.prune(self._factory, HEARTBEAT_RETENTION)
        self._beat()
        self._scheduler.start()
        for index in range(self._config.concurrency):
            threading.Thread(target=self._consume, name=f"queue-{index}", daemon=True).start()
        log.info(
            "worker_started",
            worker_id=self.identity.worker_id,
            concurrency=self._config.concurrency,
        )
        self._stop.wait()
        self._shutdown()

    def request_stop(self) -> None:
        with self._cond:
            self._stop.set()
            self._cond.notify_all()

    # --- Shutdown ---------------------------------------------------------------------

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        if self._stop.is_set():
            log.warning("worker_forced_stop", signal=signal.Signals(signum).name)
            with self._cond:
                self._abandon.set()
                self._cond.notify_all()
            return
        log.info("worker_stop_requested", signal=signal.Signals(signum).name)
        self.request_stop()

    def _shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
        with self._cond:
            running = list(self._inflight.values())
        log.info("worker_stopping", running=running)
        if not self._wait_idle(self._config.shutdown_timeout_seconds, stop_on_abandon=True):
            with self._cond:
                running = list(self._inflight.values())
            log.warning("worker_shutdown_timeout", running=running)
            self._abandon.set()
            self._wait_idle(_ABANDON_SETTLE_SECONDS, stop_on_abandon=False)
        log.info("worker_stopped", worker_id=self.identity.worker_id)

    def _wait_idle(self, timeout: float, *, stop_on_abandon: bool) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (stop_on_abandon and self._abandon.is_set()):
                    return False
                self._cond.wait(remaining)
        return True

    @contextmanager
    def _tracked(self, label: str) -> Iterator[bool]:
        """Register a unit of work as in flight; yields ``False`` once stopping."""
        key = threading.get_ident()
        with self._cond:
            stopping = self._stop.is_set()
            if not stopping:
                self._inflight[key] = label
        if stopping:
            yield False
            return
        try:
            yield True
        finally:
            with self._cond:
                del self._inflight[key]
                self._cond.notify_all()

    # --- Heartbeat --------------------------------------------------------------------

    def _beat(self) -> None:
        try:
            heartbeat.beat(self._factory, self.identity)
        except Exception:
            log.exception("heartbeat_failed")

    # --- Scheduled jobs ---------------------------------------------------------------

    def _run_scheduled(self, run: _ScheduledRun) -> None:
        with self._tracked(f"scheduled {run.name}") as started:
            if not started:
                return
            try:
                self._run_scheduled_locked(run)
            except Exception:
                log.exception("scheduled_job_error", job=run.name)

    def _run_scheduled_locked(self, run: _ScheduledRun) -> None:
        name, timeout = run.name, run.timeout
        lease, owner = f"scheduled:{name}", self.identity.worker_id
        if not locks.acquire(self._factory, lease, owner, timeout + LEASE_GRACE):
            log.info("scheduled_job_skipped", job=name, reason="running elsewhere")
            return

        def release() -> None:
            try:
                locks.release(self._factory, lease, owner)
            except Exception:
                log.exception("lease_release_failed", lease=lease)

        ctx = JobContext(job_id=name, kind=name, payload={}, attempt=1, deadline=utcnow() + timeout)
        outcome = run_with_timeout(run.handler, ctx, abandon=self._abandon, on_finish=release)
        error = _failure(outcome, f"scheduled job {name!r}", timeout)
        if error is not None:
            tags = {"source": run.source} if run.source is not None else {}
            capture_job_failure(error, job_id=name, job_type="scheduled", **tags)
            if run.on_failure is not None:
                try:
                    run.on_failure(error)
                except Exception:
                    log.exception("scheduled_job_on_failure_error", job=name)
        elif outcome.kind is OutcomeKind.OK:
            log.info("scheduled_job_succeeded", job=name)

    # --- Queue ------------------------------------------------------------------------

    def _consume(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self._run_next()
            except Exception:
                log.exception("queue_consumer_error")
                worked = False
            if not worked:
                self._stop.wait(self._config.poll_interval_seconds)

    def _run_next(self) -> bool:
        """Claim and run one due job. ``False`` when there was nothing to do."""
        with self._tracked("claiming") as started:
            if not started:
                return False
            lease = self._registry.longest_timeout(self._default_timeout) + LEASE_GRACE
            job = queue.claim(self._factory, self.identity.worker_id, lease)
            if job is None:
                return False
            with self._cond:
                self._inflight[threading.get_ident()] = f"job {job.id} ({job.kind})"
            self._run_job(job)
            return True

    def _run_job(self, job: queue.ClaimedJob) -> None:
        worker_id = self.identity.worker_id
        log.info("job_started", job_id=job.id, job_type=job.kind, attempt=job.attempt)
        registration = self._registry.get(job.kind)
        timeout = (registration and registration.timeout) or self._default_timeout
        if registration is None:
            missing = LookupError(f"no handler for job kind {job.kind!r}")
            outcome = Outcome(OutcomeKind.ERROR, missing)
        else:
            ctx = JobContext(
                job_id=job.id,
                kind=job.kind,
                payload=job.payload,
                attempt=job.attempt,
                deadline=utcnow() + timeout,
            )
            outcome = run_with_timeout(registration.handler, ctx, abandon=self._abandon)

        if outcome.kind is OutcomeKind.OK:
            queue.complete(self._factory, job.id, worker_id)
            log.info("job_succeeded", job_id=job.id, job_type=job.kind, attempt=job.attempt)
            return
        if outcome.kind is OutcomeKind.ABANDONED:
            queue.requeue(self._factory, job.id, worker_id, "interrupted by worker shutdown")
            log.warning("job_requeued", job_id=job.id, job_type=job.kind, reason="shutdown")
            return

        error = _failure(outcome, f"job {job.id} ({job.kind})", timeout)
        assert error is not None  # noqa: S101 - OK and ABANDONED returned above
        capture_job_failure(error, job_id=job.id, job_type=job.kind)
        retry_in = queue.backoff(
            job.attempt,
            base=self._config.retry_base_seconds,
            cap=self._config.retry_max_seconds,
            rng=self._rng,
        )
        status = queue.fail(
            self._factory, job.id, worker_id, f"{type(error).__name__}: {error}", retry_in=retry_in
        )
        log.info(
            "job_failed_attempt",
            job_id=job.id,
            job_type=job.kind,
            attempt=job.attempt,
            max_attempts=job.max_attempts,
            status=status,
            retry_in_seconds=None if status is not JobStatus.FAILED else retry_in.total_seconds(),
        )


def _failure(outcome: Outcome, what: str, timeout: timedelta) -> BaseException | None:
    """The exception to report for a failed outcome; ``None`` for OK and ABANDONED."""
    if outcome.kind is OutcomeKind.ERROR:
        return outcome.error
    if outcome.kind is OutcomeKind.TIMED_OUT:
        return JobTimeoutError(f"{what} timed out after {timeout.total_seconds():g}s")
    return None
