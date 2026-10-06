"""Source failure alerts: queued once when a source reaches the failure threshold,
once more when it recovers, and posted to Discord by the worker."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import DiscordSettings, Settings, SourcesSettings
from furnest.db.criteria_store import CriteriaStore
from furnest.db.models import Job
from furnest.db.session import session_scope
from furnest.db.source_store import SOURCE_ALERT_JOB, SourceNotFoundError, SqlSourceStore
from furnest.domain import Criteria, NormalizedListing
from furnest.matching.amenities import KeywordAmenityExtractor
from furnest.notify.alerts import (
    FAILING_COLOR,
    MAX_ERROR_CHARS,
    RECOVERED_COLOR,
    SourceAlertPost,
    source_alert_message,
)
from furnest.notify.discord import DiscordClient, DiscordError
from furnest.pipeline.ingest import ListingIngestor
from furnest.pipeline.schedule import RunSourceNow, SourceRun
from furnest.sources import SourceAdapter
from furnest.sources.registry import SourceRegistry
from furnest.worker.execution import JobContext, JobTimeoutError, default_registry

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
CHANNEL = "1200000000000000000"
SETTINGS = Settings(discord=DiscordSettings(channel_id=CHANNEL))
BASE_URL = str(SETTINGS.app.base_url)


class Flaky(SourceAdapter):
    """Fails while ``failing`` is set, otherwise returns one listing."""

    name: ClassVar[str] = "flaky"
    display_name: ClassVar[str] = "Flaky Rentals"
    default_enabled: ClassVar[bool] = True
    failing: ClassVar[bool] = True

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        if type(self).failing:
            raise RuntimeError("blocked by captcha")
        return [
            NormalizedListing(
                source=self.name,
                source_id="1",
                url="https://example.com/flaky/1",  # type: ignore[arg-type]
                title="Furnished place",
            )
        ]


@pytest.fixture(autouse=True)
def reset_adapter() -> None:
    Flaky.failing = True


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlSourceStore:
    store = SqlSourceStore(session_factory)
    store.ensure(["flaky"])
    return store


@pytest.fixture
def ingestor(session_factory: sessionmaker[Session]) -> ListingIngestor:
    CriteriaStore(session_factory).save(Criteria(furnished_required=False), created_by="cli")
    return ListingIngestor(
        session_factory, extractor=KeywordAmenityExtractor(), settings=SourcesSettings()
    )


def alerts(session_factory: sessionmaker[Session]) -> list[dict[str, Any]]:
    with session_scope(session_factory) as session:
        jobs = session.scalars(select(Job).where(Job.kind == SOURCE_ALERT_JOB).order_by(Job.id))
        return [job.payload for job in jobs]


def fail(store: SqlSourceStore, times: int, *, alert_after: int = 3) -> None:
    for attempt in range(times):
        store.record_failure(
            "flaky", error=f"RuntimeError: try {attempt + 1}", at=T0, alert_after=alert_after
        )


def succeed(store: SqlSourceStore, *, alert_after: int = 3) -> None:
    store.record_success("flaky", listings_found=1, at=T0, alert_after=alert_after)


# --- Threshold ---------------------------------------------------------------------------


def test_no_alert_below_the_threshold(
    session_factory: sessionmaker[Session], store: SqlSourceStore
) -> None:
    fail(store, 2)
    assert alerts(session_factory) == []


def test_alert_is_queued_on_the_nth_failure_only(
    session_factory: sessionmaker[Session], store: SqlSourceStore
) -> None:
    fail(store, 3)
    assert alerts(session_factory) == [
        {"source": "flaky", "event": "failing", "failures": 3, "error": "RuntimeError: try 3"}
    ]

    fail(store, 5)

    assert len(alerts(session_factory)) == 1
    state = store.get("flaky")
    assert state is not None
    assert state.consecutive_failures == 8


def test_recovery_is_queued_after_an_alerted_outage(
    session_factory: sessionmaker[Session], store: SqlSourceStore
) -> None:
    fail(store, 4)
    succeed(store)
    succeed(store)

    assert [(a["event"], a["failures"]) for a in alerts(session_factory)] == [
        ("failing", 3),
        ("recovered", 4),
    ]


def test_no_recovery_without_an_outage(
    session_factory: sessionmaker[Session], store: SqlSourceStore
) -> None:
    fail(store, 2)
    succeed(store)
    assert alerts(session_factory) == []


def test_a_new_outage_alerts_again(
    session_factory: sessionmaker[Session], store: SqlSourceStore
) -> None:
    fail(store, 3)
    succeed(store)
    fail(store, 3)
    assert [a["event"] for a in alerts(session_factory)] == ["failing", "recovered", "failing"]


def test_threshold_zero_turns_alerts_off(
    session_factory: sessionmaker[Session], store: SqlSourceStore
) -> None:
    fail(store, 5, alert_after=0)
    succeed(store, alert_after=0)
    assert alerts(session_factory) == []


def test_threshold_of_one_alerts_on_the_first_failure(
    session_factory: sessionmaker[Session], store: SqlSourceStore
) -> None:
    fail(store, 1, alert_after=1)
    succeed(store, alert_after=1)
    assert [(a["event"], a["failures"]) for a in alerts(session_factory)] == [
        ("failing", 1),
        ("recovered", 1),
    ]


def test_unknown_source_still_raises(store: SqlSourceStore) -> None:
    with pytest.raises(SourceNotFoundError):
        store.record_failure("nope", error="x", at=T0, alert_after=1)
    with pytest.raises(SourceNotFoundError):
        store.record_success("nope", listings_found=0, at=T0, alert_after=1)


# --- Through the scheduler ---------------------------------------------------------------


def context() -> JobContext:
    return JobContext(
        job_id="source:flaky", kind="source:flaky", payload={}, attempt=1, deadline=T0
    )


def test_source_run_passes_the_threshold_on_failure_and_success(
    session_factory: sessionmaker[Session], store: SqlSourceStore, ingestor: ListingIngestor
) -> None:
    run = SourceRun(
        Flaky,
        settings=SourcesSettings(),
        store=store,
        ingestor=ingestor,
        alert_after=2,
        clock=lambda: T0,
    )
    for _ in range(2):
        with pytest.raises(RuntimeError) as caught:
            run(context())
        run.on_failure(caught.value)
    Flaky.failing = False
    run(context())

    assert alerts(session_factory) == [
        {
            "source": "flaky",
            "event": "failing",
            "failures": 2,
            "error": "RuntimeError: blocked by captcha",
        },
        {"source": "flaky", "event": "recovered", "failures": 2},
    ]


def test_a_timeout_counts_towards_the_threshold(
    session_factory: sessionmaker[Session], store: SqlSourceStore, ingestor: ListingIngestor
) -> None:
    run = SourceRun(
        Flaky, settings=SourcesSettings(), store=store, ingestor=ingestor, alert_after=1
    )
    run.on_failure(JobTimeoutError("scheduled job 'source:flaky' timed out"))
    (alert,) = alerts(session_factory)
    assert alert["error"].startswith("JobTimeoutError")


def test_run_now_passes_the_threshold(
    session_factory: sessionmaker[Session], store: SqlSourceStore, ingestor: ListingIngestor
) -> None:
    registry = SourceRegistry()
    registry.register(Flaky)
    run_now = RunSourceNow(
        registry,
        settings=SourcesSettings(),
        session_factory=session_factory,
        ingestor=ingestor,
        alert_after=1,
    )
    payload = {"source": "flaky"}
    ctx = JobContext(
        job_id=7, kind="run_source", payload=payload, attempt=1, deadline=T0 + timedelta(hours=1)
    )
    with pytest.raises(RuntimeError):
        run_now(ctx)
    assert [a["event"] for a in alerts(session_factory)] == ["failing"]


# --- Message -----------------------------------------------------------------------------


def test_failing_message() -> None:
    payload = source_alert_message(
        display_name="Flaky_Rentals",
        event="failing",
        failures=3,
        error="RuntimeError: `bad` thing",
        base_url="https://furnest.example.com/",
    )
    (embed,) = payload["embeds"]
    assert embed["title"] == "Source failing: Flaky_Rentals"
    assert embed["color"] == FAILING_COLOR
    assert embed["description"] == (
        "Flaky\\_Rentals has failed 3 runs in a row.\n"
        "```\nRuntimeError: 'bad' thing\n```\n"
        "[Source health](https://furnest.example.com/sources)"
    )
    assert payload["allowed_mentions"] == {"parse": []}


def test_recovered_message() -> None:
    payload = source_alert_message(
        display_name="Flaky", event="recovered", failures=1, error=None, base_url=BASE_URL
    )
    (embed,) = payload["embeds"]
    assert embed["title"] == "Source recovered: Flaky"
    assert embed["color"] == RECOVERED_COLOR
    assert embed["description"].startswith("Flaky ran successfully again after 1 run failing.")
    assert "```" not in embed["description"]


def test_long_errors_are_truncated() -> None:
    payload = source_alert_message(
        display_name="Flaky", event="failing", failures=3, error="x" * 5000, base_url=BASE_URL
    )
    (embed,) = payload["embeds"]
    assert f"{'x' * (MAX_ERROR_CHARS - 1)}…\n```" in embed["description"]


def test_unknown_event_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown source alert event"):
        source_alert_message(
            display_name="Flaky", event="exploded", failures=1, error=None, base_url=BASE_URL
        )


# --- Handler -----------------------------------------------------------------------------


def fake_discord(
    *responses: httpx2.Response,
) -> tuple[list[httpx2.Request], Callable[[], DiscordClient]]:
    requests: list[httpx2.Request] = []
    replies = list(responses)

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return replies.pop(0)

    def factory() -> DiscordClient:
        return DiscordClient(
            "bot-token",
            user_agent="DiscordBot (https://example.com, 0)",
            max_retries=0,
            transport=httpx2.MockTransport(handle),
            sleep=lambda _s: None,
        )

    return requests, factory


def alert_ctx(**payload: Any) -> JobContext:  # noqa: ANN401
    return JobContext(job_id=1, kind=SOURCE_ALERT_JOB, payload=payload, attempt=1, deadline=T0)


def test_handler_posts_the_alert_with_the_display_name() -> None:
    requests, factory = fake_discord(
        httpx2.Response(200, json={"id": "1300000000000000000", "channel_id": CHANNEL})
    )
    handler = SourceAlertPost(
        settings=SETTINGS, source_name=lambda key: key.title(), client_factory=factory
    )

    handler(alert_ctx(source="flaky", event="failing", failures=3, error="RuntimeError: x"))

    (request,) = requests
    assert request.url.path == f"/api/v10/channels/{CHANNEL}/messages"
    assert json.loads(request.content) == source_alert_message(
        display_name="Flaky",
        event="failing",
        failures=3,
        error="RuntimeError: x",
        base_url=BASE_URL,
    )


def test_handler_skips_when_discord_is_not_configured() -> None:
    handler = SourceAlertPost(settings=SETTINGS, client_factory=lambda: None)
    handler(alert_ctx(source="flaky", event="recovered", failures=3))


def test_handler_raises_on_discord_errors_so_the_queue_retries() -> None:
    _, factory = fake_discord(httpx2.Response(403, json={"code": 50001, "message": "no"}))
    handler = SourceAlertPost(settings=SETTINGS, client_factory=factory)
    with pytest.raises(DiscordError):
        handler(alert_ctx(source="flaky", event="recovered", failures=3))


def test_handler_is_registered(session_factory: sessionmaker[Session]) -> None:
    registration = default_registry(session_factory, settings=SETTINGS).get(SOURCE_ALERT_JOB)
    assert registration is not None
    assert isinstance(registration.handler, SourceAlertPost)
