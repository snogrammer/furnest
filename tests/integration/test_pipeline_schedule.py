"""Per-source scheduling: which sources get a job, what a run records, and how a
failing, slow or stuck source is kept from affecting the others."""

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import (
    AdapterOverrides,
    InMemorySettingsStore,
    Settings,
    SourceOverrides,
    SourcesSettings,
    WorkerSettings,
)
from furnest.db.criteria_store import CriteriaStore
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import Job, JobLock, JobStatus, SourceRunStatus
from furnest.db.session import session_scope
from furnest.db.source_store import SourceState, SqlSourceStore
from furnest.domain import Criteria, NormalizedListing
from furnest.matching.amenities import KeywordAmenityExtractor
from furnest.pipeline.ingest import ListingIngestor
from furnest.pipeline.schedule import (
    REFRESH_JOB,
    RUN_SOURCE,
    SCHEDULE_REFRESH_INTERVAL,
    RefreshSchedules,
    RunSourceNow,
    SourceRun,
    job_name,
    register_run_now,
    schedule_sources,
)
from furnest.sources import Capabilities, Credential, SourceAdapter
from furnest.sources.apify import ApifyAdapter
from furnest.sources.budget import USAGE_KEY, ApifyBudget, ApifyUsage
from furnest.sources.registry import SourceRegistry
from furnest.worker import locks, queue, runtime
from furnest.worker.execution import Handler, JobContext, JobRegistry, JobTimeoutError
from furnest.worker.runtime import Worker

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
FAST = {
    "heartbeat_interval_seconds": 1,
    "poll_interval_seconds": 0.05,
    "concurrency": 1,
    "shutdown_timeout_seconds": 10,
}
TICK = timedelta(milliseconds=50)

Failures = list[tuple[BaseException, dict[str, Any]]]
StartWorker = Callable[..., tuple[Worker, threading.Thread]]


def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


def listing(source: str, source_id: str) -> NormalizedListing:
    return NormalizedListing(
        source=source,
        source_id=source_id,
        url=f"https://example.com/{source}/{source_id}",  # type: ignore[arg-type]
        title="Furnished place",
        beds=2,
    )


# --- Fake adapters -----------------------------------------------------------------------
# ``SourceRun`` builds a new adapter instance per run, so behaviour lives on the class.


class Good(SourceAdapter):
    name: ClassVar[str] = "good"
    display_name: ClassVar[str] = "Good"
    default_enabled: ClassVar[bool] = True
    searches: ClassVar[int] = 0

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        type(self).searches += 1
        return [listing(self.name, "1"), listing(self.name, "2")]


class Broken(Good):
    name: ClassVar[str] = "broken"
    display_name: ClassVar[str] = "Broken"

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        raise RuntimeError("blocked by captcha")


class OffByDefault(Good):
    name: ClassVar[str] = "off_by_default"
    display_name: ClassVar[str] = "Off by default"
    default_enabled: ClassVar[bool] = False


class PinnedOff(Good):
    name: ClassVar[str] = "pinned_off"
    display_name: ClassVar[str] = "Pinned off"


class NeedsBrowser(Good):
    name: ClassVar[str] = "needs_browser"
    display_name: ClassVar[str] = "Needs browser"
    capabilities: ClassVar[Capabilities] = Capabilities(needs_browser=True)


class Keyed(Good):
    name: ClassVar[str] = "keyed"
    display_name: ClassVar[str] = "Keyed"
    capabilities: ClassVar[Capabilities] = Capabilities(has_api=True, requires_api_key=True)
    credential: ClassVar[Credential | None] = Credential("keyed_provider", "Keyed token")
    tokens: ClassVar[list[str]] = []

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        assert self.config.api_key is not None
        type(self).tokens.append(self.config.api_key.get_secret_value())
        return [listing(self.name, "1")]


