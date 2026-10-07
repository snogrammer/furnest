"""The email log (``emails``): queueing outbound mail and recording how sending went.

:func:`queue_email` is how anything sends an email. It runs in the caller's
transaction, inserts the ``queued`` row with the final text and a fresh Message-ID,
and queues an :data:`EMAIL_SEND` job for it, so the email exists and gets sent if
and only if the caller's change commits. The worker's
:class:`furnest.email.send.EmailSend` then reports back through
:class:`SqlEmailStore`.

An inquiry (:mod:`furnest.email.inquiry`) starts as a :func:`draft_email`: the same
row, but ``draft`` and with no job, so nothing goes out yet. :func:`send_draft_in`
saves the reviewed text and queues it like :func:`queue_email` would, and
:func:`discard_draft_in` throws it away. A ``failed`` inquiry can be sent again the
same way. :meth:`SqlEmailStore.review_queue` lists what waits on a person: drafts
and failed inquiries whose property is still ``inquiry_drafted``.

A draft needs no SMTP: without a From address it is logged with ``from_address``
NULL, and :func:`send_draft_in` fills it in (and makes a fresh Message-ID) once SMTP
is set up. A listing with no email address is contacted by hand through its source
instead: :func:`mark_sent_manually_in` records that the draft went out that way.

When an inquiry goes out, :meth:`SqlEmailStore.mark_sent` moves its property from
``inquiry_drafted`` to ``inquiry_sent`` in the same transaction, so the status
only says "sent" once the server has accepted the message (or, for one sent by
hand, once someone said so).

Replies come back through the IMAP poller (:mod:`furnest.email.imap`).
:func:`awaiting_reply` lists the inquiries it looks for: emailed ones (not sent by
hand, which can't be answered by email) whose property is still ``inquiry_sent`` or
``in_progress``. :func:`record_reply_in` logs a reply as an ``inbound`` row with
status ``received`` and moves the property to ``responded``, both or neither. The
reply's own Message-ID is unique in the log, so finding it again changes nothing.
"""

from dataclasses import dataclass
from datetime import datetime
from email.utils import make_msgid

from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import SmtpSettings
from furnest.db.base import utcnow
from furnest.db.models import Email, EmailDirection, EmailStatus, Property
from furnest.db.property_store import primary_listing, transition_in
from furnest.db.session import session_scope
from furnest.domain import Actor, PropertyStatus
from furnest.worker import queue

# Queue job kind; the payload is ``{"email_id": <emails.id>}``.
EMAIL_SEND = "email.send"

# Long enough for an SMTP server's reply and a traceback summary.
MAX_ERROR_LENGTH = 2000

# The Message-ID domain of a draft made with no From address. It is replaced before
# the draft is ever emailed (send_draft_in), so it never leaves furnest.
_NO_DOMAIN = "furnest.invalid"


class SmtpNotConfiguredError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("SMTP is not configured (set SMTP_HOST and SMTP_FROM_ADDRESS)")


class EmailNotFoundError(LookupError):
    def __init__(self, email_id: int) -> None:
        super().__init__(f"email {email_id} not found")
        self.email_id = email_id


class NotSendableError(ValueError):
    """The email isn't a draft or a failed inquiry, so there is nothing to (re)send."""

    def __init__(self, email_id: int, status: EmailStatus) -> None:
        super().__init__(f"email {email_id} is {status}")
        self.email_id = email_id
        self.status = status


# What can be sent (again) from the review queue.
SENDABLE = frozenset({EmailStatus.DRAFT, EmailStatus.FAILED})

# Property states in which a reply to the inquiry means "responded".
AWAITING_REPLY = frozenset({PropertyStatus.INQUIRY_SENT, PropertyStatus.IN_PROGRESS})


@dataclass(frozen=True)
class StoredEmail:
    id: int
    property_id: int | None
    direction: EmailDirection
    to_address: str | None
    from_address: str | None
    subject: str
    body_rendered: str
    status: EmailStatus
    error: str | None
    attempts: int
    message_id: str
    created_at: datetime
    sent_at: datetime | None


def _stored(row: Email) -> StoredEmail:
    return StoredEmail(
        id=row.id,
        property_id=row.property_id,
        direction=row.direction,
        to_address=row.to_address,
        from_address=row.from_address,
        subject=row.subject,
        body_rendered=row.body_rendered,
        status=row.status,
        error=row.error,
        attempts=row.attempts,
        message_id=row.message_id,
        created_at=row.created_at,
        sent_at=row.sent_at,
    )


@dataclass(frozen=True)
class ReviewItem:
    """An inquiry in the review queue, with what the queue shows about its property."""

    email: StoredEmail
    # From the property's primary listing: where to send it by hand, if need be.
    title: str | None
    source: str | None
    listing_url: str | None


@dataclass(frozen=True)
class AwaitingReply:
    """An emailed inquiry whose property is still waiting to hear back."""

    email_id: int
    property_id: int
    sent_at: datetime


