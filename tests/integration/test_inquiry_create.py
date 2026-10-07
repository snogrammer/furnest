"""The inquiry.create job: drafting, auto/review modes, and inquiry_sent on delivery."""

from datetime import UTC, date, datetime
from email.message import EmailMessage
from typing import Any

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import (
    AppSettings,
    InquiryMode,
    Settings,
    SmtpSettings,
)
from furnest.db.criteria_store import CriteriaStore
from furnest.db.email_store import (
    EMAIL_SEND,
    EmailNotFoundError,
    NotSendableError,
    SmtpNotConfiguredError,
    SqlEmailStore,
    discard_draft_in,
    mark_sent_manually_in,
    send_draft_in,
)
from furnest.db.email_template_store import SqlEmailTemplateStore
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import Email, EmailStatus, EmailTemplate, Job, Property
from furnest.db.property_store import SqlPropertyStore
from furnest.db.session import session_scope
from furnest.db.settings_store import SqlSettingsStore
from furnest.domain import Actor, Criteria, NormalizedListing, PropertyStatus
from furnest.email.inquiry import INQUIRY_CREATE, InquiryCreate, request_inquiry_in
from furnest.email.send import EmailSend
from furnest.email.templates import TemplateRenderError
from furnest.worker import queue
from furnest.worker.execution import JobContext, default_registry

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
FROM = "furnest@example.com"
LISTER = "lister@example.com"


def _settings(mode: InquiryMode | None = None, *, smtp: bool = True) -> Settings:
    fields: dict[str, Any] = {}
    if smtp:
        fields["smtp"] = SmtpSettings(host="smtp.example.com", from_address=FROM)
    if mode is not None:
        fields["app"] = AppSettings(inquiry_mode=mode)
    return Settings(**fields)


def _ctx(property_id: int, **payload: Any) -> JobContext:  # noqa: ANN401
    return JobContext(
        job_id=1,
        kind=INQUIRY_CREATE,
        payload={"property_id": property_id, "actor": "discord", "actor_detail": "Sam (1)"}
        | payload,
        attempt=1,
        deadline=T0,
    )


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


@pytest.fixture
def make_property(session_factory: sessionmaker[Session]) -> Any:  # noqa: ANN401
    counter = iter(range(1, 1_000))

    def make(status: PropertyStatus = S.APPROVED, *, email: str | None = LISTER) -> int:
        n = next(counter)
        contact: dict[str, Any] = {"name": "Alex"}
        if email is not None:
            contact["email"] = email
        listing = NormalizedListing.model_validate(
            {
                "source": "test",
                "source_id": f"l{n}",
                "url": f"https://example.com/l{n}",
                "title": f"Sunny flat {n}",
                "contact": contact,
            }
        )
        (listing_id,) = (
            SqlListingStore(session_factory).upsert_batch("test", [listing], seen_at=T0).new_ids
        )
        property_id = SqlPropertyStore(session_factory).ensure_for_listing(listing_id, at=T0).id
        with session_scope(session_factory) as session:
            session.execute(
                update(Property).where(Property.id == property_id).values(status=status)
            )
        return property_id

    return make


def _status(session_factory: sessionmaker[Session], property_id: int) -> PropertyStatus:
    state = SqlPropertyStore(session_factory).get(property_id)
    assert state is not None
    return state.status


def _emails(session_factory: sessionmaker[Session], property_id: int) -> list[Email]:
    with session_scope(session_factory) as session:
        return list(session.scalars(select(Email).where(Email.property_id == property_id)))


def _send_jobs(session_factory: sessionmaker[Session]) -> int:
    with session_scope(session_factory) as session:
        return len(list(session.scalars(select(Job).where(Job.kind == EMAIL_SEND))))