class Slow(Good):
    """Tracks how many runs are inside ``search`` at once."""

    name: ClassVar[str] = "slow"
    display_name: ClassVar[str] = "Slow"
    lock: ClassVar[threading.Lock] = threading.Lock()
    running: ClassVar[int] = 0
    peak: ClassVar[int] = 0

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        cls = type(self)
        with cls.lock:
            cls.running += 1
            cls.searches += 1
            cls.peak = max(cls.peak, cls.running)
        time.sleep(0.25)
        with cls.lock:
            cls.running -= 1
        return [listing(self.name, "1")]


class Stuck(Good):
    """Blocks in ``search`` until the test releases it."""

    name: ClassVar[str] = "stuck"
    display_name: ClassVar[str] = "Stuck"
    release: ClassVar[threading.Event] = threading.Event()

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        type(self).release.wait(10)
        return [listing(self.name, "1")]


class Billed(ApifyAdapter):
    """An Apify source that never calls Apify: ``search`` just counts."""

    name: ClassVar[str] = "billed"
    display_name: ClassVar[str] = "Billed"
    default_enabled: ClassVar[bool] = True
    job: ClassVar[str] = "someone/some-actor"
    site: ClassVar[str] = "https://example.com"
    start_cost_usd: ClassVar[float] = 0.05
    result_cost_usd: ClassVar[float] = 0.01
    searches: ClassVar[int] = 0

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        type(self).searches += 1
        return [listing(self.name, "1")]

    def build_input(self, criteria: Criteria) -> dict[str, Any]:
        return {}

    def parse_items(
        self, items: list[dict[str, Any]], run_input: Mapping[str, Any]
    ) -> list[NormalizedListing]:
        return []


@pytest.fixture(autouse=True)
def reset_adapters() -> None:
    Good.searches = Broken.searches = Slow.searches = OffByDefault.searches = 0
    Billed.searches = 0
    Keyed.tokens = []
    Slow.running = Slow.peak = 0
    Stuck.release = threading.Event()


# --- Fixtures ----------------------------------------------------------------------------


@pytest.fixture
def criteria(session_factory: sessionmaker[Session]) -> None:
    CriteriaStore(session_factory).save(Criteria(furnished_required=False), created_by="cli")


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlSourceStore:
    return SqlSourceStore(session_factory)


@pytest.fixture
def ingestor(session_factory: sessionmaker[Session]) -> ListingIngestor:
    return ListingIngestor(
        session_factory, extractor=KeywordAmenityExtractor(), settings=SourcesSettings()
    )


@pytest.fixture
def make_run(store: SqlSourceStore, ingestor: ListingIngestor) -> Callable[..., SourceRun]:
    def make(adapter: type[SourceAdapter], settings: SourcesSettings | None = None) -> SourceRun:
        store.ensure([adapter.name])
        return SourceRun(
            adapter,
            settings=settings or SourcesSettings(),
            store=store,
            ingestor=ingestor,
            clock=lambda: T0,
        )

    return make


@pytest.fixture
def failures(monkeypatch: pytest.MonkeyPatch) -> Failures:
    calls: Failures = []
    monkeypatch.setattr(
        runtime, "capture_job_failure", lambda exc, **tags: calls.append((exc, tags))
    )
    return calls


@pytest.fixture
def start_worker(migrated_engine: Engine) -> Iterator[StartWorker]:
    started: list[tuple[Worker, threading.Thread]] = []

    def start(
        *runs: SourceRun, timeout: timedelta | None = None
    ) -> tuple[Worker, threading.Thread]:
        settings = Settings(worker=WorkerSettings.model_validate(FAST))
        worker = Worker(settings, migrated_engine, JobRegistry())
        for run in runs:
            worker.schedule(
                job_name(run.name),
                run,
                interval=TICK,
                timeout=timeout,
                source=run.name,
                on_failure=run.on_failure,
            )
        thread = threading.Thread(
            target=worker.run, kwargs={"install_signal_handlers": False}, daemon=True
        )
        thread.start()
        started.append((worker, thread))
        return worker, thread

    yield start
    for worker, thread in started:
        worker.request_stop()
        thread.join(timeout=15)
        assert not thread.is_alive()


