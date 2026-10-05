"""What the source health page shows for each source, gathered from the stores.

Every registered adapter gets a row, even one the worker hasn't seen yet (no
``sources`` row), so a newly added source is visible before its first run.

Whether a source is enabled comes from :func:`~furnest.sources.config.resolve_config`
with the stored dashboard toggle, the same as the scheduler. The web process never
probes for a browser itself. It uses the ``disabled_reason`` the worker stored, since
the worker is the process that would have to run the source, or else the one
``resolve_config`` finds without a probe (a missing API key).
"""

from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.orm import Session, sessionmaker

from furnest.config import SourcesSettings
from furnest.db.listing_store import SourceRunState, SqlListingStore
from furnest.db.session import session_scope
from furnest.db.source_store import SourceState, SqlSourceStore
from furnest.pipeline.schedule import RUN_SOURCE
from furnest.sources.config import resolve_config
from furnest.sources.registry import SourceRegistry
from furnest.worker import queue

# Runs listed per source under the summary table.
RECENT_RUNS = 10


class Health(StrEnum):
    OK = "ok"
    FAILING = "failing"
    NEVER_RUN = "never run"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SourceRow:
    state: SourceState
    display_name: str
    enabled: bool
    # Set in SOURCES_ADAPTERS, so the dashboard toggle has no effect.
    pinned: bool
    # Why the source can't run at all, whatever the toggle says.
    disabled_reason: str | None
    failed_runs: int
    pending_job_id: int | None = None
    runs: list[SourceRunState] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.state.name

    @property
    def runnable(self) -> bool:
        return self.enabled and self.disabled_reason is None

    @property
    def can_toggle(self) -> bool:
        return not self.pinned and self.disabled_reason is None

    @property
    def health(self) -> Health:
        if self.disabled_reason is not None:
            return Health.UNAVAILABLE
        if not self.enabled:
            return Health.DISABLED
        if self.state.consecutive_failures:
            return Health.FAILING
        if self.state.last_run_at is None:
            return Health.NEVER_RUN
        return Health.OK


def build_row(
    name: str,
    registry: SourceRegistry,
    settings: SourcesSettings,
    session_factory: sessionmaker[Session],
    *,
    with_runs: bool = False,
) -> SourceRow | None:
    """One source's row, or ``None`` if no adapter has that name."""
    rows = build_rows(registry, settings, session_factory, only=name, with_runs=with_runs)
    return rows[0] if rows else None


def build_rows(
    registry: SourceRegistry,
    settings: SourcesSettings,
    session_factory: sessionmaker[Session],
    *,
    only: str | None = None,
    with_runs: bool = True,
) -> list[SourceRow]:
    """Every registered source (or just ``only``), sorted by name."""
    states = {state.name: state for state in SqlSourceStore(session_factory).all()}
    listings = SqlListingStore(session_factory)
    failed = listings.failure_counts()
    rows = []
    for name in registry.names():
        adapter = registry.get(name)
        if adapter is None or (only is not None and name != only):
            continue
        state = states.get(name) or SourceState(
            name=name,
            enabled=None,
            last_run_at=None,
            last_success_at=None,
            consecutive_failures=0,
            last_error=None,
            listings_found=0,
        )
        config = resolve_config(adapter, settings, state.enabled)
        with session_scope(session_factory) as session:
            job = queue.pending(session, RUN_SOURCE, source=name)
            pending_job_id = None if job is None else job.id
        rows.append(
            SourceRow(
                state=state,
                display_name=adapter.display_name,
                enabled=config.enabled,
                pinned=config.enabled_pinned,
                disabled_reason=state.disabled_reason or config.disabled_reason,
                failed_runs=failed.get(name, 0),
                pending_job_id=pending_job_id,
                runs=list(listings.runs(name, limit=RECENT_RUNS)) if with_runs else [],
            )
        )
    return rows