def test_review_mode_drafts_and_waits(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
) -> None:
    property_id = make_property()
    CriteriaStore(session_factory).save(
        Criteria(my_move_in_date=date(2027, 1, 15), my_lease_months=6), created_by="test"
    )
    SqlEmailTemplateStore(session_factory).set_signature("Sam Example")

    InquiryCreate(session_factory, settings=_settings())(_ctx(property_id))

    (email,) = _emails(session_factory, property_id)
    assert email.status is EmailStatus.DRAFT
    assert email.to_address == LISTER
    assert email.from_address == FROM
    assert email.subject == "Inquiry about Sunny flat 1"
    assert "Hi Alex," in email.body_rendered
    assert "January 15, 2027 for 6 months" in email.body_rendered
    assert "Sam Example" in email.body_rendered
    assert _status(session_factory, property_id) is S.INQUIRY_DRAFTED
    assert _send_jobs(session_factory) == 0
    audit = SqlPropertyStore(session_factory).audit_events(property_id)[-1]
    assert (audit.to_status, audit.actor, audit.actor_detail) == (
        S.INQUIRY_DRAFTED,
        Actor.DISCORD,
        "Sam (1)",
    )
    (item,) = SqlEmailStore(session_factory).review_queue()
    assert (item.email.id, item.title) == (email.id, "Sunny flat 1")


def test_auto_mode_queues_the_send_and_delivery_marks_it_sent(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
) -> None:
    property_id = make_property()
    settings = _settings(InquiryMode.AUTO)

    InquiryCreate(session_factory, settings=settings)(_ctx(property_id))

    (email,) = _emails(session_factory, property_id)
    assert email.status is EmailStatus.QUEUED
    assert _send_jobs(session_factory) == 1
    # Drafted, not sent, until the server has the message.
    assert _status(session_factory, property_id) is S.INQUIRY_DRAFTED
    assert SqlEmailStore(session_factory).review_queue() == []

    sender = FakeSender()
    EmailSend(session_factory, settings=settings, sender_factory=lambda _: sender)(
        JobContext(
            job_id=2, kind=EMAIL_SEND, payload={"email_id": email.id}, attempt=1, deadline=T0
        )
    )

    assert len(sender.sent) == 1
    assert sender.sent[0]["To"] == LISTER
    assert _status(session_factory, property_id) is S.INQUIRY_SENT
    audit = SqlPropertyStore(session_factory).audit_events(property_id)[-1]
    assert (audit.actor, audit.payload) == (Actor.SYSTEM, {"email_id": email.id})


def test_stored_mode_overrides_the_env_default(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
) -> None:
    property_id = make_property()
    SqlSettingsStore(session_factory).set("inquiry_mode", "auto")

    InquiryCreate(session_factory, settings=_settings())(_ctx(property_id))

    (email,) = _emails(session_factory, property_id)
    assert email.status is EmailStatus.QUEUED


def test_auto_mode_without_an_address_leaves_a_draft(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
) -> None:
    property_id = make_property(email=None)

    InquiryCreate(session_factory, settings=_settings(InquiryMode.AUTO))(_ctx(property_id))

    (email,) = _emails(session_factory, property_id)
    assert (email.status, email.to_address) == (EmailStatus.DRAFT, None)
    assert _send_jobs(session_factory) == 0
    assert len(SqlEmailStore(session_factory).review_queue()) == 1


@pytest.mark.parametrize("status", [S.SUGGESTED, S.DENIED, S.INQUIRY_DRAFTED, S.ARCHIVED])
def test_skips_a_property_that_is_not_approved(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
    status: PropertyStatus,
) -> None:
    property_id = make_property(status)

    InquiryCreate(session_factory, settings=_settings())(_ctx(property_id))

    assert _emails(session_factory, property_id) == []
    assert _status(session_factory, property_id) is status


def test_skips_a_missing_property(session_factory: sessionmaker[Session]) -> None:
    InquiryCreate(session_factory, settings=_settings())(_ctx(999))


def test_running_twice_drafts_once(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
) -> None:
    property_id = make_property()
    handler = InquiryCreate(session_factory, settings=_settings())

    handler(_ctx(property_id))
    handler(_ctx(property_id))

    assert len(_emails(session_factory, property_id)) == 1