def context() -> JobContext:
    return JobContext(
        job_id="source:test",
        kind="source:test",
        payload={},
        attempt=1,
        deadline=T0 + timedelta(hours=1),
    )


# --- schedule_sources --------------------------------------------------------------------


class StubWorker:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.rescheduled: list[tuple[str, timedelta, timedelta]] = []

    def schedule(self, name: str, handler: Handler, **options: object) -> None:
        self.jobs[name] = {"handler": handler, **options}

    def reschedule(
        self, name: str, *, interval: timedelta, jitter: timedelta = timedelta(0)
    ) -> None:
        self.rescheduled.append((name, interval, jitter))


def test_schedule_sources_registers_one_job_per_runnable_source(
    session_factory: sessionmaker[Session], store: SqlSourceStore, ingestor: ListingIngestor
) -> None:
    registry = SourceRegistry(browser_check=lambda _settings: "playwright is not installed")
    for adapter in (Good, OffByDefault, PinnedOff, NeedsBrowser, Keyed):
        registry.register(adapter)
    settings = SourcesSettings(
        default_interval_minutes=60,
        jitter_seconds=300,
        run_timeout_seconds=900,
        adapters={
            "good": AdapterOverrides(
                interval_minutes=15, jitter_seconds=30, run_timeout_seconds=120
            ),
            "pinned_off": AdapterOverrides(enabled=False),
        },
    )
    worker = StubWorker()

    scheduled = schedule_sources(worker, registry, settings, session_factory, ingestor)

    # Toggled off by default, or missing its token, but not pinned: still scheduled, so
    # the dashboard can turn it on or add the token without a restart. Pinned off, or
    # unable to run until the image or the environment changes: not scheduled.
    assert scheduled == ["good", "keyed", "off_by_default"]
    assert sorted(worker.jobs) == [
        "source:good",
        "source:keyed",
        "source:off_by_default",
        REFRESH_JOB,
    ]
    refresh = worker.jobs[REFRESH_JOB]
    assert isinstance(refresh["handler"], RefreshSchedules)
    assert (refresh["interval"], refresh["timeout"]) == (
        SCHEDULE_REFRESH_INTERVAL,
        timedelta(minutes=1),
    )
    good = worker.jobs["source:good"]
    assert (good["interval"], good["jitter"], good["timeout"], good["source"]) == (
        timedelta(minutes=15),
        timedelta(seconds=30),
        timedelta(seconds=120),
        "good",
    )
    assert good["on_failure"] == good["handler"].on_failure
    other = worker.jobs["source:off_by_default"]
    assert (other["interval"], other["jitter"], other["timeout"]) == (
        timedelta(minutes=60),
        timedelta(seconds=300),
        timedelta(seconds=900),
    )
    assert [s.name for s in store.all()] == [
        "good",
        "keyed",
        "needs_browser",
        "off_by_default",
        "pinned_off",
    ]
    browser = store.get("needs_browser")
    assert browser is not None
    assert browser.disabled_reason == "needs a browser: playwright is not installed"
    # Only the browser reason is stored: the web works the others out itself.
    keyed = store.get("keyed")
    assert keyed is not None
    assert keyed.disabled_reason is None


def test_schedule_sources_rejects_unknown_adapters(
    session_factory: sessionmaker[Session], ingestor: ListingIngestor
) -> None:
    registry = SourceRegistry()
    registry.register(Good)
    settings = SourcesSettings(adapters={"typo": AdapterOverrides(enabled=True)})

    with pytest.raises(ValueError, match="unknown adapter"):
        schedule_sources(StubWorker(), registry, settings, session_factory, ingestor)


# --- One run -----------------------------------------------------------------------------


