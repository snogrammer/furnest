"""The email log (``emails``): queueing outbound mail and recording how sending went.

:func:`queue_email` is how anything sends an email. It runs in the caller's
transaction, inserts the ``queued`` row with the final text and a fresh Message-ID,
and queues an :data:`EMAIL_SEND` job for it, so the email exists and gets sent if
and only if the caller's change commits. The worker's
:class:`furnest.email.send.EmailSend` then reports back through
:class:`SqlEmailStore`.
"""

from dataclasses import dataclass
from datetime import datetime
from email.utils import make_msgid

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import SmtpSettings
from furnest.db.base import utcnow
from furnest.db.models import Email, EmailDirection, EmailStatus
from furnest.db.session import session_scope
from furnest.worker import queue

# Queue job kind; the payload is ``{"email_id": <emails.id>}``.
EMAIL_SEND = "email.send"

# Long enough for an SMTP server's reply and a traceback summary.
MAX_ERROR_LENGTH = 2000


class SmtpNotConfiguredError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("SMTP is not configured (set SMTP_HOST and SMTP_FROM_ADDRESS)")


@dataclass(frozen=True)
class StoredEmail:
    id: int
    property_id: int | None
    direction: EmailDirection
    to_address: str
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
    from_address = str(smtp.from_address)
    row = Email(
        property_id=property_id,
        direction=EmailDirection.OUTBOUND,
        to_address=to,
        from_address=from_address,
        subject=subject,
        body_rendered=body,
        status=EmailStatus.QUEUED,
        attempts=0,
        message_id=make_msgid(domain=from_address.rpartition("@")[2]),
    )
    session.add(row)
    session.flush()
    queue.enqueue(session, EMAIL_SEND, {"email_id": row.id})
    return _stored(row)


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

    def mark_sent(self, email_id: int, *, now: datetime | None = None) -> None:
        with session_scope(self._session_factory) as session:
            row = session.get(Email, email_id)
            if row is None:
                return
            row.attempts += 1
            row.status = EmailStatus.SENT
            row.sent_at = now or utcnow()
            row.error = None

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
