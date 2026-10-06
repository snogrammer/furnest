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

When an inquiry goes out, :meth:`SqlEmailStore.mark_sent` moves its property from
``inquiry_drafted`` to ``inquiry_sent`` in the same transaction, so the status
only says "sent" once the server has accepted the message.
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


@dataclass(frozen=True)
class StoredEmail:
    id: int
    property_id: int | None
    direction: EmailDirection
    to_address: str | None
    from_address: str
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
    title: str | None  # the property's primary listing's title


def _outbound(
    session: Session,
    smtp: SmtpSettings,
    status: EmailStatus,
    *,
    to: str | None,
    subject: str,
    body: str,
    property_id: int | None,
) -> Email:
    if smtp.from_address is None:
        raise SmtpNotConfiguredError
    from_address = str(smtp.from_address)
    row = Email(
        property_id=property_id,
        direction=EmailDirection.OUTBOUND,
        to_address=to,
        from_address=from_address,
        subject=subject,
        body_rendered=body,
        status=status,
        attempts=0,
        message_id=make_msgid(domain=from_address.rpartition("@")[2]),
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
    row = _outbound(
        session,
        smtp,
        EmailStatus.QUEUED,
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

    ``to`` may be ``None`` when the listing has no email address. Raises
    :class:`SmtpNotConfiguredError` like :func:`queue_email`.
    """
    row = _outbound(
        session,
        smtp,
        EmailStatus.DRAFT,
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
    session: Session, email_id: int, *, to: str, subject: str, body: str
) -> StoredEmail:
    """Save the reviewed text of a draft (or failed email) and queue it to be sent.

    The text is taken as given; :func:`furnest.email.inquiry.review_edit` checks
    what a person typed. The Message-ID stays the one made with the draft.
    """
    row = _sendable(session, email_id)
    row.to_address = to
    row.subject = subject
    row.body_rendered = body
    row.status = EmailStatus.QUEUED
    row.error = None
    row.attempts = 0
    session.flush()
    queue.enqueue(session, EMAIL_SEND, {"email_id": row.id})
    return _stored(row)


def discard_draft_in(session: Session, email_id: int) -> StoredEmail:
    """Throw away a draft (the row goes) or give up on a failed email (the row stays)."""
    row = _sendable(session, email_id)
    stored = _stored(row)
    if row.status is EmailStatus.DRAFT:
        session.delete(row)
        session.flush()
    return stored


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
            if row.property_id is None:
                return
            prop = session.get(Property, row.property_id)
            if prop is not None and prop.status is PropertyStatus.INQUIRY_DRAFTED:
                transition_in(
                    session,
                    prop.id,
                    PropertyStatus.INQUIRY_SENT,
                    actor=Actor.SYSTEM,
                    at=at,
                    detail="email sent",
                    payload={"email_id": row.id},
                )

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