def _message_id(from_address: str | None) -> str:
    domain = from_address.rpartition("@")[2] if from_address else _NO_DOMAIN
    return make_msgid(domain=domain)


def _outbound(
    session: Session,
    status: EmailStatus,
    *,
    from_address: str | None,
    to: str | None,
    subject: str,
    body: str,
    property_id: int | None,
) -> Email:
    row = Email(
        property_id=property_id,
        direction=EmailDirection.OUTBOUND,
        to_address=to,
        from_address=from_address,
        subject=subject,
        body_rendered=body,
        status=status,
        attempts=0,
        message_id=_message_id(from_address),
    )
    session.add(row)
    session.flush()
    return row


def queue_email(
    session: Session,
    smtp: SmtpSettings,
    *,
    to: str,
    subject: str,
    body: str,
    property_id: int | None = None,
) -> StoredEmail:
    """Log an outbound email as ``queued`` and queue the job that sends it.

    Raises :class:`SmtpNotConfiguredError` when there is no From address to
    record (and no domain for the Message-ID).
    """
    if smtp.from_address is None:
        raise SmtpNotConfiguredError
    row = _outbound(
        session,
        EmailStatus.QUEUED,
        from_address=str(smtp.from_address),
        to=to,
        subject=subject,
        body=body,
        property_id=property_id,
    )
    queue.enqueue(session, EMAIL_SEND, {"email_id": row.id})
    return _stored(row)


def draft_email(
    session: Session,
    smtp: SmtpSettings,
    *,
    to: str | None,
    subject: str,
    body: str,
    property_id: int,
) -> StoredEmail:
    """Log an outbound email as a ``draft``: nothing is sent until :func:`send_draft_in`.

    ``to`` may be ``None`` when the listing has no email address. SMTP needn't be
    set up: without a From address the draft can still be marked as sent by hand
    (:func:`mark_sent_manually_in`), and :func:`send_draft_in` fills one in.
    """
    row = _outbound(
        session,
        EmailStatus.DRAFT,
        from_address=None if smtp.from_address is None else str(smtp.from_address),
        to=to,
        subject=subject,
        body=body,
        property_id=property_id,
    )
    return _stored(row)


def _sendable(session: Session, email_id: int) -> Email:
    row = session.get(Email, email_id)
    if row is None or row.direction is not EmailDirection.OUTBOUND:
        raise EmailNotFoundError(email_id)
    if row.status not in SENDABLE:
        raise NotSendableError(email_id, row.status)
    return row


def send_draft_in(
    session: Session,
    smtp: SmtpSettings,
    email_id: int,
    *,
    to: str,
    subject: str,
    body: str,
) -> StoredEmail:
    """Save the reviewed text of a draft (or failed email) and queue it to be sent.

    The text is taken as given; :func:`furnest.email.inquiry.review_edit` checks
    what a person typed. The Message-ID stays the one made with the draft, unless
    the draft was made without a From address: it gets the configured one and a
    Message-ID in its domain. Raises :class:`SmtpNotConfiguredError` when SMTP isn't
    set up, since nothing could send it.
    """
    if not smtp.configured or smtp.from_address is None:
        raise SmtpNotConfiguredError
    row = _sendable(session, email_id)
    if row.from_address is None:
        row.from_address = str(smtp.from_address)
        row.message_id = _message_id(row.from_address)
    row.to_address = to
    row.subject = subject
    row.body_rendered = body
    row.status = EmailStatus.QUEUED
    row.error = None
    row.attempts = 0
    session.flush()
    queue.enqueue(session, EMAIL_SEND, {"email_id": row.id})
    return _stored(row)


def mark_sent_manually_in(
    session: Session, email_id: int, *, subject: str, body: str, at: datetime
) -> StoredEmail:
    """Record that a draft (or failed email) was sent by hand, through the source.

    The row keeps the text as sent, becomes ``sent`` without being queued, and its
    property moves to ``inquiry_sent`` with the ``dashboard`` actor. The text is
    taken as given; :func:`furnest.email.inquiry.manual_edit` checks what was typed.
    """
    row = _sendable(session, email_id)
    row.subject = subject
    row.body_rendered = body
    row.status = EmailStatus.SENT
    row.sent_at = at
    row.error = None
    session.flush()
    _property_sent(session, row, at=at, actor=Actor.DASHBOARD, detail="sent manually", manual=True)
    return _stored(row)


def _property_sent(
    session: Session,
    row: Email,
    *,
    at: datetime,
    actor: Actor,
    detail: str,
    manual: bool = False,
) -> None:
    """Move the inquiry's property from ``inquiry_drafted`` to ``inquiry_sent``."""
    if row.property_id is None:
        return
    prop = session.get(Property, row.property_id)
    if prop is not None and prop.status is PropertyStatus.INQUIRY_DRAFTED:
        payload: dict[str, object] = {"email_id": row.id}
        if manual:
            payload["manual"] = True
        transition_in(
            session,
            prop.id,
            PropertyStatus.INQUIRY_SENT,
            actor=actor,
            at=at,
            detail=detail,
            payload=payload,
        )


