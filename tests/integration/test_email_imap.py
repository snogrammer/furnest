"""The IMAP reply poll against a fake mailbox: matching, logging, and ``responded``."""

import imaplib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import ImapSettings, Settings, SmtpSettings
from furnest.db.email_store import SqlEmailStore, awaiting_reply, record_reply_in
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import AuditEvent, Email, EmailDirection, EmailStatus, Property
from furnest.db.property_store import SqlPropertyStore
from furnest.db.session import session_scope
from furnest.domain import Actor, NormalizedListing, PropertyStatus
from furnest.email.imap import (
    FETCH_CHUNK,
    IMAP_POLL,
    ImapError,
    ImapReplyPoll,
    schedule_imap_poll,
)
from furnest.worker.execution import JobContext

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(hours=3)
FROM = "furnest@example.com"
LISTER = "lister@example.com"
# (status=..., to=..., sent_at=...) -> (property id, email id, Message-ID)
MakeInquiry = Callable[..., tuple[int, int, str]]
PASSWORD = "imap-password"  # noqa: S105 - fake secret under test


@dataclass
class FakeImap:
    """An in-memory mailbox speaking the slice of imaplib the poll uses."""

    messages: dict[int, tuple[datetime, bytes]] = field(default_factory=dict)
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    search_status: str = "OK"
    vanish_on_full_fetch: bool = False
    logout_error: BaseException | None = None
    # Set after the header fetch, as if the job timed out mid-poll.
    cancel_after_headers: Any = None

    def add(self, raw: bytes, *, day: datetime = NOW) -> int:
        uid = max(self.messages, default=100) + 1
        self.messages[uid] = (day, raw)
        return uid

    def login(self, user: str, password: str) -> tuple[str, list[Any]]:
        self.calls.append(("login", user, password))
        return "OK", [b"logged in"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[Any]]:
        self.calls.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            if self.search_status != "OK":
                return self.search_status, [b"server says no"]
            _, since = args
            day = datetime.strptime(since, "%d-%b-%Y").replace(tzinfo=UTC)
            found = [str(u) for u, (d, _) in self.messages.items() if d >= day]
            return "OK", [" ".join(found).encode()]
        assert command == "FETCH"
        uids, what = args
        assert "PEEK" in what, "the poll must never set \\Seen"
        data: list[Any] = []
        for uid in uids.split(","):
            if int(uid) not in self.messages:
                continue
            if self.vanish_on_full_fetch and what == "(BODY.PEEK[])":
                continue
            raw = self.messages[int(uid)][1]
            content = raw.split(b"\n\n", 1)[0] if "HEADER" in what else raw
            data.append((f"1 (UID {uid} BODY[] {{{len(content)}}}".encode(), content))
            data.append(b")")
        if "HEADER" in what and self.cancel_after_headers is not None:
            self.cancel_after_headers.set()
        return "OK", data

    def logout(self) -> tuple[str, list[Any]]:
        self.calls.append(("logout",))
        if self.logout_error is not None:
            raise self.logout_error
        return "BYE", []


def _settings(**imap: Any) -> Settings:  # noqa: ANN401
    fields = {"host": "imap.example.com", "username": FROM, "password": PASSWORD} | imap
    return Settings(
        imap=ImapSettings(enabled=True, **fields),
        smtp=SmtpSettings(host="smtp.example.com", from_address=FROM),
    )


def _ctx() -> JobContext:
    return JobContext(job_id=IMAP_POLL, kind=IMAP_POLL, payload={}, attempt=1, deadline=NOW)


def _reply(
    in_reply_to: str | None, *, sender: str = LISTER, mid: str = "<r1@example.net>"
) -> bytes:
    message = EmailMessage()
    message["From"] = f"Alex <{sender}>"
    message["To"] = FROM
    message["Subject"] = "Re: Your listing"
    message["Date"] = "Tue, 06 Oct 2026 14:00:00 +0000"
    message["Message-ID"] = mid
    if in_reply_to is not None:
        message["In-Reply-To"] = in_reply_to
    message.set_content("Still available, want to see it?")
    return bytes(message).replace(b"\r\n", b"\n")