@pytest.mark.usefixtures("criteria")
def test_success_records_listings_found_and_resets_failures(
    make_run: Callable[..., SourceRun], store: SqlSourceStore
) -> None:
    run = make_run(Good)
    run.on_failure(RuntimeError("earlier outage"))
    run.on_failure(RuntimeError("earlier outage"))

    run(context())

    state = store.get("good")
    assert state is not None
    assert (state.consecutive_failures, state.last_error) == (0, None)
    assert (state.listings_found, state.last_run_at, state.last_success_at) == (2, T0, T0)


def apify_budget(used_usd: float) -> tuple[ApifyBudget, InMemorySettingsStore]:
    """A budget whose usage reading (fresh at T0) is ``used_usd`` of a $5 plan."""
    usage = ApifyUsage(
        used_usd=used_usd,
        plan_limit_usd=5.0,
        cycle_end=datetime(2026, 11, 6, 23, 59, 59, tzinfo=UTC),
        fetched_at=T0,
    )
    store = InMemorySettingsStore({USAGE_KEY: usage.to_json()})
    return ApifyBudget(SourcesSettings(), store, now=lambda: T0), store


BILLED_SETTINGS = SourcesSettings(
    adapters={"billed": AdapterOverrides(api_key=SecretStr("apify-token"))}
)


@pytest.mark.usefixtures("criteria")
def test_an_apify_source_over_budget_is_skipped_without_a_failure(
    store: SqlSourceStore, ingestor: ListingIngestor, session_factory: sessionmaker[Session]
) -> None:
    budget, _ = apify_budget(used_usd=4.9)
    store.ensure(["billed", "good"])
    billed = SourceRun(
        Billed, settings=BILLED_SETTINGS, store=store, ingestor=ingestor, budget=budget
    )
    good = SourceRun(
        Good, settings=SourcesSettings(), store=store, ingestor=ingestor, budget=budget
    )

    billed(context())
    good(context())

    assert (Billed.searches, Good.searches) == (0, 1)
    state = store.get("billed")
    assert state is not None
    assert (state.last_run_at, state.consecutive_failures, state.last_error) == (None, 0, None)
    assert list(SqlListingStore(session_factory).runs("billed", limit=5)) == []


@pytest.mark.usefixtures("criteria")
def test_an_apify_source_under_budget_runs_and_reserves_its_cost(
    store: SqlSourceStore, ingestor: ListingIngestor
) -> None:
    budget, settings_store = apify_budget(used_usd=1.0)
    store.ensure(["billed"])
    run = SourceRun(Billed, settings=BILLED_SETTINGS, store=store, ingestor=ingestor, budget=budget)

    run(context())

    assert Billed.searches == 1
    usage = ApifyUsage.from_json(settings_store.get(USAGE_KEY))
    assert usage is not None
    # 25 results at $0.01 plus the $0.05 start.
    assert usage.used_usd == pytest.approx(1.30)


def test_failure_counts_up_and_keeps_the_last_error(
    make_run: Callable[..., SourceRun], store: SqlSourceStore
) -> None:
    run = make_run(Broken)

    run.on_failure(RuntimeError("first"))
    run.on_failure(JobTimeoutError("scheduled job 'source:broken' timed out after 900s"))

    state = store.get("broken")
    assert state is not None
    assert state.consecutive_failures == 2
    assert state.last_error == "JobTimeoutError: scheduled job 'source:broken' timed out after 900s"
    assert (state.last_run_at, state.last_success_at) == (T0, None)


@pytest.mark.usefixtures("criteria")
def test_errors_propagate_to_the_worker(make_run: Callable[..., SourceRun]) -> None:
    with pytest.raises(RuntimeError, match="captcha"):
        make_run(Broken)(context())


