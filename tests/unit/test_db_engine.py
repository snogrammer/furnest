"""Engine pragmas, session scope and UTC timestamps."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import Engine, func, select, text

from furnest.config import Settings
from furnest.db.engine import create_engine_from_settings, create_sqlite_engine
from furnest.db.models import Job, JobStatus
from furnest.db.session import make_session_factory, session_scope


@pytest.mark.parametrize(
    ("pragma", "expected"),
    [("journal_mode", "wal"), ("foreign_keys", 1), ("synchronous", 1), ("busy_timeout", 5000)],
)
def test_connection_pragmas(engine: Engine, pragma: str, expected: object) -> None:
    with engine.connect() as connection:
        assert connection.exec_driver_sql(f"PRAGMA {pragma}").scalar() == expected


def test_busy_timeout_comes_from_settings(db_url: str) -> None:
    engine = create_engine_from_settings(
        Settings.model_validate({"db": {"url": db_url, "busy_timeout_ms": 1234}})
    )
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 1234
    finally:
        engine.dispose()


def test_rejects_non_sqlite_urls() -> None:
    with pytest.raises(ValueError, match="only SQLite"):
        create_sqlite_engine("postgresql://db.example.com/furnest")


def test_session_scope_commits(migrated_engine: Engine) -> None:
    factory = make_session_factory(migrated_engine)

    with session_scope(factory) as session:
        session.add(Job(kind="test"))

    with session_scope(factory) as session:
        job = session.scalars(select(Job)).one()
        assert job.status is JobStatus.QUEUED
        assert job.payload == {}
        assert job.attempts == 0


def test_session_scope_rolls_back_on_error(migrated_engine: Engine) -> None:
    factory = make_session_factory(migrated_engine)

    with pytest.raises(RuntimeError), session_scope(factory) as session:
        session.add(Job(kind="test"))
        session.flush()
        raise RuntimeError

    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_timestamps_round_trip_as_aware_utc(migrated_engine: Engine) -> None:
    factory = make_session_factory(migrated_engine)
    run_after = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=-7)))

    with session_scope(factory) as session:
        session.add(Job(kind="test", run_after=run_after))

    with session_scope(factory) as session:
        job = session.scalars(select(Job)).one()
        assert job.run_after == run_after
        assert job.run_after.tzinfo is UTC
    with migrated_engine.connect() as connection:
        stored = connection.execute(text("SELECT run_after FROM jobs")).scalar()
        assert stored == "2026-01-01 19:00:00.000000"


def test_naive_datetimes_are_rejected(migrated_engine: Engine) -> None:
    factory = make_session_factory(migrated_engine)

    with pytest.raises(Exception, match="naive datetime"), session_scope(factory) as session:
        session.add(Job(kind="test", run_after=datetime(2026, 1, 1)))
        session.flush()


def test_job_status_check_constraint(migrated_engine: Engine) -> None:
    with pytest.raises(Exception, match="CHECK constraint failed"), migrated_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO jobs (kind, payload, status, attempts, max_attempts, run_after,"
                " created_at, updated_at) VALUES ('x', '{}', 'bogus', 0, 5, '2026-01-01',"
                " '2026-01-01', '2026-01-01')"
            )
        )
