"""App factory lifespan: engine ownership and ``DB_AUTO_MIGRATE``."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from furnest.config import Settings
from furnest.db.engine import create_sqlite_engine
from furnest.db.migrate import migration_status
from furnest.web.app import create_app

pytestmark = pytest.mark.integration


def test_auto_migrate_upgrades_on_start(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> None:
    monkeypatch.setenv("DB_AUTO_MIGRATE", "true")

    with TestClient(create_app(Settings(), engine=engine)):
        pass

    assert migration_status(engine).up_to_date


def test_without_auto_migrate_database_is_left_alone(engine: Engine) -> None:
    with TestClient(create_app(Settings(), engine=engine)):
        pass

    assert migration_status(engine).current is None


def test_engine_built_from_settings_when_not_injected(
    monkeypatch: pytest.MonkeyPatch, db_url: str
) -> None:
    monkeypatch.setenv("DB_URL", db_url)
    monkeypatch.setenv("DB_AUTO_MIGRATE", "true")

    with TestClient(create_app()) as client:
        assert client.get("/readyz").json()["checks"]["migrations"]["ok"] is True

    engine = create_sqlite_engine(db_url)
    try:
        assert migration_status(engine).up_to_date
    finally:
        engine.dispose()