@pytest.mark.usefixtures("criteria")
def test_dashboard_toggle_is_read_on_every_run(
    make_run: Callable[..., SourceRun],
    store: SqlSourceStore,
    session_factory: sessionmaker[Session],
) -> None:
    run = make_run(Good)
    store.set_enabled("good", False)

    run(context())

    assert Good.searches == 0
    assert SqlListingStore(session_factory).runs("good") == []

    store.set_enabled("good", True)
    run(context())

    assert Good.searches == 1


@pytest.mark.usefixtures("criteria")
def test_an_off_by_default_source_runs_once_toggled_on(
    make_run: Callable[..., SourceRun], store: SqlSourceStore
) -> None:
    run = make_run(OffByDefault)
    run(context())
    assert OffByDefault.searches == 0

    store.set_enabled("off_by_default", True)
    run(context())

    assert OffByDefault.searches == 1


def test_no_criteria_leaves_the_counters_alone(
    make_run: Callable[..., SourceRun], store: SqlSourceStore
) -> None:
    make_run(Good)(context())

    assert store.get("good") == SourceState("good", None, None, None, 0, None, 0)


# --- In the worker -----------------------------------------------------------------------


@pytest.mark.usefixtures("criteria")
def test_a_failing_source_does_not_stop_the_others(
    start_worker: StartWorker,
    make_run: Callable[..., SourceRun],
    store: SqlSourceStore,
    failures: Failures,
) -> None:
    _, thread = start_worker(make_run(Broken), make_run(Good))

    def counts() -> tuple[int, int]:
        broken, good = store.get("broken"), store.get("good")
        assert broken is not None
        assert good is not None
        return broken.consecutive_failures, good.listings_found

    wait_for(lambda: counts()[0] >= 3 and Good.searches >= 3)
    assert thread.is_alive()
    broken = store.get("broken")
    good = store.get("good")
    assert broken is not None
    assert good is not None
    assert broken.last_error == "RuntimeError: blocked by captcha"
    assert broken.last_success_at is None
    assert (good.consecutive_failures, good.listings_found, good.last_success_at) == (0, 2, T0)
    exc, tags = failures[0]
    assert isinstance(exc, RuntimeError)
    assert tags == {"job_id": "source:broken", "job_type": "scheduled", "source": "broken"}


@pytest.mark.usefixtures("criteria")
def test_runs_of_one_source_never_overlap_across_workers(
    start_worker: StartWorker, make_run: Callable[..., SourceRun]
) -> None:
    start_worker(make_run(Slow))
    start_worker(make_run(Slow))

    wait_for(lambda: Slow.searches >= 4)

    assert Slow.peak == 1


@pytest.mark.usefixtures("criteria")
def test_a_source_leased_elsewhere_is_skipped(
    start_worker: StartWorker,
    make_run: Callable[..., SourceRun],
    session_factory: sessionmaker[Session],
) -> None:
    locks.acquire(session_factory, "scheduled:source:good", "someone-else", timedelta(minutes=5))

    start_worker(make_run(Good))
    time.sleep(0.5)

    assert Good.searches == 0


@pytest.mark.usefixtures("criteria")
def test_a_timed_out_run_is_recorded_as_failed_and_stores_nothing_late(
    start_worker: StartWorker,
    make_run: Callable[..., SourceRun],
    store: SqlSourceStore,
    session_factory: sessionmaker[Session],
    failures: Failures,
) -> None:
    worker, thread = start_worker(make_run(Stuck), timeout=timedelta(milliseconds=200))

    def state() -> SourceState:
        current = store.get("stuck")
        assert current is not None
        return current

    # Recorded while the adapter is still stuck in search.
    wait_for(lambda: state().consecutive_failures == 1)
    assert isinstance(failures[0][0], JobTimeoutError)
    assert failures[0][1]["source"] == "stuck"
    assert thread.is_alive()

    # Stop scheduling, then let the stuck search finish: its results are thrown away
    # rather than stored late, and no success is recorded over the timeout.
    worker.request_stop()
    thread.join(timeout=15)
    listings = SqlListingStore(session_factory)
    Stuck.release.set()
    wait_for(lambda: listings.runs("stuck")[0].status is SourceRunStatus.FAILED)
    run = listings.runs("stuck")[0]
    assert run.error is not None
    assert run.error.startswith("RunCancelledError:")
    assert listings.find("stuck", "1") is None
    assert (state().consecutive_failures, state().last_success_at) == (1, None)


