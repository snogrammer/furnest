"""Queueing and sending email against a real (local, in-process) SMTP server."""

import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.message import Message
from typing import Any

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP, AuthResult, Envelope, LoginPassword
from aiosmtpd.smtp import Session as SmtpSession
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import Settings, SmtpSecurity, SmtpSettings
from furnest.db.email_store import (
    EMAIL_SEND,
    SmtpNotConfiguredError,
    SqlEmailStore,
    StoredEmail,
    queue_email,
)
from furnest.db.models import Email, EmailStatus, Job, Property
from furnest.db.session import session_scope
from furnest.email.send import EmailSend
from furnest.worker import queue
from furnest.worker.execution import JobContext, default_registry

pytestmark = [pytest.mark.integration, pytest.mark.allow_hosts(["127.0.0.1"])]

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
FROM = "furnest@example.com"
TO = "landlord@example.com"
RIGHT_PASSWORD = "right-password"  # noqa: S105 - fake secret under test
WRONG_PASSWORD = "wrong-password"  # noqa: S105 - fake secret under test


@dataclass
class Inbox:
    """aiosmtpd handler: stores what it receives, or answers with a canned reply."""

    received: list[tuple[Envelope, Message]] = field(default_factory=list)
    rcpt_reply: str | None = None
    data_reply: str | None = None

    async def handle_RCPT(  # noqa: N802 - aiosmtpd hook name
        self,
        server: SMTP,
        session: SmtpSession,
        envelope: Envelope,
        address: str,
        rcpt_options: list[str],
    ) -> str:
        if self.rcpt_reply is not None:
            return self.rcpt_reply
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(  # noqa: N802 - aiosmtpd hook name
        self, server: SMTP, session: SmtpSession, envelope: Envelope
    ) -> str:
        if self.data_reply is not None:
            return self.data_reply
        assert isinstance(envelope.content, bytes)
        self.received.append((envelope, message_from_bytes(envelope.content)))
        return "250 Message accepted"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@dataclass
class Server:
    inbox: Inbox
    port: int
    logins: list[tuple[bytes, bytes]]


@pytest.fixture
def server() -> Iterator[Server]:
    logins: list[tuple[bytes, bytes]] = []

    def authenticator(
        server: SMTP, session: SmtpSession, envelope: Envelope, mechanism: str, auth_data: object
    ) -> AuthResult:
        assert isinstance(auth_data, LoginPassword)
        logins.append((auth_data.login, auth_data.password))
        # handled=False makes aiosmtpd answer a refusal with 535; by default it stays silent.
        return AuthResult(success=auth_data.password == RIGHT_PASSWORD.encode(), handled=False)

    inbox = Inbox()
    port = _free_port()
    controller = Controller(
        inbox,
        hostname="127.0.0.1",
        port=port,
        authenticator=authenticator,
        auth_require_tls=False,
    )
    controller.start()
    try:
        yield Server(inbox, port, logins)
    finally:
        controller.stop()


def _settings(port: int, **smtp: Any) -> Settings:  # noqa: ANN401
    fields: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": port,
        "security": SmtpSecurity.NONE,
        "from_address": FROM,
        "from_name": "Furnest Renter",
        "timeout_seconds": 5,
    }
    return Settings(smtp=SmtpSettings(**(fields | smtp)))


QueueFn = Callable[..., StoredEmail]


@pytest.fixture
def queue_one(session_factory: sessionmaker[Session]) -> QueueFn:
    def queue_one(settings: Settings, **overrides: Any) -> StoredEmail:  # noqa: ANN401
        fields: dict[str, Any] = {"to": TO, "subject": "Is it available?", "body": "Hi there,\n"}
        with session_scope(session_factory) as session:
            return queue_email(session, settings.smtp, **(fields | overrides))

    return queue_one


def _ctx(email_id: int, *, attempt: int = 1, max_attempts: int = 5) -> JobContext:
    return JobContext(
        job_id=1,
        kind=EMAIL_SEND,
        payload={"email_id": email_id},
        attempt=attempt,
        deadline=T0,
        max_attempts=max_attempts,
    )


# --- Queueing -------------------------------------------------------------------


