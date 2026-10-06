"""The :data:`~furnest.db.email_store.EMAIL_SEND` job: send one logged email.

The row from :func:`furnest.db.email_store.queue_email` already has the text and
the Message-ID; this job only delivers it and records the outcome.

* Already ``sent`` (a retried or duplicate job): nothing to do.
* SMTP not configured: the attempt is recorded and the job raises, so the queue
  retries. Configuring SMTP and restarting the worker lets it go out.
* The server refuses it permanently (5xx): the row is ``failed`` and the job ends
  without a retry.
* Anything else (4xx, connection errors, timeouts): the attempt is recorded and the
  job raises, so the queue retries with backoff. The row becomes ``failed`` when the
  last attempt fails.

Delivery is at least once. If the worker dies after the server accepts the message
but before the row is marked ``sent``, the retry sends it again, with the same
Message-ID, which lets mail clients spot the duplicate.
"""

from __future__ import annotations

from collections.abc import Callable
from email.message import EmailMessage
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.orm import Session, sessionmaker

from furnest.config import Settings, SmtpSettings
from furnest.db.email_store import EMAIL_SEND, SmtpNotConfiguredError, SqlEmailStore
from furnest.db.models import EmailStatus
from furnest.email.smtp import SmtpSender, build_message, is_permanent
from furnest.logging import get_logger

if TYPE_CHECKING:
    from furnest.worker.execution import JobContext

__all__ = ["EMAIL_SEND", "EmailSend", "Sender"]

log = get_logger(__name__)


class Sender(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class EmailSend:
    """The :data:`EMAIL_SEND` handler. ``sender_factory`` swaps the SMTP transport in tests."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        sender_factory: Callable[[SmtpSettings], Sender] = SmtpSender,
    ) -> None:
        self._store = SqlEmailStore(session_factory)
        self._smtp = settings.smtp
        self._sender_factory = sender_factory

    def __call__(self, ctx: JobContext) -> None:
        email_id = int(ctx.payload["email_id"])
        email = self._store.get(email_id)
        if email is None or email.status is EmailStatus.SENT:
            log.info("email_send_skipped", email_id=email_id, reason="missing or already sent")
            return
        final = ctx.attempt >= ctx.max_attempts
        if not self._smtp.configured:
            error = SmtpNotConfiguredError()
            self._store.record_failure(email_id, str(error), final=final)
            raise error
        if ctx.cancelled.is_set():
            return
        message = build_message(
            from_address=email.from_address,
            from_name=self._smtp.from_name,
            to=email.to_address,
            subject=email.subject,
            body=email.body_rendered,
            message_id=email.message_id,
        )
        recipient_domain = email.to_address.rpartition("@")[2]
        try:
            self._sender_factory(self._smtp).send(message)
        except (OSError, ValueError) as exc:
            # smtplib.SMTPException is an OSError subclass, as are socket errors/timeouts.
            permanent = is_permanent(exc)
            self._store.record_failure(
                email_id, f"{type(exc).__name__}: {exc}", final=permanent or final
            )
            log.warning(
                "email_send_failed",
                email_id=email_id,
                recipient_domain=recipient_domain,
                permanent=permanent,
                attempt=ctx.attempt,
                error_type=type(exc).__name__,
            )
            if permanent:
                return
            raise
        self._store.mark_sent(email_id)
        log.info("email_sent", email_id=email_id, recipient_domain=recipient_domain)
