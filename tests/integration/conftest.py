"""Fixtures for exercising the web app in-process."""

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from furnest.config import Settings
from furnest.db.base import utcnow
from furnest.db.models import WorkerHeartbeat
from furnest.db.session import make_session_factory, session_scope
from furnest.web.app import create_app

HeartbeatWriter = Callable[[datetime], None]


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    """A client for an app on a migrated database. Entering it runs the lifespan."""
    with TestClient(create_app(Settings(), engine=migrated_engine)) as client:
        yield client


@pytest.fixture
def write_heartbeat(migrated_engine: Engine) -> HeartbeatWriter:
    """Record a worker heartbeat last seen at the given time."""

    def write(last_seen_at: datetime) -> None:
        with session_scope(make_session_factory(migrated_engine)) as session:
            session.merge(
                WorkerHeartbeat(
                    worker_id="worker-1",
                    hostname="worker.example.com",
                    pid=1,
                    version="0.0.0",
                    started_at=last_seen_at - timedelta(minutes=5),
                    last_seen_at=last_seen_at,
                )
            )

    return write


@pytest.fixture
def fresh_heartbeat(write_heartbeat: HeartbeatWriter) -> None:
    write_heartbeat(utcnow())
