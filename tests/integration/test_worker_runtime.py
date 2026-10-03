"""The worker process end to end, in-process: heartbeat, queue, retries, timeouts,
scheduled jobs under a lease, and graceful shutdown."""

import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import Settings, WorkerSettings
from furnest.db.models import Job, JobLock, JobStatus, WorkerHeartbeat
from furnest.db.session import session_scope
from furnest.web.health import check_readiness
from furnest.worker import heartbeat, locks, queue, runtime
from furnest.worker.execution import JobContext, JobRegistry, JobTimeoutError
from furnest.worker.runtime import Worker

pytestmark = pytest.mark.integration

FAST = {
    "heartbeat_interval_seconds": 1,
    "poll_interval_seconds": 0.05,
    "concurrency": 1,
    "job_timeout_seconds": 10,
    "retry_base_seconds": 1,
    "shutdown_timeout_seconds": 10,
}

Failures = list[tuple[BaseException, dict[str, Any]]]
StartWorker = Callable[..., tuple[Worker, threading.Thread]]


def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


@pytest.fixture
def failures(monkeypatch: pytest.MonkeyPatch) -> Failures:
    """Calls to ``capture_job_failure`` (the Sentry report) made by the worker."""
    calls: Failures = []
    monkeypatch.setattr(
        runtime, "capture_job_failure", lambda exc, **tags: calls.append((exc, tags))
    )
    return calls


@pytest.fixture
def start_worker(migrated_engine: Engine) -> Iterator[StartWorker]:
    """Start a worker on its own thread; every started worker is stopped at teardown."""
    started: list[tuple[Worker, threading.Thread]] = []

    def start(
        registry: JobRegistry | None = None,
        setup: Callable[[Worker], None] | None = None,
        **overrides: object,
    ) -> tuple[Worker, threading.Thread]:
        settings = Settings(worker=WorkerSettings.model_validate({**FAST, **overrides}))
        worker = Worker(settings, migrated_engine, registry or JobRegistry(), rng=lambda: 1.0)
        if setup is not None:
            setup(worker)
        thread = threading.Thread(
            target=worker.run, kwargs={"install_signal_handlers": False}, daemon=True
        )
        thread.start()
        started.append((worker, thread))
        return worker, thread

    yield start
    for worker, thread in started:
        worker.request_stop()
        thread.join(timeout=15)
        assert not thread.is_alive()


def _enqueue(factory: sessionmaker[Session], kind: str, max_attempts: int | None = None) -> int:
    with session_scope(factory) as session:
        return queue.enqueue(session, kind, {"n": 1}, max_attempts=max_attempts).id


def _job(factory: sessionmaker[Session], job_id: int) -> Job:
    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job


def _status(factory: sessionmaker[Session], job_id: int) -> JobStatus:
    return _job(factory, job_id).status


# --- Heartbeat -------------------------------------------------------------------------