@pytest.fixture
def make_inquiry(session_factory: sessionmaker[Session]) -> MakeInquiry:
    counter = iter(range(1, 1_000))

    def make(
        status: PropertyStatus = S.INQUIRY_SENT,
        *,
        to: str | None = LISTER,
        sent_at: datetime = T0,
    ) -> tuple[int, int, str]:
        """A property in ``status`` with a sent inquiry: (property id, email id, Message-ID)."""
        n = next(counter)
        listing = NormalizedListing.model_validate(
            {
                "source": "test",
                "source_id": f"l{n}",
                "url": f"https://example.com/l{n}",
                "title": f"Flat {n}",
            }
        )
        (listing_id,) = (
            SqlListingStore(session_factory).upsert_batch("test", [listing], seen_at=T0).new_ids
        )
        property_id = SqlPropertyStore(session_factory).ensure_for_listing(listing_id, at=T0).id
        message_id = f"<inquiry-{n}@example.com>"
        with session_scope(session_factory) as session:
            session.execute(
                update(Property).where(Property.id == property_id).values(status=status)
            )
            row = Email(
                property_id=property_id,
                direction=EmailDirection.OUTBOUND,
                to_address=to,
                from_address=FROM if to else None,
                subject="Your listing",
                body_rendered="Hello",
                status=EmailStatus.SENT,
                attempts=1,
                message_id=message_id,
                created_at=sent_at,
                sent_at=sent_at,
            )
            session.add(row)
            session.flush()
            email_id = row.id
        return property_id, email_id, message_id

    return make


def _poll(
    session_factory: sessionmaker[Session], mailbox: FakeImap, settings: Settings | None = None
) -> ImapReplyPoll:
    return ImapReplyPoll(
        session_factory,
        settings=settings or _settings(),
        connect=lambda _: mailbox,
        clock=lambda: NOW,
    )


def _status(session_factory: sessionmaker[Session], property_id: int) -> PropertyStatus:
    state = SqlPropertyStore(session_factory).get(property_id)
    assert state is not None
    return state.status


def _inbound(session_factory: sessionmaker[Session]) -> list[Email]:
    with session_scope(session_factory) as session:
        rows = session.scalars(select(Email).where(Email.direction == EmailDirection.INBOUND))
        result = list(rows)
        session.expunge_all()
        return result


@pytest.mark.parametrize("status", [S.INQUIRY_SENT, S.IN_PROGRESS])
def test_a_reply_is_logged_and_the_property_responded(
    session_factory: sessionmaker[Session], make_inquiry: MakeInquiry, status: PropertyStatus
) -> None:
    property_id, email_id, inquiry_mid = make_inquiry(status)
    mailbox = FakeImap()
    mailbox.add(_reply(inquiry_mid))

    result = _poll(session_factory, mailbox).poll(_ctx())

    assert (result.checked, result.matched, result.recorded) == (1, 1, 1)
    assert _status(session_factory, property_id) is S.RESPONDED
    (reply,) = _inbound(session_factory)
    assert reply.property_id == property_id
    assert reply.status is EmailStatus.RECEIVED
    assert reply.from_address == LISTER
    assert reply.to_address == FROM
    assert reply.message_id == "<r1@example.net>"
    assert reply.body_rendered == "Still available, want to see it?"
    assert reply.sent_at == datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
    assert reply.created_at == NOW
    with session_scope(session_factory) as session:
        event = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.property_id == property_id)
            .order_by(AuditEvent.id.desc())
        ).first()
        assert event is not None
        assert event.to_status is S.RESPONDED
        assert event.actor is Actor.SYSTEM
        assert event.payload == {"email_id": reply.id, "reply_to": email_id}
    # The reply shows in the property's email log.
    log = SqlEmailStore(session_factory).for_property(property_id)
    assert [e.direction for e in log] == [EmailDirection.OUTBOUND, EmailDirection.INBOUND]