def test_queue_email_logs_row_and_job_together(
    session_factory: sessionmaker[Session], server: Server, queue_one: QueueFn
) -> None:
    with session_scope(session_factory) as session:
        prop = Property()
        session.add(prop)
        session.flush()
        property_id = prop.id

    email = queue_one(_settings(server.port), property_id=property_id)

    assert email.status is EmailStatus.QUEUED
    assert email.from_address == FROM
    assert email.message_id.startswith("<") and email.message_id.endswith("@example.com>")
    assert email.attempts == 0 and email.sent_at is None
    with session_scope(session_factory) as session:
        job = session.scalars(select(Job)).one()
        assert (job.kind, job.payload) == (EMAIL_SEND, {"email_id": email.id})
    assert SqlEmailStore(session_factory).for_property(property_id) == [email]


def test_queue_email_rolls_back_with_the_caller(
    session_factory: sessionmaker[Session], server: Server, queue_one: QueueFn
) -> None:
    with pytest.raises(RuntimeError), session_scope(session_factory) as session:
        queue_email(session, _settings(server.port).smtp, to=TO, subject="s", body="b")
        raise RuntimeError("caller's change failed")
    with session_scope(session_factory) as session:
        assert session.scalar(select(func.count()).select_from(Email)) == 0
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_queue_email_needs_a_from_address(session_factory: sessionmaker[Session]) -> None:
    with pytest.raises(SmtpNotConfiguredError), session_scope(session_factory) as session:
        queue_email(session, SmtpSettings(host="127.0.0.1"), to=TO, subject="s", body="b")


def test_message_ids_are_unique(server: Server, queue_one: QueueFn) -> None:
    settings = _settings(server.port)
    assert queue_one(settings).message_id != queue_one(settings).message_id


# --- Sending --------------------------------------------------------------------


def test_send_delivers_and_marks_sent(
    session_factory: sessionmaker[Session], server: Server, queue_one: QueueFn
) -> None:
    settings = _settings(server.port)
    email = queue_one(settings, subject="Café near the park?", body="Hello — is it free?\n")

    EmailSend(session_factory, settings=settings)(_ctx(email.id))

    ((envelope, message),) = server.inbox.received
    assert envelope.mail_from == FROM
    assert envelope.rcpt_tos == [TO]
    assert message["To"] == TO
    assert "Furnest Renter" in message["From"] and FROM in message["From"]
    assert message["Message-ID"] == email.message_id
    payload = message.get_payload(decode=True)
    assert isinstance(payload, bytes)
    assert payload.decode("utf-8").strip() == "Hello — is it free?"
    stored = SqlEmailStore(session_factory).get(email.id)
    assert stored is not None
    assert stored.status is EmailStatus.SENT
    assert stored.sent_at is not None and stored.attempts == 1 and stored.error is None
    assert server.logins == []


def test_send_logs_in_when_username_set(
    session_factory: sessionmaker[Session], server: Server, queue_one: QueueFn
) -> None:
    settings = _settings(server.port, username="renter", password=RIGHT_PASSWORD)
    email = queue_one(settings)

    EmailSend(session_factory, settings=settings)(_ctx(email.id))

    assert server.logins == [(b"renter", RIGHT_PASSWORD.encode())]
    assert len(server.inbox.received) == 1


def test_bad_credentials_fail_permanently(
    session_factory: sessionmaker[Session], server: Server, queue_one: QueueFn
) -> None:
    settings = _settings(server.port, username="renter", password=WRONG_PASSWORD)
    email = queue_one(settings)

    EmailSend(session_factory, settings=settings)(_ctx(email.id))  # no raise: no retry

    stored = SqlEmailStore(session_factory).get(email.id)
    assert stored is not None
    assert stored.status is EmailStatus.FAILED
    assert stored.error is not None and "SMTPAuthenticationError" in stored.error
    assert server.inbox.received == []


def test_already_sent_is_a_no_op(
    session_factory: sessionmaker[Session], server: Server, queue_one: QueueFn
) -> None:
    settings = _settings(server.port)
    email = queue_one(settings)
    handler = EmailSend(session_factory, settings=settings)
    handler(_ctx(email.id))
    handler(_ctx(email.id, attempt=2))
    assert len(server.inbox.received) == 1
    stored = SqlEmailStore(session_factory).get(email.id)
    assert stored is not None and stored.attempts == 1


def test_missing_row_is_a_no_op(session_factory: sessionmaker[Session], server: Server) -> None:
    EmailSend(session_factory, settings=_settings(server.port))(_ctx(999))
    assert server.inbox.received == []