@pytest.mark.parametrize("mode", [InquiryMode.REVIEW, InquiryMode.AUTO])
def test_without_smtp_drafts_with_no_from_address(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
    mode: InquiryMode,
) -> None:
    property_id = make_property()

    InquiryCreate(session_factory, settings=_settings(mode, smtp=False))(_ctx(property_id))

    (email,) = _emails(session_factory, property_id)
    assert (email.status, email.from_address, email.to_address) == (
        EmailStatus.DRAFT,
        None,
        LISTER,
    )
    assert email.message_id.endswith("@furnest.invalid>")
    assert _send_jobs(session_factory) == 0
    assert _status(session_factory, property_id) is S.INQUIRY_DRAFTED


def test_sending_a_draft_needs_smtp_and_fills_in_the_from_address(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
) -> None:
    property_id = make_property()
    InquiryCreate(session_factory, settings=_settings(smtp=False))(_ctx(property_id))
    (draft,) = _emails(session_factory, property_id)

    with pytest.raises(SmtpNotConfiguredError), session_scope(session_factory) as session:
        send_draft_in(session, SmtpSettings(), draft.id, to=LISTER, subject="s", body="b")
    with session_scope(session_factory) as session:
        sent = send_draft_in(session, _settings().smtp, draft.id, to=LISTER, subject="s", body="b")

    assert (sent.status, sent.from_address) == (EmailStatus.QUEUED, FROM)
    assert sent.message_id.endswith("@example.com>")
    assert sent.message_id != draft.message_id
    assert _send_jobs(session_factory) == 1


@pytest.mark.parametrize("smtp", [True, False])
def test_marking_a_draft_sent_by_hand(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
    smtp: bool,
) -> None:
    property_id = make_property(email=None)
    InquiryCreate(session_factory, settings=_settings(smtp=smtp))(_ctx(property_id))
    (draft,) = _emails(session_factory, property_id)

    with session_scope(session_factory) as session:
        marked = mark_sent_manually_in(
            session, draft.id, subject="Edited", body="What I sent", at=T0
        )

    assert (marked.status, marked.to_address, marked.sent_at) == (EmailStatus.SENT, None, T0)
    assert (marked.subject, marked.body_rendered) == ("Edited", "What I sent")
    assert _send_jobs(session_factory) == 0
    assert _status(session_factory, property_id) is S.INQUIRY_SENT
    (last,) = [
        e
        for e in SqlPropertyStore(session_factory).audit_events(property_id)
        if e.to_status is S.INQUIRY_SENT
    ]
    assert (last.actor, last.actor_detail, last.payload) == (
        Actor.DASHBOARD,
        "sent manually",
        {"email_id": draft.id, "manual": True},
    )
    assert SqlEmailStore(session_factory).review_queue() == []
    with pytest.raises(NotSendableError), session_scope(session_factory) as session:
        mark_sent_manually_in(session, draft.id, subject="s", body="b", at=T0)


def test_marking_a_missing_email_sent_raises(session_factory: sessionmaker[Session]) -> None:
    with pytest.raises(EmailNotFoundError), session_scope(session_factory) as session:
        mark_sent_manually_in(session, 999, subject="s", body="b", at=T0)


def test_the_review_queue_says_where_to_send_by_hand(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
) -> None:
    property_id = make_property(email=None)
    InquiryCreate(session_factory, settings=_settings())(_ctx(property_id))

    (item,) = SqlEmailStore(session_factory).review_queue()

    assert (item.source, item.listing_url) == ("test", f"https://example.com/l{property_id}")


def test_a_broken_template_fails_the_job(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
) -> None:
    property_id = make_property()
    SqlEmailTemplateStore(session_factory).save("default", subject="{{ listing.nope }}", body="Hi")

    with pytest.raises(TemplateRenderError):
        InquiryCreate(session_factory, settings=_settings())(_ctx(property_id))

    assert _status(session_factory, property_id) is S.APPROVED