def test_worker_heartbeat_makes_readyz_report_the_worker_ok(
    start_worker: StartWorker, migrated_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    worker, _ = start_worker()

    def beat_row() -> WorkerHeartbeat | None:
        with session_scope(session_factory) as session:
            return session.get(WorkerHeartbeat, worker.identity.worker_id)

    wait_for(lambda: beat_row() is not None)
    first = beat_row()
    assert first is not None
    assert (first.pid, first.hostname) == (os.getpid(), worker.identity.hostname)
    report = check_readiness(migrated_engine, max_heartbeat_age=timedelta(seconds=120))
    assert report.checks["worker"].ok

    wait_for(lambda: (row := beat_row()) is not None and row.last_seen_at > first.last_seen_at)


def test_heartbeat_failure_is_logged_not_fatal(
    start_worker: StartWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def broken(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise RuntimeError("db gone")

    monkeypatch.setattr(heartbeat, "beat", broken)
    _, thread = start_worker()

    wait_for(lambda: len(calls) >= 2)
    assert thread.is_alive()


# --- Queue -------------------------------------------------------------------------------


def test_queued_job_runs_and_succeeds(
    start_worker: StartWorker, session_factory: sessionmaker[Session], failures: Failures
) -> None:
    seen: list[JobContext] = []
    registry = JobRegistry()
    registry.register("echo", seen.append)
    job_id = _enqueue(session_factory, "echo")

    start_worker(registry)

    wait_for(lambda: _status(session_factory, job_id) is JobStatus.SUCCEEDED)
    [ctx] = seen
    assert (ctx.job_id, ctx.kind, ctx.payload, ctx.attempt) == (job_id, "echo", {"n": 1}, 1)
    assert failures == []


def test_failing_job_is_retried_with_backoff_then_dies(
    start_worker: StartWorker, session_factory: sessionmaker[Session], failures: Failures
) -> None:
    attempts: list[int] = []

    def flaky(ctx: JobContext) -> None:
        attempts.append(ctx.attempt)
        raise ConnectionError(f"attempt {ctx.attempt} failed")

    registry = JobRegistry()
    registry.register("flaky", flaky)
    job_id = _enqueue(session_factory, "flaky", max_attempts=2)

    start_worker(registry)

    wait_for(lambda: _status(session_factory, job_id) is JobStatus.DEAD)
    assert attempts == [1, 2]
    assert _job(session_factory, job_id).last_error == "ConnectionError: attempt 2 failed"
    assert [type(exc) for exc, _ in failures] == [ConnectionError, ConnectionError]
    assert failures[0][1] == {"job_id": job_id, "job_type": "flaky"}


def test_timed_out_job_is_cancelled_reported_and_retried(
    start_worker: StartWorker, session_factory: sessionmaker[Session], failures: Failures
) -> None:
    cancelled = threading.Event()

    def slow(ctx: JobContext) -> None:
        if ctx.cancelled.wait(5):
            cancelled.set()

    registry = JobRegistry()
    registry.register("slow", slow, timeout=timedelta(milliseconds=300))
    job_id = _enqueue(session_factory, "slow", max_attempts=1)

    start_worker(registry)

    wait_for(lambda: _status(session_factory, job_id) is JobStatus.DEAD)
    assert cancelled.wait(2)
    [(exc, tags)] = failures
    assert isinstance(exc, JobTimeoutError)
    assert tags == {"job_id": job_id, "job_type": "slow"}
    assert "timed out after 0.3s" in (_job(session_factory, job_id).last_error or "")


def test_job_without_a_handler_fails(
    start_worker: StartWorker, session_factory: sessionmaker[Session], failures: Failures
) -> None:
    job_id = _enqueue(session_factory, "unknown", max_attempts=1)

    start_worker()

    wait_for(lambda: _status(session_factory, job_id) is JobStatus.DEAD)
    assert _job(session_factory, job_id).last_error == (
        "LookupError: no handler for job kind 'unknown'"
    )
    assert len(failures) == 1


def test_consumer_survives_database_errors(
    start_worker: StartWorker,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_claim = queue.claim
    calls: list[int] = []

    def flaky_claim(
        factory: sessionmaker[Session], worker_id: str, lease: timedelta
    ) -> queue.ClaimedJob | None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return real_claim(factory, worker_id, lease)

    monkeypatch.setattr(queue, "claim", flaky_claim)
    registry = JobRegistry()
    registry.register("echo", lambda _ctx: None)
    job_id = _enqueue(session_factory, "echo")

    start_worker(registry)

    wait_for(lambda: _status(session_factory, job_id) is JobStatus.SUCCEEDED)


def test_jobs_run_concurrently_up_to_the_configured_limit(
    start_worker: StartWorker, session_factory: sessionmaker[Session]
) -> None:
    running, peak, lock = [0], [0], threading.Lock()

    def tracked(_ctx: JobContext) -> None:
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.3)
        with lock:
            running[0] -= 1

    registry = JobRegistry()
    registry.register("work", tracked)
    ids = [_enqueue(session_factory, "work") for _ in range(4)]

    start_worker(registry, concurrency=2)

    wait_for(lambda: all(_status(session_factory, i) is JobStatus.SUCCEEDED for i in ids))
    assert peak[0] == 2


# --- Graceful shutdown -----------------------------------------------------------------


def test_shutdown_waits_for_the_running_job(
    start_worker: StartWorker, session_factory: sessionmaker[Session]
) -> None:
    started, release = threading.Event(), threading.Event()

    def blocking(_ctx: JobContext) -> None:
        started.set()
        release.wait(10)

    registry = JobRegistry()
    registry.register("block", blocking)
    queued_later = _enqueue(session_factory, "block")
    worker, thread = start_worker(registry)
    assert started.wait(5)
    queued_after_start = _enqueue(session_factory, "block")

    worker.request_stop()
    thread.join(timeout=0.5)
    assert thread.is_alive(), "the worker must wait for the running job"

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert _status(session_factory, queued_later) is JobStatus.SUCCEEDED
    assert _status(session_factory, queued_after_start) is JobStatus.QUEUED, "no new claims"


def test_shutdown_timeout_hands_the_job_back(
    start_worker: StartWorker, session_factory: sessionmaker[Session]
) -> None:
    started = threading.Event()

    def stuck(ctx: JobContext) -> None:
        started.set()
        ctx.cancelled.wait(10)

    registry = JobRegistry()
    registry.register("stuck", stuck)
    job_id = _enqueue(session_factory, "stuck")
    worker, thread = start_worker(registry, shutdown_timeout_seconds=1)
    assert started.wait(5)

    began = time.monotonic()
    worker.request_stop()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert time.monotonic() - began < 5
    job = _job(session_factory, job_id)
    assert (job.status, job.attempts, job.locked_by) == (JobStatus.QUEUED, 0, None)
    assert job.last_error == "interrupted by worker shutdown"


def test_second_signal_skips_the_wait(
    start_worker: StartWorker, session_factory: sessionmaker[Session]
) -> None:
    started = threading.Event()

    def stuck(ctx: JobContext) -> None:
        started.set()
        ctx.cancelled.wait(30)

    registry = JobRegistry()
    registry.register("stuck", stuck)
    job_id = _enqueue(session_factory, "stuck")
    worker, thread = start_worker(registry, shutdown_timeout_seconds=30)
    assert started.wait(5)

    worker._on_signal(signal.SIGTERM, None)
    thread.join(timeout=0.5)
    assert thread.is_alive()
    worker._on_signal(signal.SIGINT, None)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert _status(session_factory, job_id) is JobStatus.QUEUED


def test_sigterm_stops_a_worker_running_on_the_main_thread(migrated_engine: Engine) -> None:
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    settings = Settings(worker=WorkerSettings.model_validate(FAST))
    worker = Worker(settings, migrated_engine, JobRegistry())
    threading.Timer(0.5, os.kill, args=(os.getpid(), signal.SIGTERM)).start()
    try:
        worker.run()  # returns once SIGTERM has been handled
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


# --- Scheduled jobs -----------------------------------------------------------------------


def test_scheduled_job_never_runs_concurrently_across_workers(
    start_worker: StartWorker, session_factory: sessionmaker[Session]
) -> None:
    running, peak, runs, lock = [0], [0], [0], threading.Lock()

    def tick(_ctx: JobContext) -> None:
        with lock:
            running[0] += 1
            runs[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.25)
        with lock:
            running[0] -= 1

    def setup(worker: Worker) -> None:
        worker.schedule("tick", tick, interval=timedelta(milliseconds=100))

    first, first_thread = start_worker(setup=setup)
    second, second_thread = start_worker(setup=setup)

    wait_for(lambda: runs[0] >= 4)
    for worker, thread in ((first, first_thread), (second, second_thread)):
        worker.request_stop()
        thread.join(timeout=10)

    assert peak[0] == 1
    with session_scope(session_factory) as session:
        assert session.scalar(select(func.count()).select_from(JobLock)) == 0, "leases released"


def test_scheduled_job_is_skipped_while_another_worker_holds_the_lease(
    start_worker: StartWorker, session_factory: sessionmaker[Session]
) -> None:
    runs: list[JobContext] = []
    locks.acquire(session_factory, "scheduled:tick", "someone-else", timedelta(minutes=5))

    start_worker(
        setup=lambda w: w.schedule("tick", runs.append, interval=timedelta(milliseconds=50))
    )

    time.sleep(0.5)
    assert runs == []


def test_failed_scheduled_job_is_reported(
    start_worker: StartWorker, failures: Failures, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_ctx: JobContext) -> None:
        raise ValueError("bad page")

    start_worker(
        setup=lambda w: w.schedule(
            "scrape", broken, interval=timedelta(milliseconds=50), jitter=timedelta(seconds=0.01)
        )
    )

    wait_for(lambda: len(failures) >= 1)
    exc, tags = failures[0]
    assert isinstance(exc, ValueError)
    assert tags == {"job_id": "scrape", "job_type": "scheduled"}


def _until_cancelled(ctx: JobContext) -> None:
    ctx.cancelled.wait(5)


def test_scheduled_job_timeout_is_reported(start_worker: StartWorker, failures: Failures) -> None:
    start_worker(
        setup=lambda w: w.schedule(
            "slow",
            _until_cancelled,
            interval=timedelta(milliseconds=50),
            timeout=timedelta(milliseconds=200),
        )
    )

    wait_for(lambda: len(failures) >= 1)
    assert isinstance(failures[0][0], JobTimeoutError)


def test_scheduled_job_errors_outside_the_handler_are_contained(
    start_worker: StartWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquire_calls: list[object] = []
    release_calls: list[object] = []
    runs: list[JobContext] = []

    def broken_acquire(*args: object, **kwargs: object) -> bool:
        acquire_calls.append(args)
        if len(acquire_calls) == 1:
            raise RuntimeError("database is locked")
        return True

    def broken_release(*args: object) -> None:
        release_calls.append(args)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(locks, "acquire", broken_acquire)
    monkeypatch.setattr(locks, "release", broken_release)
    _, thread = start_worker(
        setup=lambda w: w.schedule("tick", runs.append, interval=timedelta(milliseconds=50))
    )

    wait_for(lambda: len(runs) >= 2 and len(release_calls) >= 2)
    assert thread.is_alive()


def test_stuck_scheduled_job_is_abandoned_at_shutdown(start_worker: StartWorker) -> None:
    started = threading.Event()

    def stuck(ctx: JobContext) -> None:
        started.set()
        ctx.cancelled.wait(10)

    worker, thread = start_worker(
        setup=lambda w: w.schedule("stuck", stuck, interval=timedelta(milliseconds=50)),
        shutdown_timeout_seconds=1,
    )
    assert started.wait(5)

    worker.request_stop()
    thread.join(timeout=10)

    assert not thread.is_alive()


def test_nothing_new_starts_once_stopping(
    migrated_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    runs: list[JobContext] = []
    registry = JobRegistry()
    registry.register("echo", runs.append)
    job_id = _enqueue(session_factory, "echo")
    worker = Worker(Settings(), migrated_engine, registry)

    worker.request_stop()

    assert not worker._run_next()
    worker._run_scheduled("tick", runs.append, timedelta(seconds=1))
    assert runs == []
    assert _status(session_factory, job_id) is JobStatus.QUEUED
