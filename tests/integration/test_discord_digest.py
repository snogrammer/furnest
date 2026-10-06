"""The daily digest: scheduled at the configured local time, queued once per day,
and posted with counts by status and the day's best new suggestions."""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx2
import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import AppSettings, DiscordSettings, Settings
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import Job, JobStatus, Property
from furnest.db.property_store import SqlPropertyStore
from furnest.db.session import session_scope
from furnest.domain import Money, NormalizedListing, PropertyStatus
from furnest.domain.price import Price
from furnest.notify.digest import (
    DIGEST_JOB,
    DIGEST_SCHEDULE,
    DigestPost,
    QueueDigest,
    digest_message,
    schedule_digest,
)
from furnest.notify.discord import DiscordClient, DiscordError
from furnest.worker.execution import Handler, JobContext, default_registry

pytestmark = pytest.mark.integration

S = PropertyStatus
NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
DAY = date(2026, 10, 6)
CHANNEL = "1200000000000000000"
SETTINGS = Settings(discord=DiscordSettings(channel_id=CHANNEL, digest_top=2))
BASE_URL = str(SETTINGS.app.base_url)

Seed = Callable[..., int]


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlPropertyStore:
    return SqlPropertyStore(session_factory)


@pytest.fixture
def seed(session_factory: sessionmaker[Session], store: SqlPropertyStore) -> Seed:
    """Add a listing and its property, then set the property's fields directly."""
    listings = SqlListingStore(session_factory)
    counter = iter(range(1, 1000))

    def add(
        title: str = "Furnished place",
        *,
        status: PropertyStatus = S.SUGGESTED,
        score: int | None = None,
        price: int | None = None,
        changed: datetime = NOW - timedelta(hours=1),
        snoozed_until: datetime | None = None,
    ) -> int:
        source_id = f"l{next(counter)}"
        listing = NormalizedListing.model_validate(
            {
                "source": "test",
                "source_id": source_id,
                "url": f"https://example.com/{source_id}",
                "title": title,
                "price": None if price is None else Price(amount=Money.from_major(price)),
            }
        )
        (listing_id,) = listings.upsert_batch("test", [listing], seen_at=changed).new_ids
        property_id = store.ensure_for_listing(listing_id, at=changed).id
        with session_scope(session_factory) as session:
            session.execute(
                update(Property)
                .where(Property.id == property_id)
                .values(
                    status=status,
                    score=score,
                    status_changed_at=changed,
                    snoozed_until=snoozed_until,
                )
            )
        return property_id

    return add


def digest_jobs(session_factory: sessionmaker[Session]) -> list[dict[str, Any]]:
    with session_scope(session_factory) as session:
        jobs = session.scalars(select(Job).where(Job.kind == DIGEST_JOB).order_by(Job.id))
        return [job.payload for job in jobs]


def ctx(payload: dict[str, Any]) -> JobContext:
    return JobContext(job_id=1, kind=DIGEST_JOB, payload=payload, attempt=1, deadline=NOW)


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


def created() -> httpx2.Response:
    return httpx2.Response(200, json={"id": "1300000000000000000", "channel_id": CHANNEL})


# --- changed_since -----------------------------------------------------------------------


def test_search_changed_since_filters_on_the_status_change(
    store: SqlPropertyStore, seed: Seed
) -> None:
    recent = seed(changed=NOW - timedelta(hours=2))
    seed(changed=NOW - timedelta(days=2))

    page = store.search(now=NOW, limit=10, changed_since=NOW - timedelta(days=1))

    assert [row.property.id for row in page.rows] == [recent]
    assert page.total == 1


# --- Scheduling --------------------------------------------------------------------------


class StubWorker:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}

    def schedule_daily(self, name: str, handler: Handler, **options: object) -> None:
        self.jobs[name] = {"handler": handler, **options}


def test_no_digest_time_schedules_nothing(session_factory: sessionmaker[Session]) -> None:
    worker = StubWorker()
    assert schedule_digest(worker, session_factory, Settings()) is False
    assert worker.jobs == {}


def test_digest_time_schedules_a_daily_job_in_the_app_timezone(
    session_factory: sessionmaker[Session],
) -> None:
    worker = StubWorker()
    settings = Settings(
        app=AppSettings(timezone="America/Chicago"),
        discord=DiscordSettings(digest_time=time(8, 30)),
    )

    assert schedule_digest(worker, session_factory, settings) is True

    job = worker.jobs[DIGEST_SCHEDULE]
    assert job["at"] == time(8, 30)
    assert job["tz"] == ZoneInfo("America/Chicago")
    assert isinstance(job["handler"], QueueDigest)


def test_queue_digest_queues_the_local_date_once(session_factory: sessionmaker[Session]) -> None:
    # 03:00 UTC on the 7th is still the 6th in Chicago.
    queue_digest = QueueDigest(
        session_factory,
        tz=ZoneInfo("America/Chicago"),
        clock=lambda: datetime(2026, 10, 7, 3, 0, tzinfo=UTC),
    )

    queue_digest(ctx({}))
    queue_digest(ctx({}))

    assert digest_jobs(session_factory) == [{"date": "2026-10-06"}]