def test_a_reply_after_the_property_moved_on_is_logged_without_a_transition(
    session_factory: sessionmaker[Session], make_inquiry: MakeInquiry
) -> None:
    property_id, _, _ = make_inquiry()
    with session_scope(session_factory) as session:
        (inquiry,) = awaiting_reply(session).values()
    # Someone moves it on between the poll's lookup and the reply being logged.
    with session_scope(session_factory) as session:
        session.execute(update(Property).where(Property.id == property_id).values(status=S.TOURING))

    with session_scope(session_factory) as session:
        stored = record_reply_in(
            session,
            inquiry,
            message_id="<late@example.net>",
            from_address=LISTER,
            to_address=FROM,
            subject="Re: Your listing",
            body="See you there",
            received_at=NOW,
            at=NOW,
        )

    assert stored is not None
    assert stored.status is EmailStatus.RECEIVED
    assert _status(session_factory, property_id) is S.TOURING


def test_the_mailbox_is_opened_read_only_and_closed(
    session_factory: sessionmaker[Session], make_inquiry: MakeInquiry
) -> None:
    _, _, inquiry_mid = make_inquiry()
    mailbox = FakeImap()
    mailbox.add(_reply(inquiry_mid))

    _poll(session_factory, mailbox, _settings(folder="Landlord replies")).poll(_ctx())

    assert mailbox.calls[0] == ("login", FROM, PASSWORD)
    assert mailbox.calls[1] == ("select", '"Landlord replies"', True)
    assert mailbox.calls[2] == ("uid", "SEARCH", "SINCE", "05-Oct-2026")
    assert mailbox.calls[-1] == ("logout",)


def test_polling_twice_logs_a_reply_once(
    session_factory: sessionmaker[Session], make_inquiry: MakeInquiry
) -> None:
    property_id, _, inquiry_mid = make_inquiry()
    other_property, _, other_mid = make_inquiry()
    mailbox = FakeImap()
    mailbox.add(_reply(inquiry_mid))
    poll = _poll(session_factory, mailbox)
    poll.poll(_ctx())
    # The same reply quoting a second inquiry still awaiting one: already logged.
    mailbox.add(_reply(other_mid))

    result = poll.poll(_ctx())

    assert (result.matched, result.recorded) == (1, 0)
    assert len(_inbound(session_factory)) == 1
    assert _status(session_factory, property_id) is S.RESPONDED
    assert _status(session_factory, other_property) is S.INQUIRY_SENT


def test_unrelated_mail_is_ignored(
    session_factory: sessionmaker[Session], make_inquiry: MakeInquiry
) -> None:
    property_id, _, _ = make_inquiry()
    mailbox = FakeImap()
    mailbox.add(_reply(None, mid="<newsletter@example.org>"))
    mailbox.add(_reply("<someone-else@example.org>", mid="<thread@example.org>"))

    result = _poll(session_factory, mailbox).poll(_ctx())

    assert (result.checked, result.matched, result.recorded) == (2, 0, 0)
    assert _inbound(session_factory) == []
    assert _status(session_factory, property_id) is S.INQUIRY_SENT
    # Nothing beyond the threading headers was downloaded.
    assert not [c for c in mailbox.calls if c[:2] == ("uid", "FETCH") and c[3] == "(BODY.PEEK[])"]


def test_mail_from_our_own_address_is_not_a_reply(
    session_factory: sessionmaker[Session], make_inquiry: MakeInquiry
) -> None:
    property_id, _, inquiry_mid = make_inquiry()
    mailbox = FakeImap()
    mailbox.add(_reply(inquiry_mid, sender=FROM.upper()))

    result = _poll(session_factory, mailbox).poll(_ctx())

    assert result.matched == 0
    assert _status(session_factory, property_id) is S.INQUIRY_SENT


@pytest.mark.parametrize("status", [S.RESPONDED, S.TOURING, S.INQUIRY_DRAFTED])
def test_properties_not_waiting_for_a_reply_are_not_polled_for(
    session_factory: sessionmaker[Session], make_inquiry: MakeInquiry, status: PropertyStatus
) -> None:
    property_id, _, inquiry_mid = make_inquiry(status)
    mailbox = FakeImap()
    mailbox.add(_reply(inquiry_mid))

    result = _poll(session_factory, mailbox).poll(_ctx())

    assert result.checked == 0
    assert mailbox.calls == []  # nothing awaiting: no connection at all
    assert _status(session_factory, property_id) is status


