"""Reply detection: poll an IMAP mailbox for answers to inquiries (``IMAP_*`` settings).

Off unless ``IMAP_ENABLED`` is set. :func:`schedule_imap_poll` then runs
:class:`ImapReplyPoll` in the worker every ``IMAP_POLL_INTERVAL_SECONDS``. One poll:

1. asks the store which inquiries are still waiting for a reply
   (:func:`furnest.db.email_store.awaiting_reply`). None: it doesn't connect at all;
2. opens the folder read-only, so nothing is marked as read, moved or deleted, and
   searches it for mail since the oldest of those inquiries went out (at most
   ``IMAP_LOOKBACK_DAYS`` back);
3. fetches only the threading headers (Message-ID, In-Reply-To, References) of
   that mail, and picks the messages that quote an inquiry's Message-ID;
4. fetches each of those in full and logs it with
   :func:`furnest.db.email_store.record_reply_in`, which also moves the property to
   ``responded`` (``system`` actor).

The mailbox may hold personal mail, so anything that doesn't answer an inquiry is
never downloaded past its threading headers, stored or logged; the poll only
counts it. A message sent from furnest's own address (a follow-up written from
the same mailbox) isn't a reply either. Logging a reply is idempotent: its
Message-ID is unique in the email log, so the next poll finding it again changes
nothing.

Connection and protocol errors aren't caught: the worker reports the failed run,
and the next interval tries again.
"""

from __future__ import annotations

import hashlib
import html
import imaplib
import re
import ssl
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Protocol

import nh3
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import ImapSettings, Settings, SmtpSecurity
from furnest.db.base import utcnow
from furnest.db.email_store import AwaitingReply, awaiting_reply, record_reply_in
from furnest.db.session import session_scope
from furnest.logging import get_logger

if TYPE_CHECKING:
    from furnest.worker.execution import Handler, JobContext

__all__ = [
    "IMAP_POLL",
    "ImapClient",
    "ImapError",
    "ImapReplyPoll",
    "InboundMessage",
    "open_imap",
    "parse_reply",
    "schedule_imap_poll",
]

log = get_logger(__name__)

# The scheduled job's name (and its lease, ``scheduled:email.imap_poll``).
IMAP_POLL = "email.imap_poll"

# Matches the column sizes in ``emails``.
MAX_MESSAGE_ID_LENGTH = 255
MAX_ADDRESS_LENGTH = 320
MAX_BODY_LENGTH = 100_000

# UIDs per FETCH command, so a large search result stays a few modest round trips.
FETCH_CHUNK = 200

_THREAD_HEADERS = "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES FROM)])"
_FULL_MESSAGE = "(BODY.PEEK[])"
_MSG_ID = re.compile(r"<[^<>\s]+>")
_UID = re.compile(rb"\bUID (\d+)")
# IMAP dates are English whatever the locale (RFC 3501 date-month).
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_NO_DOMAIN = "furnest.invalid"


class ImapError(RuntimeError):
    """The server answered a command with something other than OK."""


class ImapClient(Protocol):
    """The part of :class:`imaplib.IMAP4` the poll uses (a fake in tests)."""

    def login(self, user: str, password: str) -> tuple[str, list[Any]]: ...

    def select(self, mailbox: str = ..., readonly: bool = ...) -> tuple[str, list[Any]]: ...

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]: ...

    def logout(self) -> tuple[str, list[Any]]: ...


def open_imap(settings: ImapSettings) -> ImapClient:
    """Connect (and, for ``starttls``, upgrade) without logging in."""
    if settings.host is None:
        msg = "IMAP_HOST is not set"
        raise ValueError(msg)
    timeout = settings.timeout_seconds
    if settings.security is SmtpSecurity.SSL:
        return imaplib.IMAP4_SSL(
            settings.host, settings.port, ssl_context=ssl.create_default_context(), timeout=timeout
        )
    client = imaplib.IMAP4(settings.host, settings.port, timeout=timeout)
    if settings.security is SmtpSecurity.STARTTLS:
        client.starttls(ssl_context=ssl.create_default_context())
    return client


# --- Reading a message ---------------------------------------------------------


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    # Message-IDs this one answers: In-Reply-To first, then References.
    references: tuple[str, ...]
    from_address: str | None
    to_address: str | None
    subject: str
    body: str
    # The Date header, or None when it's missing or unreadable.
    date: datetime | None


