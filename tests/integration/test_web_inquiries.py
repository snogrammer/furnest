"""The inquiry review queue (/inquiries) and the detail page's Inquire button and email log."""

import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import Settings
from furnest.db.email_store import EMAIL_SEND, SqlEmailStore, draft_email
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import Email, EmailStatus, Job, Property
from furnest.db.property_store import SqlPropertyStore
from furnest.db.session import make_session_factory, session_scope
from furnest.db.settings_store import SqlSettingsStore
from furnest.domain import Actor, NormalizedListing, PropertyStatus
from furnest.email.inquiry import INQUIRY_CREATE
from furnest.web.app import create_app

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
HTML = {"Accept": "text/html"}
FROM = "furnest@example.com"
LISTER = "lister@example.com"

Csrf = Callable[[str], str]


@pytest.fixture(autouse=True)
def smtp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM_ADDRESS", FROM)


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    # Built after smtp_env, so the app sees SMTP as configured.
    with TestClient(create_app(Settings(), engine=migrated_engine)) as client:
        yield client


class World:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.factory = session_factory
        self.n = 0

    def property(self, status: PropertyStatus = S.APPROVED, *, contact: str | None = None) -> int:
        """A property whose listing gives ``contact`` as its email address, if any."""
        self.n += 1
        listing = NormalizedListing.model_validate(
            {
                "source": "test",
                "source_id": f"l{self.n}",
                "url": f"https://example.com/l{self.n}",
                "title": f"Sunny flat {self.n}",
                "contact": {"email": contact},
            }
        )
        (listing_id,) = (
            SqlListingStore(self.factory).upsert_batch("test", [listing], seen_at=T0).new_ids
        )
        property_id = SqlPropertyStore(self.factory).ensure_for_listing(listing_id, at=T0).id
        self.set_status(property_id, status)
        return property_id

    def set_status(self, property_id: int, status: PropertyStatus) -> None:
        with session_scope(self.factory) as session:
            session.execute(
                update(Property).where(Property.id == property_id).values(status=status)
            )

    def draft(self, *, to: str | None = LISTER) -> tuple[int, int]:
        """A drafted property and its draft's id; the listing gives ``to`` as its address."""
        property_id = self.property(S.INQUIRY_DRAFTED, contact=to)
        settings = Settings()
        with session_scope(self.factory) as session:
            email = draft_email(
                session,
                settings.smtp,
                to=to,
                subject="Inquiry about the flat",
                body="Hi,\n\nIs it available?\n",
                property_id=property_id,
            )
        return property_id, email.id

    def email(self, email_id: int) -> Email | None:
        with session_scope(self.factory) as session:
            return session.get(Email, email_id)

    def status(self, property_id: int) -> PropertyStatus:
        state = SqlPropertyStore(self.factory).get(property_id)
        assert state is not None
        return state.status

    def jobs(self, kind: str) -> list[Job]:
        with session_scope(self.factory) as session:
            return list(session.scalars(select(Job).where(Job.kind == kind)))


@pytest.fixture
def world(session_factory: sessionmaker[Session]) -> World:
    return World(session_factory)


def post(client: TestClient, csrf: Csrf, path: str, data: dict[str, str] | None = None) -> Response:
    token = csrf(client.get("/inquiries", headers=HTML).text)
    return client.post(
        path,
        data={"csrf_token": token, **(data or {})},
        headers=HTML,
        follow_redirects=False,
    )


def form(**overrides: str) -> dict[str, str]:
    return {"to": LISTER, "subject": "Inquiry about the flat", "body": "Hi"} | overrides


# --- The queue ------------------------------------------------------------------


def test_queue_needs_auth(client: TestClient) -> None:
    response = client.get("/inquiries", headers=HTML, follow_redirects=False)

    assert response.status_code == 303


def test_empty_queue(authed_client: TestClient) -> None:
    html = authed_client.get("/inquiries", headers=HTML).text

    assert 'id="inquiries-empty"' in html
    assert 'href="/inquiries" aria-current="page"' in html


