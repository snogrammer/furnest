"""``/healthz`` and ``/readyz`` against real SQLite databases."""

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from furnest.config import Settings
from furnest.db.base import utcnow
from furnest.db.engine import create_sqlite_engine
from furnest.db.migrate import head_revision
from furnest.web.app import create_app

pytestmark = pytest.mark.integration

HeartbeatWriter = Callable[[datetime], None]  # the ``write_heartbeat`` fixture


def _readyz(engine: Engine, settings: Settings | None = None) -> tuple[int, dict[str, object]]:
    with TestClient(create_app(settings or Settings(), engine=engine)) as client:
        response = client.get("/readyz")
    return response.status_code, response.json()


def test_healthz_is_ok_without_touching_the_database(tmp_path: Path) -> None:
    # A database path that cannot be opened: liveness must not care.
    engine = create_sqlite_engine(f"sqlite:///{tmp_path}")
    with TestClient(create_app(Settings(), engine=engine)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.usefixtures("fresh_heartbeat")
def test_readyz_ok_when_everything_is_healthy(client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == {"ok": True, "detail": "ok"}
    assert body["checks"]["migrations"] == {"ok": True, "detail": f"at head {head_revision()}"}
    assert body["checks"]["worker"]["ok"] is True


def test_readyz_fails_when_database_unreachable(tmp_path: Path) -> None:
    # tmp_path is a directory, so SQLite cannot open it as a database file.
    engine = create_sqlite_engine(f"sqlite:///{tmp_path}")

    status, body = _readyz(engine)

    assert status == 503
    assert body["status"] == "fail"
    assert body["checks"] == {
        "database": {"ok": False, "detail": "unreachable"},
        "migrations": {"ok": False, "detail": "unknown"},
        "worker": {"ok": False, "detail": "unknown"},
    }


def test_readyz_fails_when_migrations_not_at_head(engine: Engine) -> None:
    status, body = _readyz(engine)

    assert status == 503
    checks = body["checks"]
    assert isinstance(checks, dict)
    assert checks["database"]["ok"] is True
    assert checks["migrations"] == {"ok": False, "detail": f"at none, head {head_revision()}"}


def test_readyz_fails_without_worker_heartbeat(client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 503
    checks = response.json()["checks"]
    assert checks["database"]["ok"] is True
    assert checks["migrations"]["ok"] is True
    assert checks["worker"] == {"ok": False, "detail": "no heartbeat"}


def test_readyz_fails_when_heartbeat_is_stale(
    monkeypatch: pytest.MonkeyPatch, migrated_engine: Engine, write_heartbeat: HeartbeatWriter
) -> None:
    monkeypatch.setenv("WORKER_HEARTBEAT_MAX_AGE_SECONDS", "60")
    write_heartbeat(utcnow() - timedelta(seconds=300))

    status, body = _readyz(migrated_engine)

    assert status == 503
    checks = body["checks"]
    assert isinstance(checks, dict)
    assert checks["worker"]["ok"] is False
    assert checks["worker"]["detail"].startswith("stale (last seen 30")


def test_readyz_heartbeat_threshold_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch, migrated_engine: Engine, write_heartbeat: HeartbeatWriter
) -> None:
    monkeypatch.setenv("WORKER_HEARTBEAT_MAX_AGE_SECONDS", "600")
    write_heartbeat(utcnow() - timedelta(seconds=300))

    status, _ = _readyz(migrated_engine)

    assert status == 200


def test_readyz_treats_future_heartbeat_as_fresh(
    migrated_engine: Engine, write_heartbeat: HeartbeatWriter
) -> None:
    write_heartbeat(utcnow() + timedelta(seconds=5))

    status, body = _readyz(migrated_engine)

    assert status == 200
    checks = body["checks"]
    assert isinstance(checks, dict)
    assert checks["worker"]["detail"] == "last seen 0s ago"


def test_readyz_failure_details_hide_exception_text(tmp_path: Path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path}")

    with TestClient(create_app(Settings(), engine=engine)) as client:
        text = client.get("/readyz").text

    assert "unable to open" not in text
    assert "OperationalError" not in text
    assert str(tmp_path) not in text


@pytest.mark.parametrize(("path", "status"), [("/healthz", 200), ("/readyz", 503)])
def test_health_endpoints_answer_head(client: TestClient, path: str, status: int) -> None:
    response = client.head(path)

    assert response.status_code == status
    assert response.content == b""


@pytest.mark.usefixtures("fresh_heartbeat")
def test_readyz_polling_does_not_log_alembic_noise(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO"):
        client.get("/readyz")

    assert not [r for r in caplog.records if r.name.startswith("alembic")]
