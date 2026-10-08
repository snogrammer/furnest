"""``/metrics`` is off by default, and Bearer-protected when switched on."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from furnest.config import Settings
from furnest.db.base import utcnow
from furnest.db.models import Job, Source
from furnest.db.session import make_session_factory, session_scope
from furnest.web.app import create_app

from .conftest import AUTH_TOKEN

pytestmark = pytest.mark.integration


def test_metrics_is_absent_by_default(migrated_engine: Engine) -> None:
    with TestClient(create_app(Settings(), engine=migrated_engine)) as client:
        response = client.get("/metrics", headers={"Authorization": f"Bearer {AUTH_TOKEN}"})
    assert response.status_code == 404


def test_metrics_requires_the_bearer_token(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("METRICS_ENABLED", "true")
    with TestClient(create_app(Settings(), engine=migrated_engine)) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_metrics_reports_database_state(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("METRICS_ENABLED", "true")
    factory = make_session_factory(migrated_engine)
    with session_scope(factory) as session:
        session.add(Source(name="example_source", consecutive_failures=2, last_run_at=utcnow()))
        session.add(Job(kind="send_email"))
    with TestClient(create_app(Settings(), engine=migrated_engine)) as client:
        response = client.get("/metrics", headers={"Authorization": f"Bearer {AUTH_TOKEN}"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert 'furnest_source_consecutive_failures{source="example_source"} 2' in body
    assert 'furnest_jobs{status="queued"} 1' in body
    assert "furnest_notifications_sent 0" in body
