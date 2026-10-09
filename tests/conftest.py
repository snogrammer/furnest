"""Shared pytest configuration. Fixtures common to all test tiers go here."""

import ipaddress
import logging
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
import sentry_sdk
import structlog
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from furnest import logging as furnest_logging
from furnest.config import env_var_names, get_settings
from furnest.db.engine import create_sqlite_engine
from furnest.db.migrate import upgrade
from furnest.db.session import make_session_factory


def _quiet_structlog() -> None:
    """Send unconfigured structlog output through stdlib logging instead of stdout.

    structlog's default ``PrintLogger`` writes to the live ``sys.stdout``. Worker
    threads can log between test phases, when pytest isn't capturing, and those lines
    end up in the progress output. Routed through stdlib, they reach ``caplog``
    during a test and are dropped otherwise.
    """
    structlog.configure(logger_factory=structlog.stdlib.LoggerFactory())


def pytest_configure(config: pytest.Config) -> None:
    _quiet_structlog()
    # Without any handler, stdlib's lastResort would still print WARNING and above.
    logging.getLogger().addHandler(logging.NullHandler())


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


@pytest.fixture(autouse=True)
def reset_observability() -> Iterator[None]:
    """Undo ``configure_logging``/``init_sentry`` so global state never leaks between tests."""
    yield
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, furnest_logging._StderrHandler)]:
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)
    logging.captureWarnings(False)
    structlog.reset_defaults()
    _quiet_structlog()
    structlog.contextvars.clear_contextvars()
    sentry_sdk.get_client().close()
    sentry_sdk.get_global_scope().set_client(None)
    sentry_sdk.get_global_scope().remove_tag("component")


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


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database file upgraded to head, built once per test process.

    Running every migration for each test was most of the suite's runtime; copying a
    finished file is near-free.
    """
    path = tmp_path_factory.mktemp("migrated") / "template.db"
    template = create_sqlite_engine(f"sqlite:///{path}")
    try:
        upgrade(template)
    finally:
        template.dispose()
    # Closing the last connection checkpoints the WAL into the file and deletes it.
    assert not path.with_name(f"{path.name}-wal").exists()
    return path


@pytest.fixture
def migrated_engine(engine: Engine, migrated_template: Path) -> Engine:
    """``engine``, upgraded to head (a copy of ``migrated_template``).

    ``engine`` connects lazily, so the file can be put in place before first use.
    """
    target = Path(engine.url.database or "")
    assert not target.exists(), f"{target} was opened before migrated_engine copied it"
    shutil.copyfile(migrated_template, target)
    return engine


@pytest.fixture
def session_factory(migrated_engine: Engine) -> sessionmaker[Session]:
    """A session factory on the migrated database."""
    return make_session_factory(migrated_engine)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every hostname to a public address, so tests never touch real DNS.

    Tests of the SSRF guard pass their own resolver or override this.
    """
    from furnest import netguard

    def resolve(host: str, port: int) -> list[str]:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return ["93.184.216.34"]
        return [host]

    monkeypatch.setattr(netguard, "default_resolver", resolve)
