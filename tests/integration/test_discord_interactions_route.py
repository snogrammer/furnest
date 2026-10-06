"""``POST /discord/interactions``: signatures, PING, dispatch and replay protection."""

import json
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from nacl.signing import SigningKey
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import Settings
from furnest.db.base import utcnow
from furnest.db.models import DiscordInteraction, Job
from furnest.db.session import session_scope
from furnest.notify.custom_ids import DiscordAction
from furnest.notify.interactions import (
    EPHEMERAL,
    Interaction,
    InteractionType,
    ResponseType,
)
from furnest.notify.sync import MESSAGE_SYNC_JOB
from furnest.web.app import create_app
from furnest.web.discord_dispatch import InteractionDispatcher

pytestmark = pytest.mark.integration

KEY = SigningKey(bytes(range(32)))
PATH = "/discord/interactions"

Post = Callable[..., Response]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, migrated_engine: Engine) -> Iterator[TestClient]:
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", KEY.verify_key.encode().hex())
    with TestClient(create_app(Settings(), engine=migrated_engine)) as client:
        # The endpoint's own behavior, without the buttons (test_discord_buttons.py).
        client.app.state.discord_dispatcher = InteractionDispatcher()  # type: ignore[attr-defined]
        yield client


@pytest.fixture
def post(client: TestClient) -> Post:
    """POST a body signed with ``KEY`` (or the given ``signature``) at ``timestamp``."""

    def _post(
        data: object,
        *,
        timestamp: str | None = None,
        signature: str | None = None,
        raw: bytes | None = None,
    ) -> Response:
        body = raw if raw is not None else json.dumps(data).encode()
        timestamp = timestamp or str(int(time.time()))
        signature = signature or KEY.sign(timestamp.encode() + body).signature.hex()
        headers = {
            "X-Signature-Ed25519": signature,
            "X-Signature-Timestamp": timestamp,
            "Content-Type": "application/json",
        }
        return client.post(PATH, content=body, headers=headers)

    return _post


def click(custom_id: str = "furnest:approve:42", **overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    data: dict[str, Any] = {
        "id": "1100000000000000001",
        "type": 3,
        "token": "interaction-token",
        "application_id": "1000000000000000000",
        "channel_id": "1200000000000000000",
        "message": {"id": "1300000000000000000"},
        "data": {"custom_id": custom_id, "component_type": 2},
        "member": {"user": {"id": "1400000000000000000", "username": "renter"}},
    }
    data.update(overrides)
    return data


def jobs(factory: sessionmaker[Session]) -> list[Job]:
    with session_scope(factory) as session:
        return list(session.scalars(select(Job).where(Job.kind == MESSAGE_SYNC_JOB)))


def recorded(factory: sessionmaker[Session]) -> list[str]:
    with session_scope(factory) as session:
        return list(session.scalars(select(DiscordInteraction.id)))


def test_ping_gets_pong_without_auth_or_csrf(post: Post, client: TestClient) -> None:
    response = post({"id": "1", "type": 1, "token": "t", "application_id": "2"})

    assert response.status_code == 200
    assert response.json() == {"type": 1}
    assert not client.cookies


def test_bad_signature_is_401(post: Post) -> None:
    other = SigningKey(bytes(range(1, 33)))
    timestamp = str(int(time.time()))
    body = b'{"type":1}'
    signature = other.sign(timestamp.encode() + body).signature.hex()

    response = post(None, raw=body, timestamp=timestamp, signature=signature)

    assert response.status_code == 401


@pytest.mark.parametrize("missing", ["X-Signature-Ed25519", "X-Signature-Timestamp"])
def test_missing_signature_header_is_401(client: TestClient, missing: str) -> None:
    headers = {"X-Signature-Ed25519": "ab" * 64, "X-Signature-Timestamp": str(int(time.time()))}
    del headers[missing]

    assert client.post(PATH, content=b'{"type":1}', headers=headers).status_code == 401


@pytest.mark.parametrize("age", [301, -301])
def test_stale_or_future_timestamp_is_401(post: Post, age: int) -> None:
    timestamp = str(int(time.time()) - age)

    assert post({"type": 1}, timestamp=timestamp).status_code == 401


def test_not_configured_is_404(migrated_engine: Engine) -> None:
    with TestClient(create_app(Settings(), engine=migrated_engine)) as client:
        assert client.post(PATH, content=b"{}").status_code == 404


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"type": 3}'])
def test_signed_garbage_is_400(post: Post, raw: bytes) -> None:
    assert post(None, raw=raw).status_code == 400