def test_rejected_recipient_fails_permanently(
    session_factory: sessionmaker[Session], server: Server, queue_one: QueueFn
) -> None:
    server.inbox.rcpt_reply = "550 No such user"
    settings = _settings(server.port)
    email = queue_one(settings)

    EmailSend(session_factory, settings=settings)(_ctx(email.id))

    stored = SqlEmailStore(session_factory).get(email.id)
    assert stored is not None
    assert stored.status is EmailStatus.FAILED
    assert stored.attempts == 1
    assert stored.error is not None and "No such user" in stored.error


def test_temporary_rejection_retries_until_the_last_attempt(
    session_factory: sessionmaker[Session], server: Server, queue_one: QueueFn
) -> None:
    server.inbox.data_reply = "451 Try again later"
    settings = _settings(server.port)
    email = queue_one(settings)
    handler = EmailSend(session_factory, settings=settings)
    store = SqlEmailStore(session_factory)

    with pytest.raises(OSError, match="Try again later"):
        handler(_ctx(email.id, attempt=1, max_attempts=2))
    stored = store.get(email.id)
    assert stored is not None
    assert (stored.status, stored.attempts) == (EmailStatus.QUEUED, 1)
    assert stored.error is not None and "451" in stored.error

    with pytest.raises(OSError):
        handler(_ctx(email.id, attempt=2, max_attempts=2))
    stored = store.get(email.id)
    assert stored is not None
    assert (stored.status, stored.attempts) == (EmailStatus.FAILED, 2)

    # The server recovers and a retry (say, a re-queued job) goes through.
    server.inbox.data_reply = None
    handler(_ctx(email.id, attempt=1))
    stored = store.get(email.id)
    assert stored is not None and stored.status is EmailStatus.SENT


def test_server_down_is_retried(session_factory: sessionmaker[Session], queue_one: QueueFn) -> None:
    settings = _settings(_free_port())  # nothing listens there
    email = queue_one(settings)

    with pytest.raises(ConnectionRefusedError):
        EmailSend(session_factory, settings=settings)(_ctx(email.id))

    stored = SqlEmailStore(session_factory).get(email.id)
    assert stored is not None
    assert stored.status is EmailStatus.QUEUED
    assert stored.error is not None and "ConnectionRefusedError" in stored.error


def test_not_configured_records_and_raises(
    session_factory: sessionmaker[Session], server: Server, queue_one: QueueFn
) -> None:
    email = queue_one(_settings(server.port))
    unconfigured = Settings(smtp=SmtpSettings(from_address=FROM))  # no host

    with pytest.raises(SmtpNotConfiguredError):
        EmailSend(session_factory, settings=unconfigured)(_ctx(email.id))

    stored = SqlEmailStore(session_factory).get(email.id)
    assert stored is not None
    assert stored.status is EmailStatus.QUEUED
    assert stored.error is not None and "SMTP_HOST" in stored.error


def test_cancelled_job_sends_nothing(
    session_factory: sessionmaker[Session], server: Server, queue_one: QueueFn
) -> None:
    settings = _settings(server.port)
    email = queue_one(settings)
    ctx = _ctx(email.id)
    ctx.cancelled.set()
    EmailSend(session_factory, settings=settings)(ctx)
    assert server.inbox.received == []


def test_worker_queue_runs_the_registered_handler(
    session_factory: sessionmaker[Session], server: Server, queue_one: QueueFn
) -> None:
    settings = _settings(server.port)
    email = queue_one(settings)
    registration = default_registry(session_factory, settings=settings).get(EMAIL_SEND)
    assert registration is not None

    claimed = queue.claim(session_factory, "worker-1", lease=timedelta(seconds=60))
    assert claimed is not None and claimed.kind == EMAIL_SEND
    registration.handler(
        JobContext(
            job_id=claimed.id,
            kind=claimed.kind,
            payload=claimed.payload,
            attempt=claimed.attempt,
            deadline=T0,
            max_attempts=claimed.max_attempts,
        )
    )
    assert queue.complete(session_factory, claimed.id, "worker-1")

    assert len(server.inbox.received) == 1
    stored = SqlEmailStore(session_factory).get(email.id)
    assert stored is not None and stored.status is EmailStatus.SENT


def test_store_updates_ignore_a_missing_row(session_factory: sessionmaker[Session]) -> None:
    store = SqlEmailStore(session_factory)
    store.mark_sent(999)
    store.record_failure(999, "boom", final=True)
    assert store.get(999) is None
