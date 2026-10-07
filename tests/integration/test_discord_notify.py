"""NotifyPost posts suggested properties to Discord, and every later change to a posted
property queues a sync of its message."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import DiscordSettings, Settings
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import Job, JobStatus, Listing, Property
from furnest.db.property_store import NOTIFY, PropertyNotFoundError, SqlPropertyStore
from furnest.db.session import session_scope
from furnest.domain import Actor, NormalizedListing, PropertyStatus
from furnest.notify.discord import DiscordClient, DiscordError
from furnest.notify.embeds import disabled_actions, match_message
from furnest.notify.post import NotifyPost
from furnest.notify.sync import MESSAGE_SYNC_JOB, MessageSync
from furnest.worker.execution import JobContext, default_registry

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
CHANNEL = "1200000000000000000"
MESSAGE = "1300000000000000000"
NEW_MESSAGE = "1300000000000000001"
SETTINGS = Settings(discord=DiscordSettings(channel_id=CHANNEL))


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlPropertyStore:
    return SqlPropertyStore(session_factory)


@pytest.fixture
def property_id(session_factory: sessionmaker[Session], store: SqlPropertyStore) -> int:
    """A suggested property with one listing and no message yet."""
    listing = NormalizedListing.model_validate(
        {"source": "test", "source_id": "l1", "url": "https://example.com/l1", "title": "Loft"}
    )
    (listing_id,) = (
        SqlListingStore(session_factory).upsert_batch("test", [listing], seen_at=T0).new_ids
    )
    pid = store.ensure_for_listing(listing_id, at=T0).id
    store.transition(pid, PropertyStatus.SUGGESTED, actor=Actor.SYSTEM, at=T0)
    return pid


def posted(session_factory: sessionmaker[Session], pid: int, message_id: str = MESSAGE) -> None:
    """Mark ``pid`` as already posted to Discord."""
    with session_scope(session_factory) as session:
        session.execute(
            update(Property)
            .where(Property.id == pid)
            .values(discord_channel_id=CHANNEL, discord_message_id=message_id)
        )


def sync_jobs(session_factory: sessionmaker[Session]) -> list[dict[str, Any]]:
    with session_scope(session_factory) as session:
        jobs = session.scalars(select(Job).where(Job.kind == MESSAGE_SYNC_JOB).order_by(Job.id))
        return [job.payload for job in jobs]


def ctx(kind: str, payload: dict[str, Any]) -> JobContext:
    return JobContext(job_id=1, kind=kind, payload=payload, attempt=1, deadline=T0)


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


def created(message_id: str = NEW_MESSAGE) -> httpx2.Response:
    return httpx2.Response(200, json={"id": message_id, "channel_id": CHANNEL})


def poster(
    session_factory: sessionmaker[Session],
    factory: Callable[[], DiscordClient | None],
    *,
    now: datetime = T0,
) -> NotifyPost:
    return NotifyPost(
        session_factory,
        settings=SETTINGS,
        source_name=lambda key: key.upper(),
        client_factory=factory,
        clock=lambda: now,
    )


def notify(pid: int) -> JobContext:
    return ctx(NOTIFY, {"property_id": pid})


# --- NotifyPost ---------------------------------------------------------------------


def test_posts_the_match_and_remembers_the_message(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, property_id: int
) -> None:
    requests, factory = fake_discord(created())

    poster(session_factory, factory)(notify(property_id))

    (request,) = requests
    assert request.method == "POST"
    assert request.url.path == f"/api/v10/channels/{CHANNEL}/messages"
    detail = store.detail(property_id)
    assert detail is not None
    expected = match_message(
        detail,
        base_url=str(SETTINGS.app.base_url),
        source_name=lambda key: key.upper(),
        disabled=disabled_actions(detail.property),
    )
    assert json.loads(request.content) == expected
    with session_scope(session_factory) as session:
        row = session.get(Property, property_id)
        assert row is not None
        assert (row.discord_channel_id, row.discord_message_id) == (CHANNEL, NEW_MESSAGE)
    assert sync_jobs(session_factory) == []  # nothing changed while posting


def test_does_nothing_when_discord_is_not_configured(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, property_id: int
) -> None:
    poster(session_factory, lambda: None)(notify(property_id))

    assert store.get(property_id).discord_message_id is None  # type: ignore[union-attr]


def test_does_nothing_without_a_channel(
    session_factory: sessionmaker[Session], property_id: int
) -> None:
    requests, factory = fake_discord()
    handler = NotifyPost(session_factory, settings=Settings(), client_factory=factory)

    handler(notify(property_id))

    assert requests == []


def test_a_property_decided_before_the_job_ran_is_not_posted(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, property_id: int
) -> None:
    store.deny(property_id, actor=Actor.DASHBOARD, at=T0)
    requests, factory = fake_discord()

    poster(session_factory, factory)(notify(property_id))

    assert requests == []


def test_a_snoozed_property_waits_for_its_snooze_to_end(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, property_id: int
) -> None:
    store.snooze(property_id, until=T0 + HOUR, actor=Actor.DASHBOARD, at=T0)
    requests, factory = fake_discord(created())

    poster(session_factory, factory, now=T0)(notify(property_id))
    assert requests == []

    poster(session_factory, factory, now=T0 + 2 * HOUR)(notify(property_id))
    assert len(requests) == 1


def test_a_missing_property_is_skipped(session_factory: sessionmaker[Session]) -> None:
    requests, factory = fake_discord()

    poster(session_factory, factory)(notify(999))

    assert requests == []


def test_discord_errors_raise_so_the_queue_retries(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, property_id: int
) -> None:
    _, factory = fake_discord(httpx2.Response(503, json={"message": "unavailable"}))

    with pytest.raises(DiscordError):
        poster(session_factory, factory)(notify(property_id))

    assert store.get(property_id).discord_message_id is None  # type: ignore[union-attr]


def test_renotifying_posts_again_and_retires_the_old_message(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, property_id: int
) -> None:
    posted(session_factory, property_id)
    requests, factory = fake_discord(created(NEW_MESSAGE))

    poster(session_factory, factory)(notify(property_id))

    assert requests[0].method == "POST"
    assert store.get(property_id).discord_message_id == NEW_MESSAGE  # type: ignore[union-attr]
    assert sync_jobs(session_factory) == [
        {"property_id": property_id, "channel_id": CHANNEL, "message_id": MESSAGE}
    ]


def test_a_change_made_while_posting_queues_a_sync(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, property_id: int
) -> None:
    rendered = store.get(property_id)
    assert rendered is not None
    store.transition(property_id, PropertyStatus.APPROVED, actor=Actor.DASHBOARD, at=T0)

    store.attach_message(property_id, channel_id=CHANNEL, message_id=MESSAGE, rendered=rendered)

    assert sync_jobs(session_factory) == [
        {"property_id": property_id, "channel_id": CHANNEL, "message_id": MESSAGE}
    ]


def test_attaching_to_a_missing_property_raises(store: SqlPropertyStore) -> None:
    rendered: Any = None
    with pytest.raises(PropertyNotFoundError):
        store.attach_message(999, channel_id=CHANNEL, message_id=MESSAGE, rendered=rendered)


def test_worker_registers_the_poster(session_factory: sessionmaker[Session]) -> None:
    registration = default_registry(session_factory, settings=SETTINGS).get(NOTIFY)

    assert registration is not None
    assert isinstance(registration.handler, NotifyPost)


# --- Changes from anywhere sync the message -----------------------------------------


def test_status_changes_from_anywhere_sync_a_posted_message(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, property_id: int
) -> None:
    posted(session_factory, property_id)
    expected = {"property_id": property_id, "channel_id": CHANNEL, "message_id": MESSAGE}

    store.transition(property_id, PropertyStatus.APPROVED, actor=Actor.API, at=T0)
    assert sync_jobs(session_factory) == [expected]

    # Still queued, so the next change rides on the same job.
    store.deny(property_id, actor=Actor.DASHBOARD, at=T0, reason="too far")
    assert sync_jobs(session_factory) == [expected]


def test_snoozes_sync_a_posted_message(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, property_id: int
) -> None:
    posted(session_factory, property_id)

    store.snooze(property_id, until=T0 + HOUR, actor=Actor.DASHBOARD, at=T0)
    finish_jobs(session_factory)
    store.unsnooze_expired(now=T0 + HOUR)

    assert len(sync_jobs(session_factory)) == 2


def test_listings_going_and_coming_back_sync_a_posted_message(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, property_id: int
) -> None:
    posted(session_factory, property_id)
    with session_scope(session_factory) as session:
        listing_ids = list(
            session.scalars(select(Listing.id).where(Listing.property_id == property_id))
        )

    set_removed(session_factory, listing_ids, at=T0)
    assert store.mark_gone(listing_ids, at=T0) == [property_id]
    assert len(sync_jobs(session_factory)) == 1

    finish_jobs(session_factory)
    set_removed(session_factory, listing_ids, at=None)
    assert store.restore_relisted(listing_ids, at=T0) == [property_id]
    assert len(sync_jobs(session_factory)) == 2


def set_removed(
    session_factory: sessionmaker[Session], listing_ids: list[int], *, at: datetime | None
) -> None:
    with session_scope(session_factory) as session:
        session.execute(update(Listing).where(Listing.id.in_(listing_ids)).values(removed_at=at))


def test_changes_to_an_unposted_property_queue_nothing(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, property_id: int
) -> None:
    store.transition(property_id, PropertyStatus.APPROVED, actor=Actor.DASHBOARD, at=T0)
    store.snooze(property_id, until=T0 + HOUR, actor=Actor.DASHBOARD, at=T0)

    assert sync_jobs(session_factory) == []


def test_a_running_sync_does_not_stop_the_next_one(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, property_id: int
) -> None:
    posted(session_factory, property_id)
    store.transition(property_id, PropertyStatus.APPROVED, actor=Actor.DASHBOARD, at=T0)
    with session_scope(session_factory) as session:
        session.execute(update(Job).values(status=JobStatus.RUNNING))

    store.deny(property_id, actor=Actor.DASHBOARD, at=T0)

    assert len(sync_jobs(session_factory)) == 2


def finish_jobs(session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        session.execute(update(Job).values(status=JobStatus.SUCCEEDED))


# --- Superseded messages ------------------------------------------------------------


def test_syncing_a_reposted_message_greys_out_every_button(
    session_factory: sessionmaker[Session], property_id: int
) -> None:
    posted(session_factory, property_id, NEW_MESSAGE)
    requests, factory = fake_discord(httpx2.Response(200, json={"id": MESSAGE}))
    sync = MessageSync(session_factory, settings=SETTINGS, client_factory=factory)

    sync(
        ctx(
            MESSAGE_SYNC_JOB,
            {"property_id": property_id, "channel_id": CHANNEL, "message_id": MESSAGE},
        )
    )

    body = json.loads(requests[0].content)
    assert all(b["disabled"] for b in body["components"][0]["components"])
    assert body["embeds"][0]["footer"]["text"].endswith(" · reposted")


def test_a_merged_property_is_not_posted(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, property_id: int
) -> None:
    listing = NormalizedListing.model_validate(
        {"source": "test", "source_id": "l2", "url": "https://example.com/l2", "title": "Loft"}
    )
    (listing_id,) = (
        SqlListingStore(session_factory).upsert_batch("test", [listing], seen_at=T0).new_ids
    )
    survivor = store.ensure_for_listing(listing_id, at=T0).id
    with session_scope(session_factory) as session:
        session.execute(
            update(Property).where(Property.id == property_id).values(merged_into_id=survivor)
        )
    requests, factory = fake_discord()

    poster(session_factory, factory)(notify(property_id))

    assert requests == []