def test_queue_lists_drafts_with_their_text(authed_client: TestClient, world: World) -> None:
    property_id, email_id = world.draft()

    html = authed_client.get("/inquiries", headers=HTML).text

    assert f'id="inquiry-{email_id}"' in html
    assert f'<a href="/properties/{property_id}">Sunny flat 1</a>' in html
    assert f'value="{LISTER}"' in html
    assert "Is it available?" in html


def test_queue_hides_a_draft_whose_property_moved_on(
    authed_client: TestClient, world: World
) -> None:
    property_id, email_id = world.draft()
    world.set_status(property_id, S.ARCHIVED)

    html = authed_client.get("/inquiries", headers=HTML).text

    assert f'id="inquiry-{email_id}"' not in html


def test_draft_without_an_address_asks_for_one(authed_client: TestClient, world: World) -> None:
    world.draft(to=None)

    html = authed_client.get("/inquiries", headers=HTML).text

    assert "has no email address" in html


# --- Send -----------------------------------------------------------------------


def test_send_saves_the_edits_and_queues_it(
    authed_client: TestClient, csrf: Csrf, world: World
) -> None:
    property_id, email_id = world.draft(to=None)

    response = post(
        authed_client,
        csrf,
        f"/inquiries/{email_id}/send",
        form(to=" other@example.com ", subject="New\nsubject", body="Edited\r\nbody"),
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/inquiries?done=sent"
    email = world.email(email_id)
    assert email is not None
    assert (email.status, email.to_address, email.subject, email.body_rendered) == (
        EmailStatus.QUEUED,
        "other@example.com",
        "New subject",
        "Edited\nbody",
    )
    assert [job.payload for job in world.jobs(EMAIL_SEND)] == [{"email_id": email_id}]
    # inquiry_sent waits for delivery.
    assert world.status(property_id) is S.INQUIRY_DRAFTED
    assert "Inquiry queued to send." in authed_client.get(response.headers["location"]).text


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"to": "not-an-address"}, "Enter the email address"),
        ({"to": ""}, "Enter the email address"),
        ({"subject": "  "}, "The subject can&#39;t be empty."),
        ({"subject": "x" * 301}, "at most 300 characters"),
        ({"body": " \n "}, "The message can&#39;t be empty."),
        ({"body": "x" * 20_001}, "at most 20000 characters"),
    ],
)
def test_send_rejects_bad_input_and_keeps_what_was_typed(
    authed_client: TestClient,
    csrf: Csrf,
    world: World,
    fields: dict[str, str],
    message: str,
) -> None:
    _, email_id = world.draft()

    response = post(authed_client, csrf, f"/inquiries/{email_id}/send", form(**fields))

    assert response.status_code == 422
    assert message in response.text
    email = world.email(email_id)
    assert email is not None and email.status is EmailStatus.DRAFT
    assert world.jobs(EMAIL_SEND) == []
    if fields.get("to"):
        assert f'value="{fields["to"]}"' in response.text


def test_send_of_a_sent_email_is_a_conflict(
    authed_client: TestClient, csrf: Csrf, world: World
) -> None:
    _, email_id = world.draft()
    SqlEmailStore(world.factory).mark_sent(email_id)

    response = post(authed_client, csrf, f"/inquiries/{email_id}/send", form())

    assert response.status_code == 409


def test_send_after_the_property_moved_on_is_a_conflict(
    authed_client: TestClient, csrf: Csrf, world: World
) -> None:
    property_id, email_id = world.draft()
    world.set_status(property_id, S.ARCHIVED)

    response = post(authed_client, csrf, f"/inquiries/{email_id}/send", form())

    assert response.status_code == 409
    email = world.email(email_id)
    assert email is not None and email.status is EmailStatus.DRAFT
    assert world.jobs(EMAIL_SEND) == []


def test_send_of_an_unknown_email_is_404(authed_client: TestClient, csrf: Csrf) -> None:
    assert post(authed_client, csrf, "/inquiries/999/send", form()).status_code == 404


def test_send_needs_csrf(authed_client: TestClient, world: World) -> None:
    _, email_id = world.draft()

    response = authed_client.post(
        f"/inquiries/{email_id}/send", data=form(), headers=HTML, follow_redirects=False
    )

    assert response.status_code == 403


