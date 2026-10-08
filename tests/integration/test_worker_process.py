"""A worker as a real OS process, signalled or killed in the middle of a job.

``test_worker_runtime`` drives shutdown in-process; here the signal really arrives
(SIGTERM handled by ``Worker.run``) or the process just vanishes (SIGKILL), and the
queue row in the shared SQLite file has to end up right for the next worker.
"""

import multiprocessing
import os
import signal
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from multiprocessing.process import BaseProcess

import pytest
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import Settings, WorkerSettings
from furnest.db.engine import create_sqlite_engine
from furnest.db.models import Job, JobStatus
from furnest.db.session import session_scope
from furnest.worker import queue, runtime
from furnest.worker.execution import JobContext, JobRegistry
from furnest.worker.runtime import Worker

pytestmark = pytest.mark.integration

KIND = "work"
# Job timeout for the kill tests. With no lease grace a claim's lease ends this long
# after it was taken, so the next worker can reclaim the row quickly.
SHORT_TIMEOUT = 2


def _handler(mode: str) -> Callable[[JobContext], None]:
    def finishes_soon(_ctx: JobContext) -> None:
        time.sleep(1)

    def hangs(_ctx: JobContext) -> None:
        time.sleep(60)  # ignores ctx.cancelled

    return {"ok": lambda _ctx: None, "slow": finishes_soon, "hang": hangs}[mode]


def _run_worker(db_url: str, mode: str, shutdown_timeout: int, job_timeout: int) -> None:
    """Child process body: one worker, one job kind, real signal handlers."""
    # A crashed worker's lease would otherwise outlive the job by a minute.
    runtime.LEASE_GRACE = timedelta(0)
    registry = JobRegistry()
    registry.register(KIND, _handler(mode))
    settings = Settings(
        worker=WorkerSettings(
            poll_interval_seconds=0.05,
            heartbeat_interval_seconds=1,
            shutdown_timeout_seconds=shutdown_timeout,
            job_timeout_seconds=job_timeout,
        )
    )
    Worker(settings, create_sqlite_engine(db_url), registry).run()


@pytest.fixture
def spawn(db_url: str) -> Iterator[Callable[..., BaseProcess]]:
    ctx = multiprocessing.get_context("spawn")
    started: list[BaseProcess] = []

    def start(mode: str, *, shutdown_timeout: int = 10, job_timeout: int = 30) -> BaseProcess:
        args = (db_url, mode, shutdown_timeout, job_timeout)
        process = ctx.Process(target=_run_worker, args=args)
        process.start()
        started.append(process)
        return process

    yield start
    for process in started:
        if process.is_alive():
            process.kill()
        process.join(timeout=10)


def _enqueue(factory: sessionmaker[Session], max_attempts: int = 5) -> int:
    with session_scope(factory) as session:
        return queue.enqueue(session, KIND, {}, max_attempts=max_attempts).id


def _job(factory: sessionmaker[Session], job_id: int) -> Job:
    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job


def _wait_for_status(
    factory: sessionmaker[Session], job_id: int, status: JobStatus, timeout: float = 30
) -> None:
    deadline = time.monotonic() + timeout
    while _job(factory, job_id).status is not status:
        assert time.monotonic() < deadline, f"job never reached {status}"
        time.sleep(0.05)


def _signal_and_wait(process: BaseProcess, signum: int, timeout: float = 30) -> int | None:
    assert process.pid is not None
    os.kill(process.pid, signum)
    process.join(timeout=timeout)
    return process.exitcode


def test_sigterm_lets_a_running_job_finish(
    spawn: Callable[..., BaseProcess], session_factory: sessionmaker[Session]
) -> None:
    job_id = _enqueue(session_factory)
    worker = spawn("slow")
    _wait_for_status(session_factory, job_id, JobStatus.RUNNING)

    assert _signal_and_wait(worker, signal.SIGTERM) == 0

    job = _job(session_factory, job_id)
    assert (job.status, job.attempts) == (JobStatus.SUCCEEDED, 1)


def test_sigterm_hands_back_a_job_that_outlasts_the_grace_period(
    spawn: Callable[..., BaseProcess], session_factory: sessionmaker[Session]
) -> None:
    job_id = _enqueue(session_factory)
    worker = spawn("hang", shutdown_timeout=1)
    _wait_for_status(session_factory, job_id, JobStatus.RUNNING)

    started = time.monotonic()
    assert _signal_and_wait(worker, signal.SIGTERM) == 0
    assert time.monotonic() - started < 15

    job = _job(session_factory, job_id)
    assert (job.status, job.attempts, job.locked_by) == (JobStatus.QUEUED, 0, None)
    assert job.last_error == "interrupted by worker shutdown"


def test_a_killed_workers_job_is_reclaimed_by_the_next_worker(
    spawn: Callable[..., BaseProcess], session_factory: sessionmaker[Session]
) -> None:
    job_id = _enqueue(session_factory)
    crashed = spawn("hang", job_timeout=SHORT_TIMEOUT)
    _wait_for_status(session_factory, job_id, JobStatus.RUNNING)

    assert _signal_and_wait(crashed, signal.SIGKILL) == -signal.SIGKILL
    assert _job(session_factory, job_id).status is JobStatus.RUNNING  # nobody settled it

    spawn("ok")
    _wait_for_status(session_factory, job_id, JobStatus.SUCCEEDED)
    assert _job(session_factory, job_id).attempts == 2


def test_a_job_killed_on_its_last_attempt_goes_dead(
    spawn: Callable[..., BaseProcess], session_factory: sessionmaker[Session]
) -> None:
    job_id = _enqueue(session_factory, max_attempts=1)
    crashed = spawn("hang", job_timeout=SHORT_TIMEOUT)
    _wait_for_status(session_factory, job_id, JobStatus.RUNNING)
    _signal_and_wait(crashed, signal.SIGKILL)

    spawn("ok")
    _wait_for_status(session_factory, job_id, JobStatus.DEAD)

    job = _job(session_factory, job_id)
    assert (job.attempts, job.last_error) == (1, queue.LOST_LEASE_ERROR)
