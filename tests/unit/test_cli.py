"""Smoke tests for the ``furnest`` CLI surface."""

import urllib.error
import urllib.request
from datetime import timedelta
from email.message import Message
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import furnest
from furnest.cli import app
from furnest.db.base import utcnow
from furnest.db.engine import create_sqlite_engine
from furnest.db.migrate import head_revision, migration_status
from furnest.worker.heartbeat import WorkerIdentity, beat

runner = CliRunner()


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("web", "worker", "migrate", "healthcheck"):
        assert command in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])

    assert "Usage" in result.output


def test_version_flag_prints_package_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"furnest {furnest.__version__}"


def test_worker_refuses_to_start_on_an_unmigrated_database(
    monkeypatch: pytest.MonkeyPatch, db_url: str
) -> None:
    monkeypatch.setenv("DB_URL", db_url)

    result = runner.invoke(app, ["worker"])

    assert result.exit_code == 1
    assert f"database at revision none, code expects {head_revision()}" in result.output
    assert "furnest migrate" in result.output


def test_worker_runs_until_stopped(monkeypatch: pytest.MonkeyPatch, db_url: str) -> None:
    from furnest.worker.runtime import Worker

    runs: list[Worker] = []
    monkeypatch.setattr(Worker, "run", lambda self: runs.append(self))
    monkeypatch.setenv("DB_URL", db_url)
    assert runner.invoke(app, ["migrate"]).exit_code == 0

    result = runner.invoke(app, ["worker"])

    assert result.exit_code == 0, result.output
    assert len(runs) == 1


def test_worker_refuses_to_start_with_an_unknown_adapter(
    monkeypatch: pytest.MonkeyPatch, db_url: str
) -> None:
    from furnest.worker.runtime import Worker

    runs: list[Worker] = []
    monkeypatch.setattr(Worker, "run", lambda self: runs.append(self))
    monkeypatch.setenv("DB_URL", db_url)
    monkeypatch.setenv("SOURCES_ADAPTERS", '{"no_such_source": {"enabled": true}}')
    assert runner.invoke(app, ["migrate"]).exit_code == 0

    result = runner.invoke(app, ["worker"])

    assert result.exit_code == 1
    assert "unknown adapter(s): no_such_source" in result.output
    assert runs == []


def test_web_serves_app_on_configured_address(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    monkeypatch.setenv("WEB_HOST", "0.0.0.0")  # noqa: S104 - the container setting
    monkeypatch.setenv("WEB_PORT", "8123")
    monkeypatch.setenv("AUTH_TOKEN", "a-test-token-0123456789")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "a-test-secret-0123456789")

    result = runner.invoke(app, ["web"])

    assert result.exit_code == 0, result.output
    [(served, kwargs)] = calls
    assert isinstance(served, FastAPI)
    assert kwargs["host"] == "0.0.0.0"  # noqa: S104
    assert kwargs["port"] == 8123
    assert kwargs["log_config"] is None
    assert kwargs["server_header"] is False
    assert kwargs["proxy_headers"] is False


def test_web_refuses_to_start_without_auth_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(app))

    result = runner.invoke(app, ["web"])

    assert result.exit_code == 1
    assert "AUTH_TOKEN must be set" in result.output
    assert calls == []


def test_migrate_upgrades_database_to_head(monkeypatch: pytest.MonkeyPatch, db_url: str) -> None:
    monkeypatch.setenv("DB_URL", db_url)

    first = runner.invoke(app, ["migrate"])
    second = runner.invoke(app, ["migrate"])

    head = head_revision()
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert f"at revision {head} (head {head})" in second.output
    engine = create_sqlite_engine(db_url)
    try:
        assert migration_status(engine).up_to_date
    finally:
        engine.dispose()


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_healthcheck_web_probes_local_healthz(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []
    timeouts: list[float] = []

    def fake_urlopen(url: str, timeout: float) -> _FakeResponse:
        urls.append(url)
        timeouts.append(timeout)
        return _FakeResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("WEB_HOST", "0.0.0.0")  # noqa: S104 - the container setting
    monkeypatch.setenv("WEB_PORT", "8123")

    result = runner.invoke(app, ["healthcheck", "web"])

    assert result.exit_code == 0, result.output
    assert urls == ["http://127.0.0.1:8123/healthz"]
    assert timeouts == [3]  # well inside the container healthcheck's own timeout
    assert "answered 200" in result.output


def test_healthcheck_web_fails_on_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(url: str, timeout: float) -> _FakeResponse:
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", Message(), None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = runner.invoke(app, ["healthcheck", "web"])

    assert result.exit_code == 1
    assert "answered 503" in result.output


def test_healthcheck_web_fails_when_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(url: str, timeout: float) -> _FakeResponse:
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = runner.invoke(app, ["healthcheck", "web"])

    assert result.exit_code == 1
    assert "unreachable" in result.output


@pytest.mark.parametrize(
    ("age_seconds", "exit_code", "detail"),
    [(5, 0, "heartbeat last seen 5s ago"), (500, 1, "heartbeat stale (last seen 500s ago)")],
)
def test_healthcheck_worker_checks_heartbeat_age(
    monkeypatch: pytest.MonkeyPatch,
    db_url: str,
    session_factory: sessionmaker[Session],
    age_seconds: int,
    exit_code: int,
    detail: str,
) -> None:
    monkeypatch.setenv("DB_URL", db_url)
    beat(session_factory, WorkerIdentity(), now=utcnow() - timedelta(seconds=age_seconds))

    result = runner.invoke(app, ["healthcheck", "worker"])

    assert result.exit_code == exit_code, result.output
    assert detail in result.output


def test_healthcheck_worker_fails_without_heartbeat(
    monkeypatch: pytest.MonkeyPatch, db_url: str, session_factory: sessionmaker[Session]
) -> None:
    monkeypatch.setenv("DB_URL", db_url)

    result = runner.invoke(app, ["healthcheck", "worker"])

    assert result.exit_code == 1
    assert "heartbeat no heartbeat" in result.output


def test_healthcheck_worker_fails_on_unmigrated_database(
    monkeypatch: pytest.MonkeyPatch, db_url: str
) -> None:
    monkeypatch.setenv("DB_URL", db_url)

    result = runner.invoke(app, ["healthcheck", "worker"])

    assert result.exit_code == 1
    assert "heartbeat unknown" in result.output