def test_a_failed_inquiry_can_be_resent(
    authed_client: TestClient, csrf: Csrf, world: World
) -> None:
    _, email_id = world.draft()
    store = SqlEmailStore(world.factory)
    with session_scope(world.factory) as session:
        session.execute(update(Email).where(Email.id == email_id).values(status=EmailStatus.FAILED))
    store.record_failure(email_id, "550 mailbox unavailable", final=True)

    html = authed_client.get("/inquiries", headers=HTML).text
    assert "Last attempt failed: 550 mailbox unavailable" in html

    response = post(authed_client, csrf, f"/inquiries/{email_id}/send", form())

    assert response.status_code == 303
    email = world.email(email_id)
    assert email is not None
    assert (email.status, email.error, email.attempts) == (EmailStatus.QUEUED, None, 0)


# --- Discard --------------------------------------------------------------------


def test_discard_deletes_the_draft_and_reapproves(
    authed_client: TestClient, csrf: Csrf, world: World
) -> None:
    property_id, email_id = world.draft()

    response = post(authed_client, csrf, f"/inquiries/{email_id}/discard")

    assert response.status_code == 303
    assert response.headers["location"] == "/inquiries?done=discarded"
    assert world.email(email_id) is None
    assert world.status(property_id) is S.APPROVED
    audit = SqlPropertyStore(world.factory).audit_events(property_id)[-1]
    assert (audit.actor, audit.payload) == (
        Actor.DASHBOARD,
        {"email_id": email_id, "discarded": True},
    )


def test_discarding_a_failed_inquiry_keeps_it_in_the_log(
    authed_client: TestClient, csrf: Csrf, world: World
) -> None:
    property_id, email_id = world.draft()
    SqlEmailStore(world.factory).record_failure(email_id, "550", final=True)

    response = post(authed_client, csrf, f"/inquiries/{email_id}/discard")

    assert response.status_code == 303
    email = world.email(email_id)
    assert email is not None and email.status is EmailStatus.FAILED
    assert world.status(property_id) is S.APPROVED


def test_discard_after_the_property_moved_on_is_a_conflict(
    authed_client: TestClient, csrf: Csrf, world: World
) -> None:
    property_id, email_id = world.draft()
    world.set_status(property_id, S.ARCHIVED)

    response = post(authed_client, csrf, f"/inquiries/{email_id}/discard")

    assert response.status_code == 409
    assert world.email(email_id) is not None


def test_discard_of_an_unknown_email_is_404(authed_client: TestClient, csrf: Csrf) -> None:
    assert post(authed_client, csrf, "/inquiries/999/discard").status_code == 404


# --- The detail page ------------------------------------------------------------