def references(message: EmailMessage) -> tuple[str, ...]:
    """The Message-IDs ``message`` quotes in In-Reply-To and References, in that order."""
    found: list[str] = []
    for header in ("In-Reply-To", "References"):
        for value in message.get_all(header, []):
            found.extend(m for m in _MSG_ID.findall(str(value)) if m not in found)
    return tuple(found)


def parse_reply(raw: bytes) -> InboundMessage:
    """What the email log keeps of a received message."""
    message = message_from_bytes(raw, policy=policy.default)
    assert isinstance(message, EmailMessage)  # noqa: S101 - policy.default makes these
    return InboundMessage(
        message_id=_own_message_id(message, raw),
        references=references(message),
        from_address=_first_address(message, "From"),
        to_address=_first_address(message, "To"),
        subject=header(message, "Subject"),
        body=_body(message)[:MAX_BODY_LENGTH],
        date=_date(message),
    )


def header(message: EmailMessage, name: str) -> str:
    try:
        value = message[name]
    except (ValueError, IndexError):  # a header the parser can't make sense of
        return ""
    return "" if value is None else " ".join(str(value).split())


def _own_message_id(message: EmailMessage, raw: bytes) -> str:
    found = _MSG_ID.search(header(message, "Message-ID"))
    if found and len(found.group()) <= MAX_MESSAGE_ID_LENGTH:
        return found.group()
    # Without a usable one, the content stands in, so the same message maps to the
    # same id on every poll.
    return f"<{hashlib.sha256(raw).hexdigest()}@{_NO_DOMAIN}>"


def _first_address(message: EmailMessage, name: str) -> str | None:
    for _, address in getaddresses([header(message, name)]):
        if "@" in address:
            return address[:MAX_ADDRESS_LENGTH]
    return None


def _date(message: EmailMessage) -> datetime | None:
    value = header(message, "Date")
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    # "-0000" means UTC with no zone known; parsedate_to_datetime leaves it naive.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _body(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content: str
    try:
        content = part.get_content()  # a str: get_body only returns text parts
    except (LookupError, ValueError):  # an unknown or wrong charset
        payload = part.get_payload(decode=True)
        content = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
    if part.get_content_subtype() == "html":
        content = html_to_text(content)
    return content.strip()


def html_to_text(markup: str) -> str:
    """Plain text from an HTML-only reply: tags (and scripts, styles) dropped."""
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", markup)
    text = html.unescape(nh3.clean(markup, tags=set()))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


# --- Polling -------------------------------------------------------------------


def imap_date(day: datetime) -> str:
    """``day`` as an IMAP SEARCH date, e.g. ``06-Oct-2026``."""
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def _ok(response: tuple[str, list[Any]], command: str) -> list[Any]:
    status, data = response
    if status != "OK":
        detail = b" ".join(d for d in data if isinstance(d, bytes)).decode(errors="replace")
        msg = f"IMAP {command} failed: {status} {detail}".strip()
        raise ImapError(msg)
    return data


def fetched(data: Iterable[Any]) -> Iterator[tuple[str, bytes]]:
    """(uid, content) for each message in a UID FETCH response."""
    for item in data:
        # Each message comes as (b"<seq> (UID <uid> BODY[...] {<n>}", content); the
        # bare b")" between them closes it.
        if isinstance(item, tuple) and len(item) >= 2:
            found = _UID.search(item[0])
            if found and isinstance(item[1], bytes):
                yield found.group(1).decode(), item[1]


def _chunks(uids: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(uids), size):
        yield uids[start : start + size]


@dataclass
class PollResult:
    checked: int = 0  # messages whose headers were read
    matched: int = 0  # replies to an inquiry waiting for one
    recorded: int = 0  # replies logged for the first time


