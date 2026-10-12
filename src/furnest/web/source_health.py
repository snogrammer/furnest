"""What the source health page shows: each source, and each shared credential.

Every registered adapter gets a row, even one the worker hasn't seen yet (no
``sources`` row), so a newly added source is visible before its first run.

Whether a source is enabled, and its settings, come from
:func:`~furnest.sources.config.resolve_config` with what the dashboard stored (the
toggle, the settings, the credentials), the same as the scheduler. The web process
never probes for a browser itself, so that one reason comes from the worker, which
stores it; every other reason (not built, no token) is worked out here, fresh, so a
token just added shows at once.

Apify sources also show what they cost: each one's projected monthly spend at its
interval and ``max_results``, and the account's usage this cycle against
``SOURCES_APIFY_MONTHLY_BUDGET_USD`` (:class:`ApifySpend`). Once the budget is
reached they show as paused, with the same reason the worker skips their runs for.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.orm import Session, sessionmaker

from furnest.config import SourceOverrides, SourcesSettings
from furnest.db.criteria_store import CriteriaStore
from furnest.db.listing_store import SourceRunState, SqlListingStore
from furnest.db.session import session_scope
from furnest.db.source_store import SourceState, SqlSourceStore
from furnest.domain.criteria import criteria_cities
from furnest.pipeline.schedule import RUN_SOURCE
from furnest.sources.apify import ApifyAdapter
from furnest.sources.base import AdapterConfig, Blocker, Credential
from furnest.sources.budget import ApifyBudget, ApifyUsage
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
    PAUSED = "paused"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CredentialUser:
    name: str
    display_name: str
    # The source's own ``api_key`` is set in SOURCES_ADAPTERS, so it ignores this one.
    env_pinned: bool


@dataclass(frozen=True)
class CredentialRow:
    credential: Credential
    # A token is stored from the dashboard.
    stored: bool
    users: list[CredentialUser]

    @property
    def key(self) -> str:
        return self.credential.key


@dataclass(frozen=True)
class SourceRow:
    state: SourceState
    display_name: str
    config: AdapterConfig
    # The config with nothing stored from the dashboard: what an empty field falls back to.
    defaults: AdapterConfig
    # Why the source can't run at all, whatever the toggle says.
    disabled_reason: str | None
    failed_runs: int
    credential: Credential | None = None
    search_locations_hint: str = ""
    # The active criteria's cities, when this source takes place names and has no
    # locations yet: what the settings form offers as a one-click fill.
    criteria_cities: tuple[str, ...] = ()
    implemented: bool = True
    pending_job_id: int | None = None
    runs: list[SourceRunState] = field(default_factory=list)
    # Billed sources only: a month of runs at the current interval and max results.
    projected_monthly_usd: float | None = None
    # Why the Apify budget stops this source starting a run (see furnest.sources.budget).
    budget_reason: str | None = None
    # The adapter's own defaults, when it has them (see SourceAdapter).
    default_interval_minutes: int | None = None
    default_max_results: int | None = None

    @property
    def name(self) -> str:
        return self.state.name

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def pinned(self) -> bool:
        """``enabled`` is set in SOURCES_ADAPTERS, so the dashboard toggle has no effect."""
        return self.config.enabled_pinned

    @property
    def overrides(self) -> SourceOverrides:
        """What the dashboard stored, for the settings form."""
        return self.state.overrides

    @property
    def runnable(self) -> bool:
        return self.enabled and self.disabled_reason is None and self.budget_reason is None

    @property
    def can_toggle(self) -> bool:
        return not self.pinned and self.disabled_reason is None

    @property
    def can_edit(self) -> bool:
        """Has a settings form: anything but a stub."""
        return self.implemented

    @property
    def missing_credential(self) -> bool:
        return self.config.blocker is Blocker.NO_CREDENTIAL and self.credential is not None

    @property
    def health(self) -> Health:
        if self.disabled_reason is not None:
            return Health.UNAVAILABLE
        if not self.enabled:
            return Health.DISABLED
        if self.budget_reason is not None:
            return Health.PAUSED
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
    budget: ApifyBudget | None = None,
) -> SourceRow | None:
    """One source's row, or ``None`` if no adapter has that name."""
    rows = build_rows(
        registry, settings, session_factory, only=name, with_runs=with_runs, budget=budget
    )
    return rows[0] if rows else None