@pytest.mark.parametrize("status", [S.SUGGESTED, S.APPROVED])
def test_inquire_from_the_detail_page_queues_the_job(
    authed_client: TestClient, csrf: Csrf, world: World, status: PropertyStatus
) -> None:
    property_id = world.property(status)
    page = authed_client.get(f"/properties/{property_id}", headers=HTML).text
    assert f'action="/properties/{property_id}/inquire"' in page

    response = authed_client.post(
        f"/properties/{property_id}/inquire",
        data={"csrf_token": csrf(page)},
        headers=HTML,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/properties/{property_id}?inquiry=requested"
    assert world.status(property_id) is S.APPROVED
    (job,) = world.jobs(INQUIRY_CREATE)
    assert job.payload == {"property_id": property_id, "actor": "dashboard", "actor_detail": None}
    html = authed_client.get(response.headers["location"], headers=HTML).text
    assert "shows up under Inquiries" in html


def test_inquire_notice_in_auto_mode(authed_client: TestClient, world: World) -> None:
    property_id = world.property()
    SqlSettingsStore(world.factory).set("inquiry_mode", "auto")

    html = authed_client.get(f"/properties/{property_id}?inquiry=requested", headers=HTML).text

    assert "It is sent as soon as it&#39;s written." in html


def test_inquire_on_a_denied_property_is_a_conflict(
    authed_client: TestClient, csrf: Csrf, world: World
) -> None:
    property_id = world.property(S.DENIED)
    page = authed_client.get(f"/properties/{property_id}", headers=HTML).text
    assert "/inquire" not in page

    response = authed_client.post(
        f"/properties/{property_id}/inquire",
        data={"csrf_token": csrf(page)},
        headers=HTML,
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "Can&#39;t send an inquiry for this property" in response.text
    assert world.jobs(INQUIRY_CREATE) == []


def test_inquire_on_a_missing_property_is_404(authed_client: TestClient, csrf: Csrf) -> None:
    token = csrf(authed_client.get("/inquiries", headers=HTML).text)

    response = authed_client.post(
        "/properties/999/inquire", data={"csrf_token": token}, headers=HTML
    )

    assert response.status_code == 404


def test_detail_links_to_the_draft_and_logs_the_email(
    authed_client: TestClient, world: World
) -> None:
    property_id, email_id = world.draft()

    html = authed_client.get(f"/properties/{property_id}", headers=HTML).text

    assert "An inquiry draft is waiting for review." in html
    assert f'href="/inquiries#inquiry-{email_id}"' in html
    assert f'id="email-{email_id}"' in html
    assert "Is it available?" in html
    assert "/inquire" not in html


def test_detail_says_when_the_inquiry_is_being_sent(
    authed_client: TestClient, csrf: Csrf, world: World
) -> None:
    property_id, email_id = world.draft()
    post(authed_client, csrf, f"/inquiries/{email_id}/send", form())

    html = authed_client.get(f"/properties/{property_id}", headers=HTML).text

    assert "The inquiry is being sent." in html


def test_detail_without_emails(authed_client: TestClient, world: World) -> None:
    property_id = world.property()

    html = authed_client.get(f"/properties/{property_id}", headers=HTML).text

    assert "No emails yet." in html


def test_inquire_works_without_smtp(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    login: Any,  # noqa: ANN401
) -> None:
    monkeypatch.delenv("SMTP_HOST")
    world = World(make_session_factory(migrated_engine))
    property_id = world.property()
    with TestClient(create_app(Settings(), engine=migrated_engine)) as client:
        login(client)
        html = client.get(f"/properties/{property_id}", headers=HTML).text

    assert re.search(r'<button type="submit" aria-describedby="inquire-hint">Inquire', html)
    assert "copied and marked as sent by hand" in html


def test_send_without_smtp_is_a_conflict(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    login: Any,  # noqa: ANN401
    csrf: Csrf,
) -> None:
    world = World(make_session_factory(migrated_engine))
    _, email_id = world.draft()
    monkeypatch.delenv("SMTP_HOST")
    with TestClient(create_app(Settings(), engine=migrated_engine)) as client:
        login(client)
        response = post(client, csrf, f"/inquiries/{email_id}/send", form(body="Edited"))

    assert response.status_code == 409
    assert "SMTP isn&#39;t set up" in response.text
    assert "Edited" in response.text
    email = world.email(email_id)
    assert email is not None and email.status is EmailStatus.DRAFT
    assert world.jobs(EMAIL_SEND) == []


# --- Manual contact -------------------------------------------------------------


def manual(**overrides: str) -> dict[str, str]:
    return {"subject": "Inquiry about the flat", "body": "Hi, sent on the site"} | overrides


def test_queue_offers_mark_as_sent_with_the_listing_link(
    authed_client: TestClient, world: World
) -> None:
    _, email_id = world.draft(to=None)

    html = authed_client.get("/inquiries", headers=HTML).text

    assert f'formaction="/inquiries/{email_id}/mark-sent"' in html
    assert 'href="https://example.com/l1"' in html


def test_mark_sent_logs_the_text_and_moves_the_property(
    authed_client: TestClient, csrf: Csrf, world: World
) -> None:
    property_id, email_id = world.draft(to=None)

    response = post(
        authed_client,
        csrf,
        f"/inquiries/{email_id}/mark-sent",
        manual(subject=" Edited\n subject ", body="Sent it\r\nthere"),
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/inquiries?done=marked"
    email = world.email(email_id)
    assert email is not None
    assert (email.status, email.subject, email.body_rendered, email.to_address) == (
        EmailStatus.SENT,
        "Edited subject",
        "Sent it\nthere",
        None,
    )
    assert world.status(property_id) is S.INQUIRY_SENT
    assert world.jobs(EMAIL_SEND) == []
    flash = authed_client.get(response.headers["location"], headers=HTML).text
    assert "Inquiry marked as sent." in flash


def test_mark_sent_from_the_detail_page_goes_back_there(
    authed_client: TestClient, csrf: Csrf, world: World
) -> None:
    property_id, email_id = world.draft(to=None)

    response = post(
        authed_client, csrf, f"/inquiries/{email_id}/mark-sent", manual(back="property")
    )

    assert response.headers["location"] == f"/properties/{property_id}?inquiry=marked"
    html = authed_client.get(response.headers["location"], headers=HTML).text
    assert "Inquiry marked as sent." in html
    assert "sent by hand" in html


def test_mark_sent_rejects_an_empty_message(
    authed_client: TestClient, csrf: Csrf, world: World
) -> None:
    _, email_id = world.draft(to=None)

    response = post(authed_client, csrf, f"/inquiries/{email_id}/mark-sent", manual(body=" "))

    assert response.status_code == 422
    assert "The message can&#39;t be empty." in response.text
    email = world.email(email_id)
    assert email is not None and email.status is EmailStatus.DRAFT


def test_mark_sent_after_the_property_moved_on_is_a_conflict(
    authed_client: TestClient, csrf: Csrf, world: World
) -> None:
    property_id, email_id = world.draft(to=None)
    world.set_status(property_id, S.ARCHIVED)

    response = post(authed_client, csrf, f"/inquiries/{email_id}/mark-sent", manual())

    assert response.status_code == 409
    email = world.email(email_id)
    assert email is not None and email.status is EmailStatus.DRAFT


def test_mark_sent_of_a_sent_email_is_a_conflict(
    authed_client: TestClient, csrf: Csrf, world: World
) -> None:
    _, email_id = world.draft()
    SqlEmailStore(world.factory).mark_sent(email_id)

    response = post(authed_client, csrf, f"/inquiries/{email_id}/mark-sent", manual())

    assert response.status_code == 409


def test_mark_sent_of_an_unknown_email_is_404(authed_client: TestClient, csrf: Csrf) -> None:
    assert post(authed_client, csrf, "/inquiries/999/mark-sent", manual()).status_code == 404


def test_mark_sent_needs_csrf(authed_client: TestClient, world: World) -> None:
    _, email_id = world.draft(to=None)

    response = authed_client.post(
        f"/inquiries/{email_id}/mark-sent", data=manual(), headers=HTML, follow_redirects=False
    )

    assert response.status_code == 403


def test_detail_shows_the_platform_link_for_manual_contact(
    authed_client: TestClient, world: World
) -> None:
    property_id = world.property()

    html = authed_client.get(f"/properties/{property_id}", headers=HTML).text

    assert "Manual contact required." in html
    assert re.search(r'<a class="button" href="https://example.com/l1"[^>]*>Contact on test', html)
    assert "Message to send" not in html


def test_detail_has_no_platform_link_when_the_listing_has_an_email(
    authed_client: TestClient, world: World
) -> None:
    property_id = world.property(contact=LISTER)

    html = authed_client.get(f"/properties/{property_id}", headers=HTML).text

    assert "Manual contact required." not in html


def test_detail_shows_the_draft_ready_to_copy(authed_client: TestClient, world: World) -> None:
    property_id, email_id = world.draft(to=None)

    html = authed_client.get(f"/properties/{property_id}", headers=HTML).text

    assert "Message to send" in html
    assert f'action="/inquiries/{email_id}/mark-sent"' in html
    assert '<input type="hidden" name="back" value="property">' in html
    assert 'data-copy-target="manual-body"' in html
    assert "/static/js/copy.js" in html


def test_detail_has_no_copy_block_for_an_emailed_draft(
    authed_client: TestClient, world: World
) -> None:
    property_id, _ = world.draft()

    html = authed_client.get(f"/properties/{property_id}", headers=HTML).text

    assert "Message to send" not in html