class ImapReplyPoll:
    """The scheduled job. ``connect`` swaps the IMAP connection in tests."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        connect: Callable[[ImapSettings], ImapClient] = open_imap,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._factory = session_factory
        self._imap = settings.imap
        # Mail from these is ours, never a landlord's reply.
        own = {settings.imap.username, settings.smtp.from_address}
        self._own_addresses = frozenset(str(a).lower() for a in own if a)
        self._connect = connect
        self._clock = clock

    def __call__(self, ctx: JobContext) -> None:
        self.poll(ctx)

    def poll(self, ctx: JobContext) -> PollResult:
        result = PollResult()
        with session_scope(self._factory) as session:
            awaiting = awaiting_reply(session)
        if not awaiting:
            log.info("imap_poll_skipped", reason="no inquiry awaiting a reply")
            return result
        now = self._clock()
        oldest = min(inquiry.sent_at for inquiry in awaiting.values())
        since = max(oldest, now - timedelta(days=self._imap.lookback_days)) - timedelta(days=1)
        client = self._connect(self._imap)
        try:
            self._login(client)
            matches = self._find_replies(client, since, awaiting, result, ctx)
            for uid, inquiry in matches:
                if ctx.cancelled.is_set():
                    break
                if self._record(client, uid, inquiry):
                    result.recorded += 1
        finally:
            try:
                client.logout()
            except (OSError, imaplib.IMAP4.error):
                log.warning("imap_logout_failed")
        log.info(
            "imap_poll_done",
            awaiting=len(awaiting),
            checked=result.checked,
            matched=result.matched,
            recorded=result.recorded,
        )
        return result

    def _login(self, client: ImapClient) -> None:
        imap = self._imap
        assert imap.username is not None  # noqa: S101 - ImapSettings checks it when enabled
        password = imap.password.get_secret_value() if imap.password else ""
        _ok(client.login(imap.username, password), "LOGIN")
        folder = imap.folder
        if " " in folder and not folder.startswith('"'):
            folder = f'"{folder}"'
        # Read-only: the poll never changes the mailbox, not even \Seen flags.
        _ok(client.select(folder, readonly=True), "SELECT")

    def _find_replies(
        self,
        client: ImapClient,
        since: datetime,
        awaiting: dict[str, AwaitingReply],
        result: PollResult,
        ctx: JobContext,
    ) -> list[tuple[str, AwaitingReply]]:
        data = _ok(client.uid("SEARCH", "SINCE", imap_date(since)), "SEARCH")
        uids = b" ".join(d for d in data if isinstance(d, bytes)).decode().split()
        matches: list[tuple[str, AwaitingReply]] = []
        for chunk in _chunks(uids, FETCH_CHUNK):
            if ctx.cancelled.is_set():
                break
            response = _ok(client.uid("FETCH", ",".join(chunk), _THREAD_HEADERS), "FETCH")
            for uid, headers in fetched(response):
                result.checked += 1
                inquiry = self._answers(headers, awaiting)
                if inquiry is not None:
                    result.matched += 1
                    matches.append((uid, inquiry))
        return matches

    def _answers(self, headers: bytes, awaiting: dict[str, AwaitingReply]) -> AwaitingReply | None:
        message = message_from_bytes(headers, policy=policy.default)
        assert isinstance(message, EmailMessage)  # noqa: S101 - policy.default makes these
        sender = parseaddr(header(message, "From"))[1].lower()
        if sender in self._own_addresses:
            return None
        for reference in references(message):
            if reference in awaiting:
                return awaiting[reference]
        return None

    def _record(self, client: ImapClient, uid: str, inquiry: AwaitingReply) -> bool:
        raw = next(
            (c for _, c in fetched(_ok(client.uid("FETCH", uid, _FULL_MESSAGE), "FETCH"))), None
        )
        if raw is None:  # gone since the header fetch
            return False
        reply = parse_reply(raw)
        now = self._clock()
        with session_scope(self._factory) as session:
            stored = record_reply_in(
                session,
                inquiry,
                message_id=reply.message_id,
                from_address=reply.from_address,
                to_address=reply.to_address,
                subject=reply.subject,
                body=reply.body,
                received_at=reply.date or now,
                at=now,
            )
        if stored is None:
            return False
        log.info(
            "imap_reply_recorded",
            email_id=stored.id,
            property_id=inquiry.property_id,
            reply_to=inquiry.email_id,
            sender_domain=(reply.from_address or "").rpartition("@")[2] or None,
        )
        return True


class Scheduler(Protocol):
    """The part of :class:`furnest.worker.runtime.Worker` this module needs."""

    def schedule(self, name: str, handler: Handler, *, interval: timedelta) -> None: ...


def schedule_imap_poll(
    worker: Scheduler, session_factory: sessionmaker[Session], settings: Settings
) -> bool:
    """Schedule the poll if ``IMAP_ENABLED`` is set; returns whether it did."""
    if not settings.imap.enabled:
        return False
    interval = timedelta(seconds=settings.imap.poll_interval_seconds)
    worker.schedule(IMAP_POLL, ImapReplyPoll(session_factory, settings=settings), interval=interval)
    log.info("imap_poll_scheduled", interval_seconds=settings.imap.poll_interval_seconds)
    return True