def test_unhandled_button_click_is_acked_and_syncs_the_message(
    post: Post, session_factory: sessionmaker[Session]
) -> None:
    response = post(click())

    assert response.status_code == 200
    assert response.json() == {"type": ResponseType.DEFERRED_UPDATE_MESSAGE}
    assert recorded(session_factory) == ["1100000000000000001"]
    (job,) = jobs(session_factory)
    assert job.payload == {
        "property_id": 42,
        "channel_id": "1200000000000000000",
        "message_id": "1300000000000000000",
    }


def test_replayed_interaction_is_a_no_op(
    post: Post, session_factory: sessionmaker[Session]
) -> None:
    first = post(click())
    replay = post(click())

    assert first.json() == replay.json() == {"type": ResponseType.DEFERRED_UPDATE_MESSAGE}
    assert recorded(session_factory) == ["1100000000000000001"]
    assert len(jobs(session_factory)) == 1


def test_foreign_custom_id_gets_ephemeral_reply(
    post: Post, session_factory: sessionmaker[Session]
) -> None:
    response = post(click("otherbot:thing"))

    body = response.json()
    assert body["type"] == ResponseType.CHANNEL_MESSAGE_WITH_SOURCE
    assert body["data"]["flags"] == EPHEMERAL
    assert jobs(session_factory) == []


def test_slash_command_gets_ephemeral_reply(post: Post) -> None:
    response = post(click(type=2, data={"name": "furnest"}))

    assert response.json()["type"] == ResponseType.CHANNEL_MESSAGE_WITH_SOURCE


def test_registered_handler_answers_instead_of_the_queue(
    post: Post, client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    seen: list[Interaction] = []

    def open_modal(_session: Session, interaction: Interaction) -> dict[str, Any]:
        seen.append(interaction)
        return {"type": ResponseType.MODAL, "data": {"custom_id": "furnest:deny:42"}}

    dispatcher = client.app.state.discord_dispatcher  # type: ignore[attr-defined]
    dispatcher.register(InteractionType.MESSAGE_COMPONENT, DiscordAction.DENY, open_modal)

    response = post(click("furnest:deny:42"))

    assert response.json()["type"] == ResponseType.MODAL
    assert [i.property_id for i in seen] == [42]
    assert jobs(session_factory) == []


def test_failing_handler_does_not_record_the_interaction(
    post: Post, client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    def boom(_session: Session, _interaction: Interaction) -> dict[str, Any]:
        raise RuntimeError("boom")

    dispatcher = client.app.state.discord_dispatcher  # type: ignore[attr-defined]
    dispatcher.register(InteractionType.MESSAGE_COMPONENT, DiscordAction.SNOOZE, boom)

    with pytest.raises(RuntimeError):
        post(click("furnest:snooze:42"))

    # Discord's retry of the same id must still be handled.
    assert recorded(session_factory) == []


def test_old_interaction_ids_are_pruned(post: Post, session_factory: sessionmaker[Session]) -> None:
    with session_scope(session_factory) as session:
        session.add(DiscordInteraction(id="1", type=3, received_at=utcnow() - timedelta(days=2)))
        session.add(DiscordInteraction(id="2", type=3, received_at=utcnow() - timedelta(hours=1)))

    post(click())

    assert sorted(recorded(session_factory)) == ["1100000000000000001", "2"]