# --- Run now (the run_source queue job) -----------------------------------------------


@pytest.fixture
def run_now(
    session_factory: sessionmaker[Session], store: SqlSourceStore, ingestor: ListingIngestor
) -> RunSourceNow:
    registry = SourceRegistry()
    for adapter in (Good, Broken, OffByDefault):
        registry.register(adapter)
    store.ensure(registry.names())
    return RunSourceNow(
        registry,
        settings=SourcesSettings(),
        session_factory=session_factory,
        ingestor=ingestor,
        clock=lambda: T0,
    )


def run_now_context(source: str, job_id: int = 7) -> JobContext:
    return JobContext(
        job_id=job_id,
        kind=RUN_SOURCE,
        payload={"source": source},
        attempt=1,
        deadline=T0 + timedelta(hours=1),
    )


def lease_holder(session_factory: sessionmaker[Session], source: str) -> str | None:
    with session_scope(session_factory) as session:
        lock = session.get(JobLock, f"scheduled:{job_name(source)}")
        return None if lock is None else lock.owner


@pytest.mark.usefixtures("criteria")
def test_run_now_runs_the_source_and_records_success(
    run_now: RunSourceNow, store: SqlSourceStore, session_factory: sessionmaker[Session]
) -> None:
    run_now(run_now_context("good"))

    assert Good.searches == 1
    state = store.get("good")
    assert state is not None
    assert (state.listings_found, state.last_success_at) == (2, T0)
    assert lease_holder(session_factory, "good") is None


@pytest.mark.usefixtures("criteria")
def test_run_now_records_a_failure_and_reraises(
    run_now: RunSourceNow, store: SqlSourceStore, session_factory: sessionmaker[Session]
) -> None:
    with pytest.raises(RuntimeError, match="captcha"):
        run_now(run_now_context("broken"))

    state = store.get("broken")
    assert state is not None
    assert state.consecutive_failures == 1
    assert state.last_error == "RuntimeError: blocked by captcha"
    assert lease_holder(session_factory, "broken") is None


@pytest.mark.usefixtures("criteria")
def test_run_now_skips_a_source_that_is_already_running(
    run_now: RunSourceNow, store: SqlSourceStore, session_factory: sessionmaker[Session]
) -> None:
    locks.acquire(session_factory, "scheduled:source:good", "worker-1", timedelta(hours=2))

    run_now(run_now_context("good"))

    assert Good.searches == 0
    assert lease_holder(session_factory, "good") == "worker-1"


@pytest.mark.usefixtures("criteria")
def test_run_now_skips_a_disabled_source(run_now: RunSourceNow) -> None:
    run_now(run_now_context("off_by_default"))

    assert OffByDefault.searches == 0


def test_run_now_fails_for_an_unknown_source(run_now: RunSourceNow) -> None:
    with pytest.raises(LookupError, match="nope"):
        run_now(run_now_context("nope"))


def test_register_run_now_uses_the_longest_source_timeout(
    session_factory: sessionmaker[Session], ingestor: ListingIngestor
) -> None:
    registry = SourceRegistry()
    for adapter in (Good, Broken):
        registry.register(adapter)
    settings = SourcesSettings(
        run_timeout_seconds=600, adapters={"broken": AdapterOverrides(run_timeout_seconds=1200)}
    )
    jobs = JobRegistry()

    register_run_now(jobs, registry, settings, session_factory, ingestor)

    registration = jobs.get(RUN_SOURCE)
    assert registration is not None
    assert isinstance(registration.handler, RunSourceNow)
    assert registration.timeout == timedelta(seconds=1200)


