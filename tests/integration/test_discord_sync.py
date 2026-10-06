"""MessageSync: the worker edits a match message to show its property's state."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import Settings
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import Property
from furnest.db.property_store import SqlPropertyStore
from furnest.db.session import session_scope
from furnest.domain import NormalizedListing, PropertyStatus
from furnest.email.inquiry import INQUIRY_CREATE
from furnest.notify.discord import DiscordClient, DiscordError
from furnest.notify.embeds import disabled_actions, match_message
from furnest.notify.sync import MESSAGE_SYNC_JOB, MessageSync, enqueue_sync
from furnest.worker import queue
from furnest.worker.execution import JobContext, default_registry

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CHANNEL = "1200000000000000000"
MESSAGE = "1300000000000000000"


@pytest.fixture
def property_id(session_factory: sessionmaker[Session]) -> int:
    """An approved property with one listing."""
    listing = NormalizedListing.model_validate(
        {"source": "test", "source_id": "l1", "url": "https://example.com/l1", "title": "Loft"}
    )
    (listing_id,) = (
        SqlListingStore(session_factory).upsert_batch("test", [listing], seen_at=T0).new_ids
    )
    pid = SqlPropertyStore(session_factory).ensure_for_listing(listing_id, at=T0).id
    with session_scope(session_factory) as session:
        session.execute(
            update(Property).where(Property.id == pid).values(status=PropertyStatus.APPROVED)
        )
    return pid


def ctx(property_id: int) -> JobContext:
    payload = {"property_id": property_id, "channel_id": CHANNEL, "message_id": MESSAGE}
    return JobContext(
        job_id=1, kind=MESSAGE_SYNC_JOB, payload=payload, attempt=1, deadline=datetime.now(UTC)
    )


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


def sync(
    session_factory: sessionmaker[Session], factory: Callable[[], DiscordClient | None]
) -> MessageSync:
    return MessageSync(
        session_factory,
        settings=Settings(),
        source_name=lambda key: key.upper(),
        client_factory=factory,
    )


def test_edits_the_message_to_the_current_state(
    session_factory: sessionmaker[Session], property_id: int
) -> None:
    ok = httpx2.Response(200, json={"id": MESSAGE, "channel_id": CHANNEL})
    requests, factory = fake_discord(ok)

    sync(session_factory, factory)(ctx(property_id))

    (request,) = requests
    assert request.method == "PATCH"
    assert request.url.path == f"/api/v10/channels/{CHANNEL}/messages/{MESSAGE}"
    detail = SqlPropertyStore(session_factory).detail(property_id)
    assert detail is not None
    expected: dict[str, Any] = match_message(
        detail,
        base_url=str(Settings().app.base_url),
        source_name=lambda key: key.upper(),
        disabled=disabled_actions(detail.property),
    )
    assert json.loads(request.content) == expected
    buttons = {b["label"]: b["disabled"] for b in expected["components"][0]["components"]}
    assert buttons == {"Approve": True, "Deny": False, "Inquire": False, "Snooze": False}
    assert "approved" in expected["embeds"][0]["footer"]["text"]


def test_does_nothing_when_discord_is_not_configured(
    session_factory: sessionmaker[Session], property_id: int
) -> None:
    sync(session_factory, lambda: None)(ctx(property_id))  # no client, no error


def test_a_deleted_message_is_dropped(
    session_factory: sessionmaker[Session], property_id: int
) -> None:
    gone = httpx2.Response(404, json={"message": "Unknown Message", "code": 10008})
    requests, factory = fake_discord(gone)

    sync(session_factory, factory)(ctx(property_id))

    assert len(requests) == 1


def test_a_missing_property_is_skipped(session_factory: sessionmaker[Session]) -> None:
    requests, factory = fake_discord()

    sync(session_factory, factory)(ctx(999))

    assert requests == []


def test_other_discord_errors_raise_so_the_queue_retries(
    session_factory: sessionmaker[Session], property_id: int
) -> None:
    _, factory = fake_discord(
        httpx2.Response(403, json={"message": "Missing Access", "code": 50001})
    )

    with pytest.raises(DiscordError):
        sync(session_factory, factory)(ctx(property_id))


def test_enqueue_sync_skips_duplicates_and_clicks_without_a_message(
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        enqueue_sync(session, 1, channel_id=CHANNEL, message_id=MESSAGE)
        enqueue_sync(session, 1, channel_id=CHANNEL, message_id=MESSAGE)
        enqueue_sync(session, 1, channel_id=None, message_id=MESSAGE)
        enqueue_sync(session, 1, channel_id=CHANNEL, message_id=None)
        job = queue.pending(session, MESSAGE_SYNC_JOB, message_id=MESSAGE)
        assert job is not None
        assert job.payload == {"property_id": 1, "channel_id": CHANNEL, "message_id": MESSAGE}


def test_worker_registers_the_sync_and_inquiry_handlers(
    session_factory: sessionmaker[Session],
) -> None:
    registry = default_registry(session_factory)

    assert registry.get(MESSAGE_SYNC_JOB) is not None
    inquiry = registry.get(INQUIRY_CREATE)
    assert inquiry is not None
    payload = {"property_id": 1, "actor": "discord", "actor_detail": None}
    job = JobContext(
        job_id=2, kind=INQUIRY_CREATE, payload=payload, attempt=1, deadline=datetime.now(UTC)
    )
    inquiry.handler(job)  # logs only, until #42
