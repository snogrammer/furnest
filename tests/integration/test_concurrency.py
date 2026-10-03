"""Two OS processes share one WAL database without ``database is locked``.

This mirrors production: web and worker are separate processes on the same
SQLite file. Each process interleaves reads with short write transactions,
including read-then-write ones, which are the case ``BEGIN IMMEDIATE`` exists for.
"""

import multiprocessing
import os
from multiprocessing.synchronize import Event

import pytest
from sqlalchemy import Engine, func, select

from furnest.db.base import utcnow
from furnest.db.engine import create_sqlite_engine
from furnest.db.models import Job, WorkerHeartbeat
from furnest.db.session import make_session_factory, session_scope

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