def build_rows(
    registry: SourceRegistry,
    settings: SourcesSettings,
    session_factory: sessionmaker[Session],
    *,
    only: str | None = None,
    with_runs: bool = True,
    budget: ApifyBudget | None = None,
) -> list[SourceRow]:
    """Every registered source (or just ``only``), sorted by name. With ``budget``,
    an enabled Apify source the budget stops gets its ``budget_reason``."""
    store = SqlSourceStore(session_factory)
    states = {state.name: state for state in store.all()}
    credentials = store.stored(registry.credential_keys()).credentials
    listings = SqlListingStore(session_factory)
    failed = listings.failure_counts()
    active = CriteriaStore(session_factory).active()
    cities = () if active is None else criteria_cities(active.criteria)
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
        config = resolve_config(adapter, settings, state.stored, credentials=credentials)
        defaults = resolve_config(adapter, settings, credentials=credentials)
        with session_scope(session_factory) as session:
            job = queue.pending(session, RUN_SOURCE, source=name)
            pending_job_id = None if job is None else job.id
        projected = budget_reason = None
        if issubclass(adapter, ApifyAdapter):
            projected = adapter.monthly_cost_usd(config)
            if budget is not None and config.enabled:
                budget_reason = budget.blocked_reason(adapter, config)
        rows.append(
            SourceRow(
                state=state,
                display_name=adapter.display_name,
                config=config,
                defaults=defaults,
                disabled_reason=config.disabled_reason or state.disabled_reason,
                failed_runs=failed.get(name, 0),
                credential=adapter.credential,
                search_locations_hint=adapter.search_locations_hint,
                criteria_cities=(
                    cities
                    if adapter.search_locations_are_places and not config.search_locations
                    else ()
                ),
                implemented=adapter.implemented,
                pending_job_id=pending_job_id,
                runs=list(listings.runs(name, limit=RECENT_RUNS)) if with_runs else [],
                projected_monthly_usd=projected,
                budget_reason=budget_reason,
                default_interval_minutes=adapter.default_interval_minutes,
                default_max_results=adapter.default_max_results,
            )
        )
    return rows


@dataclass(frozen=True)
class ApifySpend:
    """The Sources page's Apify panel: this cycle's usage (``None`` when there's no
    token or Apify can't be read), the budget, and what the sources that are on would
    cost in a month."""

    usage: ApifyUsage | None
    budget_usd: float
    # (display name, projected monthly USD) for each Apify source that is on.
    projections: list[tuple[str, float]]

    @property
    def limit_usd(self) -> float:
        """The budget, or the plan's own limit if that's lower."""
        if self.usage is None:
            return self.budget_usd
        return min(self.budget_usd, self.usage.plan_limit_usd)

    @property
    def projected_usd(self) -> float:
        return sum(cost for _, cost in self.projections)

    @property
    def over_budget(self) -> bool:
        return self.projected_usd > self.limit_usd


def build_apify_spend(rows: list[SourceRow], budget: ApifyBudget) -> ApifySpend | None:
    """The Apify panel for ``rows``, or ``None`` if no source is billed through Apify."""
    billed = [row for row in rows if row.projected_monthly_usd is not None]
    if not billed:
        return None
    token = next((row.config.api_key for row in billed if row.config.api_key is not None), None)
    usage = budget.usage(token.get_secret_value()) if token is not None else None
    return ApifySpend(
        usage=usage,
        budget_usd=budget.budget_usd,
        projections=[
            (row.display_name, row.projected_monthly_usd or 0.0)
            for row in billed
            if row.enabled and row.disabled_reason is None
        ],
    )


def build_credentials(
    registry: SourceRegistry,
    settings: SourcesSettings,
    session_factory: sessionmaker[Session],
) -> list[CredentialRow]:
    """Each shared credential, whether a token is stored, and the sources that use it."""
    store = SqlSourceStore(session_factory)
    stored = store.stored(registry.credential_keys()).credentials
    rows = []
    for credential in registry.credentials():
        users = [
            CredentialUser(
                name=adapter.name,
                display_name=adapter.display_name,
                env_pinned=(
                    adapter.name in settings.adapters
                    and settings.adapters[adapter.name].api_key is not None
                ),
            )
            for adapter in registry.credential_users(credential.key)
        ]
        rows.append(
            CredentialRow(credential=credential, stored=credential.key in stored, users=users)
        )
    return rows
