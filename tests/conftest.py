"""Shared pytest configuration. Fixtures common to all test tiers go here."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from furnest.config import env_var_names, get_settings
from furnest.db.engine import create_sqlite_engine
from furnest.db.migrate import upgrade


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Keep tests hermetic: no furnest env vars, no ``.env`` file, no cached settings.

    Settings read ``.env`` from the working directory, so each test runs in its own
    empty temp directory.
    """
    for name in env_var_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """URL of a fresh, empty SQLite file for this test."""
    return f"sqlite:///{tmp_path / 'db' / 'furnest.db'}"


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    """A furnest engine on an empty database (no migrations applied)."""
    engine = create_sqlite_engine(db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def migrated_engine(engine: Engine) -> Engine:
    """``engine``, upgraded to head."""
    upgrade(engine)
    return engine
