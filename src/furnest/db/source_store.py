"""SqlSourceStore: the ``sources`` table, i.e. per-adapter toggles and health.

Rows are created for every registered adapter with :meth:`SqlSourceStore.ensure`,
so the dashboard can list a source before it has ever run. The scheduler
(:mod:`furnest.pipeline.schedule`) records each run's outcome and, from the
resolved adapter configs, why a source can't run
(:meth:`SqlSourceStore.set_disabled_reasons`); the source health page
(:mod:`furnest.web.routes.sources`) reads it back and sets the toggle.

Recording an outcome can also queue a :data:`SOURCE_ALERT_JOB` in the same
transaction (``alert_after``, from ``DISCORD_ALERT_AFTER_FAILURES``): ``failing`` on
the run that brings ``consecutive_failures`` up to the threshold, and ``recovered``
on the first success after that. Only the crossing alerts, so an outage posts one
alert however long it lasts. :mod:`furnest.notify.alerts` posts them.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.models import Source
from furnest.db.session import session_scope
from furnest.worker import queue

# Long enough for a useful traceback summary, short enough for a table cell.
MAX_ERROR_LENGTH = 2000

# Queue job kind for a source alert; see :mod:`furnest.notify.alerts` for the payload.
SOURCE_ALERT_JOB = "discord.source_alert"
FAILING = "failing"
RECOVERED = "recovered"


class SourceNotFoundError(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"no source named {name!r}")
        self.name = name


@dataclass(frozen=True)
class SourceState:
    name: str
    enabled: bool | None
    last_run_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    listings_found: int
    disabled_reason: str | None = None


def _state(row: Source) -> SourceState:
    return SourceState(
        name=row.name,
        enabled=row.enabled,
        last_run_at=row.last_run_at,
        last_success_at=row.last_success_at,
        consecutive_failures=row.consecutive_failures,
        last_error=row.last_error,
        listings_found=row.listings_found,
        disabled_reason=row.disabled_reason,
    )


class SqlSourceStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def ensure(self, names: Iterable[str]) -> None:
        """Create a row for each name that has none; existing rows are left alone."""
        rows = [{"name": name} for name in sorted(set(names))]
        if not rows:
            return
        statement = insert(Source).values(rows).on_conflict_do_nothing(index_elements=["name"])
        with session_scope(self._session_factory) as session:
            session.execute(statement)

    def get(self, name: str) -> SourceState | None:
        with session_scope(self._session_factory) as session:
            row = session.get(Source, name)
            return None if row is None else _state(row)

    def all(self) -> list[SourceState]:
        with session_scope(self._session_factory) as session:
            return [_state(row) for row in session.scalars(select(Source).order_by(Source.name))]

    def stored_enabled(self) -> dict[str, bool | None]:
        """The dashboard toggle per source, as ``SourceRegistry.configured`` takes it."""
        with session_scope(self._session_factory) as session:
            rows = session.execute(select(Source.name, Source.enabled))
            return {name: enabled for name, enabled in rows}

    def set_enabled(self, name: str, enabled: bool | None) -> None:
        """Set the dashboard toggle; ``None`` clears it so config defaults apply again."""
        self._update(name, enabled=enabled)

    def set_disabled_reasons(self, reasons: Mapping[str, str | None]) -> None:
        """Store why each named source can't run; ``None`` clears a stale reason."""
        for name, reason in sorted(reasons.items()):
            self._update(name, disabled_reason=reason)

    def record_success(
        self, name: str, *, listings_found: int, at: datetime, alert_after: int = 0
    ) -> None:
        """Record a successful run; queue a ``recovered`` alert if it ends an alerted outage."""
        with session_scope(self._session_factory) as session:
            failures = session.scalar(
                select(Source.consecutive_failures).where(Source.name == name)
            )
            if failures is None:
                raise SourceNotFoundError(name)
            session.execute(
                update(Source)
                .where(Source.name == name)
                .values(
                    last_run_at=at,
                    last_success_at=at,
                    consecutive_failures=0,
                    last_error=None,
                    listings_found=listings_found,
                )
            )
            if alert_after > 0 and failures >= alert_after:
                _enqueue_alert(session, name, RECOVERED, failures=failures)

    def record_failure(self, name: str, *, error: str, at: datetime, alert_after: int = 0) -> None:
        """Record a failed run; queue a ``failing`` alert on the ``alert_after``-th in a row."""
        error = error[:MAX_ERROR_LENGTH]
        statement = (
            update(Source)
            .where(Source.name == name)
            .values(
                last_run_at=at,
                consecutive_failures=Source.consecutive_failures + 1,
                last_error=error,
            )
            .returning(Source.consecutive_failures)
        )
        with session_scope(self._session_factory) as session:
            failures = session.scalar(statement)
            if failures is None:
                raise SourceNotFoundError(name)
            if alert_after > 0 and failures == alert_after:
                _enqueue_alert(session, name, FAILING, failures=failures, error=error)

    def _update(self, name: str, **values: object) -> None:
        statement = update(Source).where(Source.name == name).values(**values)
        with session_scope(self._session_factory) as session:
            result = session.execute(statement)
            if result.rowcount == 0:  # type: ignore[attr-defined]
                raise SourceNotFoundError(name)


def _enqueue_alert(session: Session, name: str, event: str, **details: str | int) -> None:
    queue.enqueue(session, SOURCE_ALERT_JOB, {"source": name, "event": event, **details})
