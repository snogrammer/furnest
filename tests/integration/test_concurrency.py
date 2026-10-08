"""OS processes share one WAL database without ``database is locked``.

This mirrors production: web and worker are separate processes on the same
SQLite file. Each process interleaves reads with short write transactions,
including read-then-write ones, which are the case ``BEGIN IMMEDIATE`` exists for.
The last test shows what happens when a writer holds the lock past ``busy_timeout``.
"""

import multiprocessing
import os
import signal
import time
from datetime import timedelta
from multiprocessing.synchronize import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import Settings, WorkerSettings
from furnest.db.base import utcnow
from furnest.db.criteria_store import CriteriaStore
from furnest.db.engine import create_sqlite_engine
from furnest.db.models import Job, JobStatus, WorkerHeartbeat
from furnest.db.session import make_session_factory, session_scope
from furnest.domain.criteria import Criteria
from furnest.pipeline.rerun import RERUN_CRITERIA
from furnest.web.app import create_app
from furnest.worker import locks
from furnest.worker.execution import JobContext, JobRegistry
from furnest.worker.runtime import Worker

pytestmark = pytest.mark.integration

ITERATIONS = 200


def _hammer(db_url: str, worker_id: str, start: Event) -> None:
    """Child process body; any exception gives a non-zero exit code."""
    engine = create_sqlite_engine(db_url)
    factory = make_session_factory(engine)
    start.wait()
    for i in range(ITERATIONS):
        with session_scope(factory) as session:
            # Read, then write in the same transaction.
            session.scalar(select(func.count()).select_from(Job))
            session.add(Job(kind="test", payload={"worker": worker_id, "i": i}))
        with session_scope(factory) as session:
            session.merge(
                WorkerHeartbeat(
                    worker_id=worker_id,
                    hostname="test",
                    pid=os.getpid(),
                    version="0",
                    last_seen_at=utcnow(),
                )
            )
        with session_scope(factory) as session:
            session.scalars(select(Job).where(Job.payload["worker"].as_string() == worker_id))
    engine.dispose()


def test_two_processes_read_and_write_concurrently(db_url: str, migrated_engine: Engine) -> None:
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    workers = [
        ctx.Process(target=_hammer, args=(db_url, name, start)) for name in ("web", "worker")
    ]
    for process in workers:
        process.start()
    start.set()
    for process in workers:
        process.join(timeout=120)

    assert [p.exitcode for p in workers] == [0, 0]
    with session_scope(make_session_factory(migrated_engine)) as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 2 * ITERATIONS
        assert session.scalar(select(func.count()).select_from(WorkerHeartbeat)) == 2


# --- The real web app and a real worker on the same file --------------------------------

REQUESTS = 40


def _web(db_url: str, token: str, start: Event) -> None:
    """Child process: the dashboard app queueing re-runs while reading the API."""
    app = create_app(Settings(), engine=create_sqlite_engine(db_url))
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        start.wait()
        for _ in range(REQUESTS):
            queued = client.post("/criteria/rerun", follow_redirects=False)
            assert queued.status_code == 303, queued.text
            assert client.get("/api/v1/properties").status_code == 200


def _write_in_handler(db_url: str) -> JobRegistry:
    factory = make_session_factory(create_sqlite_engine(db_url))

    def handler(ctx: JobContext) -> None:
        # A read-then-write transaction of its own, as real handlers make.
        name = f"test:{ctx.job_id}"
        assert locks.acquire(factory, name, "handler", timedelta(minutes=1))
        locks.release(factory, name, "handler")

    registry = JobRegistry()
    registry.register(RERUN_CRITERIA, handler)
    return registry


def _worker(db_url: str, start: Event) -> None:
    """Child process: a worker draining the queue with two consumers."""
    settings = Settings(
        worker=WorkerSettings(
            poll_interval_seconds=0.01, heartbeat_interval_seconds=1, concurrency=2
        )
    )
    worker = Worker(settings, create_sqlite_engine(db_url), _write_in_handler(db_url))
    start.wait()
    worker.run()  # until SIGTERM


def _statuses(factory: sessionmaker[Session]) -> list[JobStatus]:
    with session_scope(factory) as session:
        return list(session.scalars(select(Job.status)))


def test_web_and_worker_processes_write_the_same_database(
    db_url: str,
    session_factory: sessionmaker[Session],
    auth_token: str,
    capfd: pytest.CaptureFixture[str],
) -> None:
    CriteriaStore(session_factory).save(Criteria(min_beds=1), created_by="cli")
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    web = ctx.Process(target=_web, args=(db_url, auth_token, start))
    worker = ctx.Process(target=_worker, args=(db_url, start))
    for process in (web, worker):
        process.start()
    try:
        start.set()
        web.join(timeout=120)
        assert web.exitcode == 0

        deadline = time.monotonic() + 60
        while (statuses := _statuses(session_factory)).count(JobStatus.SUCCEEDED) < REQUESTS:
            assert time.monotonic() < deadline, f"jobs not drained: {statuses}"
            time.sleep(0.1)
        assert statuses == [JobStatus.SUCCEEDED] * REQUESTS

        assert worker.pid is not None
        os.kill(worker.pid, signal.SIGTERM)
        worker.join(timeout=30)
        assert worker.exitcode == 0
    finally:
        for process in (web, worker):
            if process.is_alive():
                process.kill()
    assert "database is locked" not in capfd.readouterr().out


# --- Past busy_timeout ------------------------------------------------------------------


def test_a_writer_held_past_busy_timeout_fails_the_other_writer(
    db_url: str, migrated_engine: Engine
) -> None:
    """Short transactions keep this from happening; when it does, the error surfaces.

    The worker retries its job-outcome writes on this error (``runtime._settle``).
    """
    impatient = create_sqlite_engine(db_url, busy_timeout_ms=100)
    with migrated_engine.connect() as holder:
        holder.execute(text("UPDATE jobs SET attempts = attempts"))  # holds the write lock
        started = time.monotonic()
        with (
            pytest.raises(OperationalError, match="database is locked"),
            impatient.connect() as other,
        ):
            other.execute(text("SELECT 1"))  # BEGIN IMMEDIATE wants the write lock too
        assert time.monotonic() - started >= 0.1
        holder.rollback()
    impatient.dispose()
