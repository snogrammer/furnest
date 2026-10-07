"""The match message's buttons, end to end through ``POST /discord/interactions``."""

import json
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from nacl.signing import SigningKey
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import Settings
from furnest.db.base import utcnow
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import AuditEvent, Job, Note, Property
from furnest.db.property_store import MAX_NOTE_LENGTH, SqlPropertyStore
from furnest.db.session import session_scope
from furnest.domain import Actor, NormalizedListing, PropertyStatus
from furnest.email.inquiry import INQUIRY_CREATE
from furnest.notify.interactions import EPHEMERAL, ResponseType
from furnest.notify.sync import MESSAGE_SYNC_JOB
from furnest.web.app import create_app
from furnest.web.discord_actions import REASON_INPUT

pytestmark = pytest.mark.integration

S = PropertyStatus
KEY = SigningKey(bytes(range(32)))
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CHANNEL = "1200000000000000000"
MESSAGE = "1300000000000000000"
DEFERRED = {"type": ResponseType.DEFERRED_UPDATE_MESSAGE}

Post = Callable[[dict[str, Any]], Response]
MakeProperty = Callable[..., int]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, migrated_engine: Engine) -> Iterator[TestClient]:
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", KEY.verify_key.encode().hex())
    with TestClient(create_app(Settings(), engine=migrated_engine)) as client:
        yield client


@pytest.fixture
def post(client: TestClient) -> Post:
    def _post(data: dict[str, Any]) -> Response:
        body = json.dumps(data).encode()
        timestamp = str(int(time.time()))
        headers = {
            "X-Signature-Ed25519": KEY.sign(timestamp.encode() + body).signature.hex(),
            "X-Signature-Timestamp": timestamp,
            "Content-Type": "application/json",
        }
        response = client.post("/discord/interactions", content=body, headers=headers)
        assert response.status_code == 200
        return response

    return _post


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlPropertyStore:
    return SqlPropertyStore(session_factory)


@pytest.fixture
def make_property(session_factory: sessionmaker[Session], store: SqlPropertyStore) -> MakeProperty:
    """A property for a new listing, forced into ``status``."""
    counter = iter(range(1, 1_000))

    def make(status: PropertyStatus = S.SUGGESTED) -> int:
        n = next(counter)
        listing = NormalizedListing.model_validate(
            {
                "source": "test",
                "source_id": f"l{n}",
                "url": f"https://example.com/l{n}",
                "title": f"Listing {n}",
            }
        )
        (listing_id,) = (
            SqlListingStore(session_factory).upsert_batch("test", [listing], seen_at=T0).new_ids
        )
        property_id = store.ensure_for_listing(listing_id, at=T0).id
        with session_scope(session_factory) as session:
            session.execute(
                update(Property).where(Property.id == property_id).values(status=status)
            )
        return property_id

    return make


_ids = iter(range(1_100_000_000_000_000_001, 1_200_000_000_000_000_000))


def click(action: str, property_id: int, *, interaction_id: str | None = None) -> dict[str, Any]:
    return {
        "id": interaction_id or str(next(_ids)),
        "type": 3,
        "token": "interaction-token",
        "application_id": "1000000000000000000",
        "channel_id": CHANNEL,
        "message": {"id": MESSAGE},
        "data": {"custom_id": f"furnest:{action}:{property_id}", "component_type": 2},
        "member": {"user": {"id": "1400000000000000000", "username": "renter"}},
    }


def submit_deny(
    property_id: int, reason: str, *, interaction_id: str | None = None
) -> dict[str, Any]:
    data = click("deny", property_id, interaction_id=interaction_id)
    data["type"] = 5
    data["data"] = {
        "custom_id": f"furnest:deny:{property_id}",
        "components": [
            {"type": 1, "components": [{"type": 4, "custom_id": REASON_INPUT, "value": reason}]}
        ],
    }
    return data


def jobs(factory: sessionmaker[Session], kind: str) -> list[Job]:
    with session_scope(factory) as session:
        return list(session.scalars(select(Job).where(Job.kind == kind).order_by(Job.id)))


def counts(factory: sessionmaker[Session]) -> tuple[int, int, int]:
    """Audit events, notes and jobs: everything a click could add."""
    with session_scope(factory) as session:
        return (
            session.scalar(select(func.count()).select_from(AuditEvent)) or 0,
            session.scalar(select(func.count()).select_from(Note)) or 0,
            session.scalar(select(func.count()).select_from(Job)) or 0,
        )


def assert_ephemeral(response: Response, text: str) -> None:
    body = response.json()
    assert body["type"] == ResponseType.CHANNEL_MESSAGE_WITH_SOURCE
    assert body["data"]["flags"] == EPHEMERAL
    assert text in body["data"]["content"]


def assert_synced(factory: sessionmaker[Session], property_id: int) -> None:
    (job,) = jobs(factory, MESSAGE_SYNC_JOB)
    assert job.payload == {
        "property_id": property_id,
        "channel_id": CHANNEL,
        "message_id": MESSAGE,
    }


# --- Approve ----------------------------------------------------------------------