@pytest.mark.usefixtures("criteria")
def test_a_queued_run_now_job_runs_in_the_worker(
    migrated_engine: Engine,
    session_factory: sessionmaker[Session],
    store: SqlSourceStore,
    ingestor: ListingIngestor,
) -> None:
    registry = SourceRegistry()
    registry.register(Good)
    store.ensure(["good"])
    jobs = JobRegistry()
    register_run_now(jobs, registry, SourcesSettings(), session_factory, ingestor)
    with session_scope(session_factory) as session:
        job_id = queue.enqueue(session, RUN_SOURCE, {"source": "good"}, max_attempts=1).id
    worker = Worker(Settings(worker=WorkerSettings.model_validate(FAST)), migrated_engine, jobs)
    thread = threading.Thread(
        target=worker.run, kwargs={"install_signal_handlers": False}, daemon=True
    )
    thread.start()
    try:

        def succeeded() -> bool:
            with session_scope(session_factory) as session:
                job = session.get(Job, job_id)
                return job is not None and job.status is JobStatus.SUCCEEDED

        wait_for(succeeded)
    finally:
        worker.request_stop()
        thread.join(timeout=15)

    assert Good.searches == 1
    state = store.get("good")
    assert state is not None
    assert state.listings_found == 2


# --- dashboard settings and credentials ----------------------------------------


@pytest.mark.usefixtures("criteria")
def test_a_source_runs_once_its_credential_is_stored(
    make_run: Callable[..., SourceRun], store: SqlSourceStore
) -> None:
    run = make_run(Keyed)

    run(context())
    assert Keyed.tokens == []

    store.set_credential("keyed_provider", "token-from-dashboard")
    run(context())

    assert Keyed.tokens == ["token-from-dashboard"]
    state = store.get("keyed")
    assert state is not None
    assert state.listings_found == 1


@pytest.mark.usefixtures("criteria")
def test_stored_settings_reach_the_adapter(
    make_run: Callable[..., SourceRun], store: SqlSourceStore
) -> None:
    run = make_run(Good)
    store.set_overrides(
        "good", SourceOverrides(search_locations=("Springfield, IL",), max_results=3)
    )

    assert run.resolve().search_locations == ("Springfield, IL",)
    assert run.resolve().max_results == 3


def test_refresh_reschedules_a_source_whose_interval_changed(
    session_factory: sessionmaker[Session], store: SqlSourceStore, ingestor: ListingIngestor
) -> None:
    registry = SourceRegistry()
    registry.register(Good)
    registry.register(OffByDefault)
    settings = SourcesSettings(
        default_interval_minutes=60,
        jitter_seconds=30,
        adapters={"off_by_default": AdapterOverrides(interval_minutes=10)},
    )
    worker = StubWorker()
    schedule_sources(worker, registry, settings, session_factory, ingestor)
    refresh = worker.jobs[REFRESH_JOB]["handler"]
    assert isinstance(refresh, RefreshSchedules)
    assert refresh.intervals == {"good": 60, "off_by_default": 10}

    refresh(context())
    assert worker.rescheduled == []

    store.set_overrides("good", SourceOverrides(interval_minutes=15))
    # Pinned in SOURCES_ADAPTERS, so the stored value changes nothing.
    store.set_overrides("off_by_default", SourceOverrides(interval_minutes=99))
    refresh(context())

    assert worker.rescheduled == [(job_name("good"), timedelta(minutes=15), timedelta(seconds=30))]
    assert refresh.intervals == {"good": 15, "off_by_default": 10}

    refresh(context())
    assert len(worker.rescheduled) == 1

    store.set_overrides("good", None)
    refresh(context())
    assert worker.rescheduled[-1] == (
        job_name("good"),
        timedelta(minutes=60),
        timedelta(seconds=30),
    )
