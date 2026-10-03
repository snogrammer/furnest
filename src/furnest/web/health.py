"""Readiness checks behind ``/readyz``.

The web process is ready when all three hold:

* ``database``: the SQLite file can be opened and queried;
* ``migrations``: the schema is at the Alembic head, so code and tables agree;
* ``worker``: some worker wrote a heartbeat recently (``WORKER_HEARTBEAT_MAX_AGE_SECONDS``).
  Without a live worker nothing is scraped or sent, so the deployment is not healthy
  even though pages still render.

Each check runs on its own and never raises. ``/readyz`` is unauthenticated, so the
reported details are short fixed phrases; exception text is only logged.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, func, select, text

from furnest.db.base import utcnow
from furnest.db.migrate import migration_status
from furnest.db.models import WorkerHeartbeat
from furnest.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str


@dataclass(frozen=True)
class ReadinessReport:
    checks: dict[str, CheckResult]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks.values())


def check_readiness(
    engine: Engine, *, max_heartbeat_age: timedelta, now: datetime | None = None
) -> ReadinessReport:
    return ReadinessReport(
        checks={
            "database": _check_database(engine),
            "migrations": _check_migrations(engine),
            "worker": check_worker(engine, max_heartbeat_age, now),
        }
    )


def _check_database(engine: Engine) -> CheckResult:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        log.exception("readiness_check_failed", check="database")
        return CheckResult(ok=False, detail="unreachable")
    return CheckResult(ok=True, detail="ok")


def _check_migrations(engine: Engine) -> CheckResult:
    try:
        status = migration_status(engine)
    except Exception:
        log.exception("readiness_check_failed", check="migrations")
        return CheckResult(ok=False, detail="unknown")
    if not status.up_to_date:
        return CheckResult(ok=False, detail=f"at {status.current or 'none'}, head {status.head}")
    return CheckResult(ok=True, detail=f"at head {status.head}")


def check_worker(engine: Engine, max_age: timedelta, now: datetime | None = None) -> CheckResult:
    """Is the newest worker heartbeat younger than ``max_age``?

    Also used by ``furnest healthcheck worker``, the worker container's probe.
    """
    try:
        with engine.connect() as connection:
            last_seen = connection.execute(
                select(func.max(WorkerHeartbeat.last_seen_at))
            ).scalar_one()
    except Exception:
        log.exception("readiness_check_failed", check="worker")
        return CheckResult(ok=False, detail="unknown")
    if last_seen is None:
        return CheckResult(ok=False, detail="no heartbeat")
    # Clamp at zero: a worker clock slightly ahead of ours is still fresh.
    age = max((now or utcnow()) - last_seen, timedelta(0))
    seconds = int(age.total_seconds())
    if age > max_age:
        return CheckResult(ok=False, detail=f"stale (last seen {seconds}s ago)")
    return CheckResult(ok=True, detail=f"last seen {seconds}s ago")