def test_an_inquiry_sent_by_hand_is_not_polled_for(
    session_factory: sessionmaker[Session], make_inquiry: MakeInquiry
) -> None:
    make_inquiry(to=None)
    mailbox = FakeImap()

    _poll(session_factory, mailbox).poll(_ctx())

    assert mailbox.calls == []


def test_the_search_window_is_capped_by_the_lookback(
    session_factory: sessionmaker[Session], make_inquiry: MakeInquiry
) -> None:
    make_inquiry(sent_at=T0 - timedelta(days=90))
    mailbox = FakeImap()

    _poll(session_factory, mailbox, _settings(lookback_days=7)).poll(_ctx())

    assert ("uid", "SEARCH", "SINCE", "28-Sep-2026") in mailbox.calls


def test_headers_are_fetched_in_chunks(
    session_factory: sessionmaker[Session], make_inquiry: MakeInquiry
) -> None:
    make_inquiry()
    mailbox = FakeImap()
    for n in range(FETCH_CHUNK + 1):
        mailbox.add(_reply(None, mid=f"<m{n}@example.org>"))

    result = _poll(session_factory, mailbox).poll(_ctx())

    assert result.checked == FETCH_CHUNK + 1
    assert len([c for c in mailbox.calls if c[:2] == ("uid", "FETCH")]) == 2


def test_a_reply_gone_before_the_full_fetch_is_skipped(
    session_factory: sessionmaker[Session], make_inquiry: MakeInquiry
) -> None:
    property_id, _, inquiry_mid = make_inquiry()
    mailbox = FakeImap(vanish_on_full_fetch=True)
    mailbox.add(_reply(inquiry_mid))

    result = _poll(session_factory, mailbox).poll(_ctx())

    assert (result.matched, result.recorded) == (1, 0)
    assert _status(session_factory, property_id) is S.INQUIRY_SENT


def test_a_cancelled_poll_records_nothing(
    session_factory: sessionmaker[Session], make_inquiry: MakeInquiry
) -> None:
    property_id, _, inquiry_mid = make_inquiry()
    mailbox = FakeImap()
    mailbox.add(_reply(inquiry_mid))
    ctx = _ctx()
    ctx.cancelled.set()

    result = _poll(session_factory, mailbox).poll(ctx)

    assert result.recorded == 0
    assert _status(session_factory, property_id) is S.INQUIRY_SENT
    assert mailbox.calls[-1] == ("logout",)


def test_a_poll_cancelled_after_matching_records_nothing(
    session_factory: sessionmaker[Session], make_inquiry: MakeInquiry
) -> None:
    property_id, _, inquiry_mid = make_inquiry()
    ctx = _ctx()
    mailbox = FakeImap(cancel_after_headers=ctx.cancelled)
    mailbox.add(_reply(inquiry_mid))

    result = _poll(session_factory, mailbox).poll(ctx)

    assert (result.matched, result.recorded) == (1, 0)
    assert _status(session_factory, property_id) is S.INQUIRY_SENT


def test_a_server_error_propagates_after_logging_out(
    session_factory: sessionmaker[Session], make_inquiry: MakeInquiry
) -> None:
    make_inquiry()
    mailbox = FakeImap(search_status="NO", logout_error=imaplib.IMAP4.abort("gone"))

    with pytest.raises(ImapError, match="IMAP SEARCH failed: NO server says no"):
        _poll(session_factory, mailbox)(_ctx())

    assert mailbox.calls[-1] == ("logout",)


class FakeWorker:
    def __init__(self) -> None:
        self.scheduled: list[tuple[str, Any, timedelta]] = []

    def schedule(self, name: str, handler: Any, *, interval: timedelta) -> None:  # noqa: ANN401
        self.scheduled.append((name, handler, interval))


def test_the_poll_is_scheduled_only_when_enabled(
    session_factory: sessionmaker[Session],
) -> None:
    worker = FakeWorker()

    assert schedule_imap_poll(worker, session_factory, Settings()) is False
    assert worker.scheduled == []

    assert schedule_imap_poll(worker, session_factory, _settings(poll_interval_seconds=60))
    ((name, handler, interval),) = worker.scheduled
    assert name == IMAP_POLL
    assert isinstance(handler, ImapReplyPoll)
    assert interval == timedelta(seconds=60)
