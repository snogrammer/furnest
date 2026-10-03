"""Every migration upgrades, downgrades and re-upgrades cleanly on an empty DB."""

import pytest
from sqlalchemy import Engine, inspect

from furnest.db.migrate import downgrade, head_revision, migration_status, upgrade

pytestmark = pytest.mark.migrations

TABLES = {"settings", "worker_heartbeat", "job_locks", "jobs", "criteria_versions"}


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def test_empty_database_is_not_up_to_date(engine: Engine) -> None:
    status = migration_status(engine)

    assert status.current is None
    assert status.head == head_revision()
    assert status.up_to_date is False


def test_upgrade_downgrade_upgrade_round_trip(engine: Engine) -> None:
    upgrade(engine)
    assert _tables(engine) >= TABLES
    assert migration_status(engine).up_to_date

    downgrade(engine, "base")
    assert _tables(engine) == set()
    assert migration_status(engine).current is None

    upgrade(engine)
    assert _tables(engine) >= TABLES
    assert migration_status(engine).up_to_date


def test_upgrade_is_idempotent(migrated_engine: Engine) -> None:
    upgrade(migrated_engine)

    assert migration_status(migrated_engine).up_to_date
