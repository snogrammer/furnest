"""Inquiries: the email asking a lister about a property, drafted and then sent.

Asking starts with :func:`request_inquiry_in`, which the Discord Inquire button and
the dashboard's Inquire button both call. It approves a ``suggested`` property and
queues an :data:`INQUIRY_CREATE` job with ``property_id``, ``actor`` and
``actor_detail`` in the payload, unless one is already waiting.

:class:`InquiryCreate` runs that job. In one transaction it renders the ``default``
template (:mod:`furnest.email.templates`) for the property's primary listing, logs
the result as a ``draft`` email (:func:`~furnest.db.email_store.draft_email`) and
moves the property from ``approved`` to ``inquiry_drafted``. Then ``INQUIRY_MODE``
decides:

* ``review``: the draft waits in the dashboard's review queue (``/inquiries``),
  where it can be edited and sent, or discarded.
* ``auto``: the draft is sent straight away, unless the listing has no email
  address or SMTP isn't set up, in which case it waits for review all the same.

Either way the property becomes ``inquiry_sent`` only once the server accepts the
message (:meth:`~furnest.db.email_store.SqlEmailStore.mark_sent`). An inquiry that
fails for good goes back to the review queue with its error.

**Manual contact.** Some listings give no email address: the source only relays
messages through its own site (Furnished Finder does this), and the property is
flagged ``manual_contact_required``. Drafting doesn't need SMTP, so such a property
gets a draft all the same. The detail page and the review queue show it ready to
copy, with a link to the listing, and once it has been sent there, **Mark as sent**
(:func:`manual_edit`, :func:`~furnest.db.email_store.mark_sent_manually_in`)
records the text that went out and moves the property to ``inquiry_sent``.

The job is idempotent: it does nothing for a property that is no longer
``approved`` (a draft already exists, or someone denied it in the meantime).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import INQUIRY_MODE, InquiryMode, RuntimeSettings, Settings
from furnest.db.base import utcnow
from furnest.db.criteria_store import CriteriaStore
from furnest.db.email_store import draft_email, send_draft_in
from furnest.db.email_template_store import SqlEmailTemplateStore
from furnest.db.grouping import resolve
from furnest.db.listing_store import stored_listing
from furnest.db.models import Property
from furnest.db.property_store import primary_listing, transition_in
from furnest.db.session import session_scope
from furnest.db.settings_store import SqlSettingsStore
from furnest.domain import Actor, PropertyStatus
from furnest.email.templates import (
    DEFAULT_BODY,
    DEFAULT_SUBJECT,
    DEFAULT_TEMPLATE_NAME,
    MAX_BODY_LENGTH,
    MAX_SUBJECT_LENGTH,
    context_for,
    render,
)
from furnest.logging import get_logger
from furnest.worker import queue

if TYPE_CHECKING:
    from furnest.worker.execution import JobContext

log = get_logger(__name__)

INQUIRY_CREATE = "inquiry.create"

_EMAIL = TypeAdapter(EmailStr)


def request_inquiry_in(
    session: Session,
    property_id: int,
    *,
    actor: Actor,
    at: datetime,
    detail: str | None = None,
) -> bool:
    """Approve a ``suggested`` property and queue :data:`INQUIRY_CREATE` for it.

    The caller checks the property is in :data:`~furnest.notify.embeds.INQUIRABLE`.
    Returns whether a job was queued: ``False`` means one was already waiting.
    """
    row = session.get(Property, property_id)
    if row is not None and row.status is PropertyStatus.SUGGESTED:
        transition_in(
            session, property_id, PropertyStatus.APPROVED, actor=actor, at=at, detail=detail
        )
    if queue.pending(session, INQUIRY_CREATE, property_id=property_id) is not None:
        return False
    queue.enqueue(
        session,
        INQUIRY_CREATE,
        {"property_id": property_id, "actor": actor.value, "actor_detail": detail},
    )
    return True


class InquiryCreate:
    """The :data:`INQUIRY_CREATE` handler."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._smtp = settings.smtp
        self._runtime = RuntimeSettings(settings, SqlSettingsStore(session_factory))
        self._criteria = CriteriaStore(session_factory)
        self._templates = SqlEmailTemplateStore(session_factory)
        self._clock = clock

    def __call__(self, ctx: JobContext) -> None:
        property_id = int(ctx.payload["property_id"])
        actor, detail = _requested_by(ctx.payload)
        template = self._templates.get(DEFAULT_TEMPLATE_NAME)
        subject, body = (
            (template.subject, template.body) if template else (DEFAULT_SUBJECT, DEFAULT_BODY)
        )
        active = self._criteria.active()
        signature = self._templates.signature()
        mode = self._runtime.get(INQUIRY_MODE)
        with session_scope(self._session_factory) as session:
            # A property merged since the job was queued: draft for the one it became.
            row = resolve(session, property_id)
            if row is not None:
                property_id = row.id
            if row is None or row.status is not PropertyStatus.APPROVED:
                log.info(
                    "inquiry_skipped",
                    property_id=property_id,
                    status=None if row is None else row.status.value,
                )
                return
            listing_row = primary_listing(session, property_id)
            if listing_row is None:
                log.warning("inquiry_skipped", property_id=property_id, reason="no listing")
                return
            listing = stored_listing(listing_row).listing
            rendered = render(
                subject,
                body,
                context_for(listing, active.criteria if active else None, signature),
            )
            to = str(listing.contact.email) if listing.contact.email else None
            email = draft_email(
                session,
                self._smtp,
                to=to,
                subject=rendered.subject,
                body=rendered.body,
                property_id=property_id,
            )
            transition_in(
                session,
                property_id,
                PropertyStatus.INQUIRY_DRAFTED,
                actor=actor,
                at=self._clock(),
                detail=detail,
                payload={"email_id": email.id},
            )
            send_now = False
            if mode is InquiryMode.AUTO and to is not None and self._smtp.configured:
                send_draft_in(
                    session,
                    self._smtp,
                    email.id,
                    to=to,
                    subject=email.subject,
                    body=email.body_rendered,
                )
                send_now = True
        log.info(
            "inquiry_drafted",
            property_id=property_id,
            email_id=email.id,
            mode=mode.value,
            queued=send_now,
            has_recipient=to is not None,
            smtp_configured=self._smtp.configured,
        )


