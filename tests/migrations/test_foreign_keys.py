"""Migrations run with foreign keys off, so table rebuilds don't cascade, and turn them
back on afterwards."""

import pytest
from sqlalchemy import Engine, text

from furnest.db.engine import ForeignKeyViolationError, check_foreign_keys, foreign_keys_off
from furnest.db.migrate import downgrade, upgrade

pytestmark = pytest.mark.migrations


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608


def test_rebuilding_listings_keeps_rows_that_point_at_it(engine: Engine) -> None:
    upgrade(engine, "0006")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO listings (id, source, source_id, url, title, data, content_hash,"
                " first_seen, last_seen, missed_runs, created_at, updated_at) VALUES"
                " (1, 'test', 'a', 'https://example.com/a', 'Place', '{}', 'x',"
                " '2026-10-05', '2026-10-05', 0, '2026-10-05', '2026-10-05')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO listing_images (listing_id, position, url)"
                " VALUES (1, 0, 'https://example.com/1.jpg')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO price_history (listing_id, amount_minor, currency, period,"
                " monthly_minor, observed_at) VALUES (1, 200000, 'USD', 'monthly', 200000,"
                " '2026-10-05')"
            )
        )

    upgrade(engine)  # 0007 rebuilds listings to add its foreign key
    downgrade(engine, "0006")  # and rebuilds it again to drop it

    assert _count(engine, "listings") == 1
    assert _count(engine, "listing_images") == 1
    assert _count(engine, "price_history") == 1


def test_foreign_keys_are_back_on_after_migrating(engine: Engine) -> None:
    upgrade(engine)

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_foreign_keys_off_must_precede_the_transaction(engine: Engine) -> None:
    with (
        engine.begin() as connection,
        pytest.raises(RuntimeError, match="before the transaction"),
        foreign_keys_off(connection),
    ):
        pass


def test_foreign_keys_off_restores_enforcement_even_after_an_error(engine: Engine) -> None:
    with engine.connect() as connection:
        with pytest.raises(ValueError, match="boom"), foreign_keys_off(connection):
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0
            connection.exec_driver_sql("SELECT 1")  # autobegins a transaction
            raise ValueError("boom")

        assert not connection.in_transaction()
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_check_foreign_keys_reports_violations(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as connection, foreign_keys_off(connection):
        connection.execute(
            text(
                "INSERT INTO audit_events (property_id, to_status, actor, created_at)"
                " VALUES (999, 'new', 'system', '2026-10-05')"
            )
        )
        with pytest.raises(ForeignKeyViolationError) as excinfo:
            check_foreign_keys(connection)

    assert excinfo.value.violations[0][0] == "audit_events"