def test_queue_digest_skips_a_day_already_posted(session_factory: sessionmaker[Session]) -> None:
    queue_digest = QueueDigest(session_factory, tz=UTC, clock=lambda: NOW)
    queue_digest(ctx({}))
    with session_scope(session_factory) as session:
        session.execute(update(Job).values(status=JobStatus.SUCCEEDED))

    queue_digest(ctx({}))
    QueueDigest(session_factory, tz=UTC, clock=lambda: NOW + timedelta(days=1))(ctx({}))

    assert digest_jobs(session_factory) == [{"date": "2026-10-06"}, {"date": "2026-10-07"}]


# --- Message -----------------------------------------------------------------------------


def test_digest_message_counts_and_top_suggestions(store: SqlPropertyStore, seed: Seed) -> None:
    best = seed("Sunny [loft]", score=91, price=2400)
    second = seed("Quiet flat", score=80)
    seed("Okay room", score=50)
    page = store.search(now=NOW, limit=2, statuses=[S.SUGGESTED])

    payload = digest_message(
        DAY,
        {S.SUGGESTED: 3, S.DENIED: 2, S.APPROVED: 1},
        page.rows,
        new_total=page.total,
        base_url="https://furnest.example.com/",
    )

    (embed,) = payload["embeds"]
    assert embed["title"] == "Daily digest · 2026-10-06"
    assert embed["url"] == "https://furnest.example.com/"
    assert embed["footer"] == {"text": "furnest · 6 properties"}
    assert embed["fields"] == [
        {"name": "Suggested", "value": "3", "inline": True},
        {"name": "Approved", "value": "1", "inline": True},
        {"name": "Denied", "value": "2", "inline": True},
        {
            "name": "New suggestions (3)",
            "value": (
                f"• [Sunny \\[loft\\]](https://furnest.example.com/properties/{best})"
                " · 91/100 · $2,400.00/mo\n"
                f"• [Quiet flat](https://furnest.example.com/properties/{second}) · 80/100\n"
                "…and 1 more"
            ),
            "inline": False,
        },
    ]
    assert payload["allowed_mentions"] == {"parse": []}


def test_digest_message_without_new_suggestions() -> None:
    payload = digest_message(DAY, {}, [], new_total=0, base_url=BASE_URL)
    (embed,) = payload["embeds"]
    assert embed["fields"] == [
        {
            "name": "New suggestions",
            "value": "No new suggestions in the last day.",
            "inline": False,
        }
    ]
    assert embed["footer"] == {"text": "furnest · 0 properties"}


# --- DigestPost --------------------------------------------------------------------------


def poster(
    session_factory: sessionmaker[Session], factory: Callable[[], DiscordClient | None]
) -> DigestPost:
    return DigestPost(session_factory, settings=SETTINGS, client_factory=factory, clock=lambda: NOW)


def test_posts_the_digest(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, seed: Seed
) -> None:
    top = seed("Top", score=95)
    runner_up = seed("Runner up", score=90)
    seed("Third", score=10)
    seed("Yesterday's", score=99, changed=NOW - timedelta(days=2))
    seed("Approved", status=S.APPROVED, score=99)
    seed("Snoozed", score=99, snoozed_until=NOW + timedelta(days=1))
    requests, factory = fake_discord(created())

    poster(session_factory, factory)(ctx({"date": "2026-10-06"}))

    (request,) = requests
    assert request.url.path == f"/api/v10/channels/{CHANNEL}/messages"
    body = json.loads(request.content)
    (embed,) = body["embeds"]
    assert embed["title"] == "Daily digest · 2026-10-06"
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    # Counts include snoozed and older properties.
    assert fields["Suggested"] == "5"
    assert fields["Approved"] == "1"
    # New: suggested in the last day and not snoozed, best first, top 2 of 3.
    new = fields["New suggestions (3)"].splitlines()
    assert [line.split("](")[0] for line in new[:2]] == ["• [Top", "• [Runner up"]
    assert f"/properties/{top})" in new[0]
    assert f"/properties/{runner_up})" in new[1]
    assert new[2] == "…and 1 more"


def test_skips_when_discord_is_not_configured(session_factory: sessionmaker[Session]) -> None:
    poster(session_factory, lambda: None)(ctx({"date": "2026-10-06"}))


def test_discord_errors_raise_so_the_queue_retries(
    session_factory: sessionmaker[Session],
) -> None:
    _, factory = fake_discord(httpx2.Response(500, json={"message": "oops"}))
    with pytest.raises(DiscordError):
        poster(session_factory, factory)(ctx({"date": "2026-10-06"}))


def test_digest_post_is_registered(session_factory: sessionmaker[Session]) -> None:
    registration = default_registry(session_factory, settings=SETTINGS).get(DIGEST_JOB)
    assert registration is not None
    assert isinstance(registration.handler, DigestPost)