def _requested_by(payload: Mapping[str, Any]) -> tuple[Actor, str | None]:
    try:
        actor = Actor(payload.get("actor", Actor.SYSTEM.value))
    except ValueError:
        actor = Actor.SYSTEM
    detail = payload.get("actor_detail")
    return actor, detail if isinstance(detail, str) else None


class InvalidEditError(ValueError):
    """What someone typed into the review form can't be sent."""


@dataclass(frozen=True)
class ReviewEdit:
    to: str
    subject: str
    body: str


@dataclass(frozen=True)
class ManualEdit:
    subject: str
    body: str


def review_edit(to: str, subject: str, body: str) -> ReviewEdit:
    """Check a reviewed inquiry before it is sent.

    The subject is folded onto one line, as :func:`~furnest.email.templates.render`
    does, since it becomes a header.
    """
    try:
        address = str(_EMAIL.validate_python(to.strip()))
    except ValidationError:
        raise InvalidEditError("Enter the email address to send this to.") from None
    text = manual_edit(subject, body)
    return ReviewEdit(to=address, subject=text.subject, body=text.body)


def manual_edit(subject: str, body: str) -> ManualEdit:
    """Check the text of an inquiry someone sent by hand before it is logged as sent.

    The same rules as :func:`review_edit`, without a recipient: the log should hold
    what an email would have.
    """
    folded = " ".join(subject.split())
    if not folded:
        raise InvalidEditError("The subject can't be empty.")
    if len(folded) > MAX_SUBJECT_LENGTH:
        raise InvalidEditError(f"The subject can be at most {MAX_SUBJECT_LENGTH} characters.")
    body = body.replace("\r\n", "\n")  # what a browser's textarea submits
    if not body.strip():
        raise InvalidEditError("The message can't be empty.")
    if len(body) > MAX_BODY_LENGTH:
        raise InvalidEditError(f"The message can be at most {MAX_BODY_LENGTH} characters.")
    return ManualEdit(subject=folded, body=body)
