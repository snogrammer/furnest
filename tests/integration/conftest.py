"""Fixtures for exercising the web app in-process."""

import re
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine

from furnest.config import Settings
from furnest.db.base import utcnow
from furnest.db.models import WorkerHeartbeat
from furnest.db.session import make_session_factory, session_scope
from furnest.web.app import create_app

HeartbeatWriter = Callable[[datetime], None]

AUTH_TOKEN = "test-dashboard-token-0123456789"  # noqa: S105 - fake secret under test
SESSION_SECRET = "test-session-secret-0123456789"  # noqa: S105 - fake secret under test


@pytest.fixture(autouse=True)
def auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every app needs auth secrets to start. TestClient talks plain HTTP, so cookies
    must not be marked Secure or httpx would never send them back."""
    monkeypatch.setenv("AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("AUTH_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    """An unauthenticated client for an app on a migrated database.

    Entering it runs the lifespan.
    """
    with TestClient(create_app(Settings(), engine=migrated_engine)) as client:
        yield client


LoginFn = Callable[..., Response]  # (client, token=AUTH_TOKEN) -> response


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "page has no CSRF field"
    return match.group(1)


def _login(client: TestClient, token: str = AUTH_TOKEN) -> Response:
    csrf = csrf_from(client.get("/login").text)
    return client.post("/login", data={"token": token, "csrf_token": csrf}, follow_redirects=False)


@pytest.fixture
def auth_token() -> str:
    return AUTH_TOKEN


@pytest.fixture
def login() -> LoginFn:
    """Fetch the login form and submit a token (default: the right one) with its CSRF value."""
    return _login


@pytest.fixture
def csrf() -> Callable[[str], str]:
    """Extract the CSRF form value from a rendered page."""
    return csrf_from


@pytest.fixture
def authed_client(client: TestClient) -> TestClient:
    """``client``, logged in through the login form (session cookie + CSRF cookie)."""
    _login(client)
    return client


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
