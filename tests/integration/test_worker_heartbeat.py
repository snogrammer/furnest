"""Heartbeat upserts and pruning of rows left by earlier worker processes."""

import os
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from furnest import __version__
from furnest.db.base import utcnow
from furnest.db.models import WorkerHeartbeat
from furnest.db.session import session_scope
from furnest.worker import heartbeat
from furnest.worker.heartbeat import WorkerIdentity

pytestmark = pytest.mark.integration


def _rows(factory: sessionmaker[Session]) -> dict[str, WorkerHeartbeat]:
    with session_scope(factory) as session:
        return {row.worker_id: row for row in session.scalars(select(WorkerHeartbeat))}


def test_identity_is_unique_per_worker() -> None:
    first, second = WorkerIdentity(), WorkerIdentity()

    assert first.worker_id != second.worker_id
    assert first.worker_id.startswith(f"{first.hostname}:{os.getpid()}:")
    assert (first.pid, first.version) == (os.getpid(), __version__)


def test_beat_inserts_then_updates_one_row(session_factory: sessionmaker[Session]) -> None:
    identity = WorkerIdentity()
    now = utcnow()

    heartbeat.beat(session_factory, identity, now=now)
    heartbeat.beat(session_factory, identity, now=now + timedelta(seconds=30))

    [row] = _rows(session_factory).values()
    assert row.worker_id == identity.worker_id
    assert row.started_at == identity.started_at
    assert row.last_seen_at == now + timedelta(seconds=30)


def test_prune_removes_only_long_gone_workers(session_factory: sessionmaker[Session]) -> None:
    now = utcnow()
    gone, recent = WorkerIdentity(), WorkerIdentity()
    heartbeat.beat(session_factory, gone, now=now - timedelta(days=2))
    heartbeat.beat(session_factory, recent, now=now - timedelta(hours=1))

    assert heartbeat.prune(session_factory, timedelta(days=1), now=now) == 1

    assert set(_rows(session_factory)) == {recent.worker_id}