def test_a_missing_default_template_falls_back_to_the_built_in_text(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
) -> None:
    property_id = make_property()
    with session_scope(session_factory) as session:
        session.execute(delete(EmailTemplate))

    InquiryCreate(session_factory, settings=_settings())(_ctx(property_id))

    (email,) = _emails(session_factory, property_id)
    assert email.subject == "Inquiry about Sunny flat 1"


def test_unknown_actor_in_payload_is_recorded_as_system(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
) -> None:
    property_id = make_property()

    InquiryCreate(session_factory, settings=_settings())(
        _ctx(property_id, actor="martian", actor_detail=42)
    )

    audit = SqlPropertyStore(session_factory).audit_events(property_id)[-1]
    assert (audit.actor, audit.actor_detail) == (Actor.SYSTEM, None)


def test_request_inquiry_approves_a_suggested_property_and_queues_once(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
) -> None:
    property_id = make_property(S.SUGGESTED)

    with session_scope(session_factory) as session:
        first = request_inquiry_in(session, property_id, actor=Actor.DASHBOARD, at=T0)
    with session_scope(session_factory) as session:
        second = request_inquiry_in(session, property_id, actor=Actor.DASHBOARD, at=T0)

    assert (first, second) == (True, False)
    assert _status(session_factory, property_id) is S.APPROVED
    with session_scope(session_factory) as session:
        job = queue.pending(session, INQUIRY_CREATE, property_id=property_id)
        assert job is not None
        assert job.payload == {
            "property_id": property_id,
            "actor": "dashboard",
            "actor_detail": None,
        }


def test_send_and_discard_refuse_a_sent_email(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
) -> None:
    property_id = make_property()
    InquiryCreate(session_factory, settings=_settings(InquiryMode.AUTO))(_ctx(property_id))
    (email,) = _emails(session_factory, property_id)

    with pytest.raises(NotSendableError), session_scope(session_factory) as session:
        send_draft_in(session, _settings().smtp, email.id, to=LISTER, subject="s", body="b")
    with pytest.raises(NotSendableError), session_scope(session_factory) as session:
        discard_draft_in(session, email.id)


def test_failed_inquiry_stays_in_the_queue_until_resent(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
) -> None:
    property_id = make_property()
    InquiryCreate(session_factory, settings=_settings(InquiryMode.AUTO))(_ctx(property_id))
    (email,) = _emails(session_factory, property_id)
    store = SqlEmailStore(session_factory)
    store.record_failure(email.id, "550 no such user", final=True)

    (item,) = store.review_queue()
    assert (item.email.status, item.email.error) == (EmailStatus.FAILED, "550 no such user")

    with session_scope(session_factory) as session:
        resent = send_draft_in(
            session, _settings().smtp, email.id, to="other@example.com", subject="s", body="b"
        )
    assert (resent.status, resent.error, resent.attempts) == (EmailStatus.QUEUED, None, 0)
    assert store.review_queue() == []


def test_the_worker_registers_the_handler(session_factory: sessionmaker[Session]) -> None:
    registry = default_registry(session_factory, settings=_settings())

    registration = registry.get(INQUIRY_CREATE)
    assert registration is not None
    assert isinstance(registration.handler, InquiryCreate)


def test_a_merged_property_drafts_for_the_one_it_became(
    session_factory: sessionmaker[Session],
    make_property: Any,  # noqa: ANN401
) -> None:
    survivor, absorbed = make_property(S.APPROVED), make_property(S.APPROVED)
    with session_scope(session_factory) as session:
        session.execute(
            update(Property).where(Property.id == absorbed).values(merged_into_id=survivor)
        )

    InquiryCreate(session_factory, settings=_settings())(_ctx(absorbed))

    assert len(_emails(session_factory, survivor)) == 1
    assert _emails(session_factory, absorbed) == []
    assert _status(session_factory, survivor) is S.INQUIRY_DRAFTED
    assert _status(session_factory, absorbed) is S.APPROVED
