"""Smoke tests for the ``furnest`` CLI surface."""

from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from typer.testing import CliRunner

import furnest
from furnest.cli import app
from furnest.db.engine import create_sqlite_engine
from furnest.db.migrate import head_revision, migration_status

runner = CliRunner()


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("web", "worker", "migrate"):
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