def test_approve(
    post: Post,
    make_property: MakeProperty,
    store: SqlPropertyStore,
    session_factory: sessionmaker[Session],
) -> None:
    property_id = make_property()

    response = post(click("approve", property_id))

    assert response.json() == DEFERRED
    assert store.get(property_id).status is S.APPROVED  # type: ignore[union-attr]
    event = store.audit_events(property_id)[-1]
    assert event.actor is Actor.DISCORD
    assert event.actor_detail == "renter (1400000000000000000)"
    assert_synced(session_factory, property_id)


def test_approve_again_is_refused_and_syncs_the_stale_message(
    post: Post,
    make_property: MakeProperty,
    store: SqlPropertyStore,
    session_factory: sessionmaker[Session],
) -> None:
    property_id = make_property(S.APPROVED)

    response = post(click("approve", property_id))

    assert_ephemeral(response, "Can't approve this property: it is approved.")
    assert len(store.audit_events(property_id)) == 1  # only the creation
    assert_synced(session_factory, property_id)


def test_approve_reverses_a_deny(
    post: Post, make_property: MakeProperty, store: SqlPropertyStore
) -> None:
    property_id = make_property(S.DENIED)

    assert post(click("approve", property_id)).json() == DEFERRED
    assert store.get(property_id).status is S.APPROVED  # type: ignore[union-attr]


def test_approve_never_throws_away_a_drafted_inquiry(
    post: Post, make_property: MakeProperty, store: SqlPropertyStore
) -> None:
    property_id = make_property(S.INQUIRY_DRAFTED)

    response = post(click("approve", property_id))

    assert_ephemeral(response, "Can't approve this property: it is inquiry drafted.")
    assert store.get(property_id).status is S.INQUIRY_DRAFTED  # type: ignore[union-attr]


# --- Deny -------------------------------------------------------------------------


def test_deny_click_opens_the_reason_modal_without_changing_anything(
    post: Post,
    make_property: MakeProperty,
    store: SqlPropertyStore,
    session_factory: sessionmaker[Session],
) -> None:
    property_id = make_property()
    before = counts(session_factory)

    body = post(click("deny", property_id)).json()

    assert body["type"] == ResponseType.MODAL
    assert body["data"]["custom_id"] == f"furnest:deny:{property_id}"
    (row,) = body["data"]["components"]
    assert row["components"][0]["custom_id"] == REASON_INPUT
    assert store.get(property_id).status is S.SUGGESTED  # type: ignore[union-attr]
    assert counts(session_factory) == before


def test_deny_click_on_a_denied_property_is_refused(
    post: Post, make_property: MakeProperty, session_factory: sessionmaker[Session]
) -> None:
    property_id = make_property(S.DENIED)

    assert_ephemeral(post(click("deny", property_id)), "Can't deny this property: it is denied.")
    assert_synced(session_factory, property_id)


def test_deny_submit_denies_and_saves_the_reason_as_a_note(
    post: Post,
    make_property: MakeProperty,
    store: SqlPropertyStore,
    session_factory: sessionmaker[Session],
) -> None:
    property_id = make_property()

    response = post(submit_deny(property_id, "  No parking.  "))

    assert response.json() == DEFERRED
    assert store.get(property_id).status is S.DENIED  # type: ignore[union-attr]
    (note,) = store.notes(property_id)
    assert note.body_md == "No parking."
    assert note.actor is Actor.DISCORD
    event = store.audit_events(property_id)[-1]
    assert event.actor is Actor.DISCORD
    assert event.payload == {"note_id": note.id}
    assert_synced(session_factory, property_id)


def test_deny_submit_with_a_blank_reason_saves_no_note(
    post: Post, make_property: MakeProperty, store: SqlPropertyStore
) -> None:
    property_id = make_property()

    post(submit_deny(property_id, "   "))

    assert store.get(property_id).status is S.DENIED  # type: ignore[union-attr]
    assert store.notes(property_id) == []


def test_a_stale_deny_submit_is_refused(
    post: Post, make_property: MakeProperty, store: SqlPropertyStore
) -> None:
    property_id = make_property(S.DENIED)

    assert_ephemeral(post(submit_deny(property_id, "again")), "Can't deny this property")
    assert store.notes(property_id) == []


def test_a_reason_too_long_for_a_note_is_refused(
    post: Post, make_property: MakeProperty, store: SqlPropertyStore
) -> None:
    property_id = make_property()

    response = post(submit_deny(property_id, "x" * (MAX_NOTE_LENGTH + 1)))

    assert_ephemeral(response, "longer than")
    assert store.get(property_id).status is S.SUGGESTED  # type: ignore[union-attr]


# --- Snooze -----------------------------------------------------------------------


def test_snooze_hides_the_property_for_the_configured_hours(
    post: Post,
    make_property: MakeProperty,
    store: SqlPropertyStore,
    session_factory: sessionmaker[Session],
) -> None:
    property_id = make_property()
    before = utcnow()

    assert post(click("snooze", property_id)).json() == DEFERRED

    state = store.get(property_id)
    assert state is not None
    assert state.status is S.SUGGESTED
    assert state.snoozed_until is not None
    expected = before + timedelta(hours=72)
    assert expected <= state.snoozed_until <= expected + timedelta(minutes=1)
    event = store.audit_events(property_id)[-1]
    assert event.actor is Actor.DISCORD
    assert event.payload == {"snoozed_until": state.snoozed_until.isoformat()}
    assert_synced(session_factory, property_id)