def discard_draft_in(session: Session, email_id: int) -> StoredEmail:
    """Throw away a draft (the row goes) or give up on a failed email (the row stays)."""
    row = _sendable(session, email_id)
    stored = _stored(row)
    if row.status is EmailStatus.DRAFT:
        session.delete(row)
        session.flush()
    return stored


def awaiting_reply(session: Session) -> dict[str, AwaitingReply]:
    """The inquiries a reply would answer, by their Message-ID."""
    statement = (
        select(Email)
        .join(Property, Property.id == Email.property_id)
        .where(
            Email.direction == EmailDirection.OUTBOUND,
            Email.status == EmailStatus.SENT,
            Email.to_address.is_not(None),
            Email.from_address.is_not(None),
            Email.sent_at.is_not(None),
            Property.status.in_(AWAITING_REPLY),
        )
    )
    awaiting = {}
    for row in session.scalars(statement):
        assert row.property_id is not None  # noqa: S101 - the join guarantees it
        assert row.sent_at is not None  # noqa: S101 - so does the where clause
        awaiting[row.message_id] = AwaitingReply(row.id, row.property_id, row.sent_at)
    return awaiting


def record_reply_in(
    session: Session,
    inquiry: AwaitingReply,
    *,
    message_id: str,
    from_address: str | None,
    to_address: str | None,
    subject: str,
    body: str,
    received_at: datetime,
    at: datetime,
) -> StoredEmail | None:
    """Log a reply to ``inquiry`` and move its property to ``responded``.

    Returns ``None``, changing nothing, when a row with ``message_id`` is already
    logged. The property only moves if it is still waiting for a reply; the reply
    is logged either way.
    """
    if session.scalar(select(Email.id).where(Email.message_id == message_id)) is not None:
        return None
    row = Email(
        property_id=inquiry.property_id,
        direction=EmailDirection.INBOUND,
        to_address=to_address,
        from_address=from_address,
        subject=subject,
        body_rendered=body,
        status=EmailStatus.RECEIVED,
        attempts=0,
        message_id=message_id,
        created_at=at,
        sent_at=received_at,
    )
    session.add(row)
    session.flush()
    prop = session.get(Property, inquiry.property_id)
    if prop is not None and prop.status in AWAITING_REPLY:
        transition_in(
            session,
            prop.id,
            PropertyStatus.RESPONDED,
            actor=Actor.SYSTEM,
            at=at,
            detail="reply received",
            payload={"email_id": row.id, "reply_to": inquiry.email_id},
        )
    return _stored(row)


def _awaiting_review() -> ColumnElement[bool]:
    # A property that moved on (archived, or unavailable for now) takes its draft
    # out of the queue with it.
    return and_(
        Email.direction == EmailDirection.OUTBOUND,
        Email.status.in_(SENDABLE),
        Property.status == PropertyStatus.INQUIRY_DRAFTED,
    )


class SqlEmailStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, email_id: int) -> StoredEmail | None:
        with session_scope(self._session_factory) as session:
            row = session.get(Email, email_id)
            return None if row is None else _stored(row)

    def for_property(self, property_id: int) -> list[StoredEmail]:
        """A property's emails, oldest first."""
        statement = (
            select(Email)
            .where(Email.property_id == property_id)
            .order_by(Email.created_at, Email.id)
        )
        with session_scope(self._session_factory) as session:
            return [_stored(row) for row in session.scalars(statement)]

    def review_queue(self) -> list[ReviewItem]:
        """Inquiries waiting on a person, oldest first."""
        statement = (
            select(Email)
            .join(Property, Property.id == Email.property_id)
            .where(_awaiting_review())
            .order_by(Email.created_at, Email.id)
        )
        with session_scope(self._session_factory) as session:
            items = []
            for row in session.scalars(statement):
                listing = None
                if row.property_id is not None:
                    listing = primary_listing(session, row.property_id)
                items.append(
                    ReviewItem(
                        email=_stored(row),
                        title=listing.title if listing else None,
                        source=listing.source if listing else None,
                        listing_url=listing.url if listing else None,
                    )
                )
            return items

    def mark_sent(self, email_id: int, *, now: datetime | None = None) -> None:
        """Record the send and, for an inquiry, move its property to ``inquiry_sent``."""
        at = now or utcnow()
        with session_scope(self._session_factory) as session:
            row = session.get(Email, email_id)
            if row is None:
                return
            row.attempts += 1
            row.status = EmailStatus.SENT
            row.sent_at = at
            row.error = None
            _property_sent(session, row, at=at, actor=Actor.SYSTEM, detail="email sent")

    def record_failure(self, email_id: int, error: str, *, final: bool) -> None:
        """Count a failed attempt. ``final`` means it won't be retried: the row is ``failed``."""
        with session_scope(self._session_factory) as session:
            row = session.get(Email, email_id)
            if row is None:
                return
            row.attempts += 1
            row.error = error[-MAX_ERROR_LENGTH:]
            if final:
                row.status = EmailStatus.FAILED