def test_snooze_of_a_finished_property_is_refused(
    post: Post, make_property: MakeProperty, store: SqlPropertyStore
) -> None:
    property_id = make_property(S.ARCHIVED)

    assert_ephemeral(post(click("snooze", property_id)), "Can't snooze this property: it is")
    assert store.get(property_id).snoozed_until is None  # type: ignore[union-attr]


# --- Inquire ----------------------------------------------------------------------


def test_inquire_on_a_suggestion_approves_it_and_queues_the_inquiry(
    post: Post,
    make_property: MakeProperty,
    store: SqlPropertyStore,
    session_factory: sessionmaker[Session],
) -> None:
    property_id = make_property()

    assert post(click("inquire", property_id)).json() == DEFERRED

    assert store.get(property_id).status is S.APPROVED  # type: ignore[union-attr]
    assert store.audit_events(property_id)[-1].actor is Actor.DISCORD
    (job,) = jobs(session_factory, INQUIRY_CREATE)
    assert job.payload == {
        "property_id": property_id,
        "actor": "discord",
        "actor_detail": "renter (1400000000000000000)",
    }
    assert_synced(session_factory, property_id)


def test_inquire_on_an_approved_property_only_queues_the_inquiry(
    post: Post,
    make_property: MakeProperty,
    store: SqlPropertyStore,
    session_factory: sessionmaker[Session],
) -> None:
    property_id = make_property(S.APPROVED)

    post(click("inquire", property_id))

    assert store.get(property_id).status is S.APPROVED  # type: ignore[union-attr]
    assert len(store.audit_events(property_id)) == 1
    assert len(jobs(session_factory, INQUIRY_CREATE)) == 1


def test_a_second_inquire_click_does_not_queue_a_second_inquiry(
    post: Post, make_property: MakeProperty, session_factory: sessionmaker[Session]
) -> None:
    property_id = make_property()

    post(click("inquire", property_id))
    post(click("inquire", property_id))

    assert len(jobs(session_factory, INQUIRY_CREATE)) == 1
    assert len(jobs(session_factory, MESSAGE_SYNC_JOB)) == 1


@pytest.mark.parametrize("status", [S.DENIED, S.INQUIRY_SENT])
def test_inquire_is_refused_unless_suggested_or_approved(
    post: Post,
    make_property: MakeProperty,
    session_factory: sessionmaker[Session],
    status: PropertyStatus,
) -> None:
    property_id = make_property(status)

    assert_ephemeral(post(click("inquire", property_id)), "Can't send an inquiry for this property")
    assert jobs(session_factory, INQUIRY_CREATE) == []


# --- Every button -----------------------------------------------------------------

BUTTONS = [
    ("approve", lambda pid, iid: click("approve", pid, interaction_id=iid)),
    ("deny", lambda pid, iid: submit_deny(pid, "Too far.", interaction_id=iid)),
    ("inquire", lambda pid, iid: click("inquire", pid, interaction_id=iid)),
    ("snooze", lambda pid, iid: click("snooze", pid, interaction_id=iid)),
]


@pytest.mark.parametrize(("name", "make"), BUTTONS, ids=[name for name, _ in BUTTONS])
def test_replayed_interaction_changes_nothing(
    post: Post,
    make_property: MakeProperty,
    store: SqlPropertyStore,
    session_factory: sessionmaker[Session],
    name: str,
    make: Callable[[int, str], dict[str, Any]],
) -> None:
    property_id = make_property()
    first = post(make(property_id, "1150000000000000001"))
    after_first = (store.get(property_id), counts(session_factory))

    replay = post(make(property_id, "1150000000000000001"))

    assert first.json() == replay.json() == DEFERRED, name
    assert (store.get(property_id), counts(session_factory)) == after_first


@pytest.mark.parametrize("action", ["approve", "deny", "inquire", "snooze"])
def test_unknown_property_gets_an_ephemeral_reply(
    post: Post, session_factory: sessionmaker[Session], action: str
) -> None:
    assert_ephemeral(post(click(action, 999)), "no longer exists")
    assert counts(session_factory) == (0, 0, 0)


# --- Merged properties ---------------------------------------------------------------


@pytest.mark.parametrize("action", ["approve", "deny", "inquire", "snooze"])
def test_a_click_on_a_merged_property_points_at_the_survivor(
    post: Post,
    make_property: MakeProperty,
    session_factory: sessionmaker[Session],
    action: str,
) -> None:
    survivor, absorbed = make_property(), make_property()
    with session_scope(session_factory) as session:
        session.execute(
            update(Property).where(Property.id == absorbed).values(merged_into_id=survivor)
        )
    before = counts(session_factory)

    assert_ephemeral(post(click(action, absorbed)), f"property #{survivor}")
    assert_ephemeral(post(submit_deny(absorbed, "x")), f"property #{survivor}")
    assert counts(session_factory) == before
