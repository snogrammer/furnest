"""The inquiry review queue: drafts to edit and send, or discard. All need auth and CSRF.

``/inquiries`` lists what waits on a person
(:meth:`~furnest.db.email_store.SqlEmailStore.review_queue`): every draft, and every
inquiry that failed for good while its property is still ``inquiry_drafted``. Each
one is a form with the recipient, subject and body, filled in from the draft.

* **Send** checks the form (:func:`~furnest.email.inquiry.review_edit`), saves it to
  the email row and queues it, then redirects back (303). A form that doesn't pass
  re-renders the queue with the error and what was typed (422). The property becomes
  ``inquiry_sent`` once the worker has delivered it.
* **Mark as sent** is for an inquiry someone sent by hand, through the listing's
  source, because the listing has no email address (or SMTP isn't set up). It checks
  the subject and message (:func:`~furnest.email.inquiry.manual_edit`), logs them as
  sent and moves the property to ``inquiry_sent`` straight away
  (:func:`~furnest.db.email_store.mark_sent_manually_in`). The detail page posts here
  too, with ``back=property`` to land back on the property.
* **Discard** deletes the draft (a failed email stays in the log) and moves the
  property back to ``approved``, in one transaction, then redirects back (303).

Each answers 404 for an email that doesn't exist, and 409 when it, or its property,
has moved on: already sent, or the property was archived meanwhile. Send also
answers 409 while SMTP isn't set up.

Handlers are plain ``def`` so FastAPI runs the blocking SQLite calls in its threadpool.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import Response

from furnest.db.base import utcnow
from furnest.db.email_store import (
    EmailNotFoundError,
    NotSendableError,
    SmtpNotConfiguredError,
    SqlEmailStore,
    discard_draft_in,
    mark_sent_manually_in,
    send_draft_in,
)
from furnest.db.models import Email, Property
from furnest.db.property_store import transition_in
from furnest.db.session import make_session_factory, session_scope
from furnest.domain import Actor, InvalidTransitionError, PropertyStatus
from furnest.email.inquiry import InvalidEditError, manual_edit, review_edit
from furnest.email.templates import MAX_BODY_LENGTH, MAX_SUBJECT_LENGTH
from furnest.web.auth import require_auth
from furnest.web.csrf import require_csrf
from furnest.web.templating import templates

router = APIRouter(prefix="/inquiries", dependencies=[Depends(require_auth), Depends(require_csrf)])

# Form slack over the limits review_edit enforces, so it can say what's wrong.
_SUBJECT_FORM_MAX = MAX_SUBJECT_LENGTH + 1_000
_BODY_FORM_MAX = MAX_BODY_LENGTH + 1_000

_FLASHES = {
    "sent": "Inquiry queued to send.",
    "marked": "Inquiry marked as sent.",
    "discarded": "Draft discarded.",
}


def _factory(request: Request) -> sessionmaker[Session]:
    return make_session_factory(request.app.state.engine)


def _render(
    request: Request,
    *,
    flash: str | None = None,
    error: str | None = None,
    failed: int | None = None,
    typed: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    context: dict[str, Any] = {
        "items": SqlEmailStore(_factory(request)).review_queue(),
        "flash": flash,
        "error": error,
        # The email whose form failed, and what was typed into it.
        "failed": failed,
        "typed": typed or {},
        "max_subject": MAX_SUBJECT_LENGTH,
        "max_body": MAX_BODY_LENGTH,
        "smtp_configured": request.app.state.settings.smtp.configured,
        "source_name": request.app.state.source_registry.display_name,
    }
    return templates.TemplateResponse(
        request, "inquiries/index.html", context, status_code=status_code
    )


def _property_moved_on(session: Session, property_id: int | None) -> bool:
    """Whether the email's property is no longer waiting on its inquiry."""
    if property_id is None:
        return False
    row = session.get(Property, property_id)
    return row is not None and row.status is not PropertyStatus.INQUIRY_DRAFTED


@router.get("", response_class=HTMLResponse)
def index(request: Request, done: Annotated[str | None, Query(max_length=20)] = None) -> Response:
    return _render(request, flash=_FLASHES.get(done or ""))


@router.post("/{email_id}/send", response_class=HTMLResponse)
def send(
    request: Request,
    email_id: int,
    to: Annotated[str, Form(max_length=320)] = "",
    subject: Annotated[str, Form(max_length=_SUBJECT_FORM_MAX)] = "",
    body: Annotated[str, Form(max_length=_BODY_FORM_MAX)] = "",
) -> Response:
    typed = {"to": to, "subject": subject, "body": body}
    try:
        edit = review_edit(to, subject, body)
    except InvalidEditError as e:
        return _render(request, error=str(e), failed=email_id, typed=typed, status_code=422)
    try:
        with session_scope(_factory(request)) as session:
            email = send_draft_in(
                session,
                request.app.state.settings.smtp,
                email_id,
                to=edit.to,
                subject=edit.subject,
                body=edit.body,
            )
            if _property_moved_on(session, email.property_id):
                raise NotSendableError(email_id, email.status)
    except EmailNotFoundError:
        raise HTTPException(status_code=404) from None
    except SmtpNotConfiguredError:
        return _render(
            request,
            error=(
                "SMTP isn't set up, so furnest can't send this. Send it yourself and"
                " mark it as sent, or set SMTP_HOST and SMTP_FROM_ADDRESS."
            ),
            failed=email_id,
            typed=typed,
            status_code=409,
        )
    except NotSendableError:
        return _render(
            request,
            error="That inquiry can't be sent any more: it was sent or its property moved on.",
            status_code=409,
        )
    return RedirectResponse("/inquiries?done=sent", status_code=303)


@router.post("/{email_id}/mark-sent", response_class=HTMLResponse)
def mark_sent(
    request: Request,
    email_id: int,
    subject: Annotated[str, Form(max_length=_SUBJECT_FORM_MAX)] = "",
    body: Annotated[str, Form(max_length=_BODY_FORM_MAX)] = "",
    to: Annotated[str, Form(max_length=320)] = "",
    back: Annotated[str | None, Form(max_length=20)] = None,
) -> Response:
    # ``to`` only comes along from the queue's form, to re-render what was typed.
    typed = {"to": to, "subject": subject, "body": body}
    try:
        edit = manual_edit(subject, body)
    except InvalidEditError as e:
        return _render(request, error=str(e), failed=email_id, typed=typed, status_code=422)
    try:
        with session_scope(_factory(request)) as session:
            # Checked first: marking it sent moves the property on itself.
            row = session.get(Email, email_id)
            if row is not None and _property_moved_on(session, row.property_id):
                raise NotSendableError(email_id, row.status)
            email = mark_sent_manually_in(
                session, email_id, subject=edit.subject, body=edit.body, at=utcnow()
            )
    except EmailNotFoundError:
        raise HTTPException(status_code=404) from None
    except NotSendableError:
        return _render(
            request,
            error="That inquiry can't be marked as sent: it was sent or its property moved on.",
            status_code=409,
        )
    if back == "property" and email.property_id is not None:
        return RedirectResponse(f"/properties/{email.property_id}?inquiry=marked", status_code=303)
    return RedirectResponse("/inquiries?done=marked", status_code=303)


@router.post("/{email_id}/discard", response_class=HTMLResponse)
def discard(request: Request, email_id: int) -> Response:
    try:
        with session_scope(_factory(request)) as session:
            email = discard_draft_in(session, email_id)
            if email.property_id is not None:
                transition_in(
                    session,
                    email.property_id,
                    PropertyStatus.APPROVED,
                    actor=Actor.DASHBOARD,
                    at=utcnow(),
                    payload={"email_id": email_id, "discarded": True},
                )
    except EmailNotFoundError:
        raise HTTPException(status_code=404) from None
    except (NotSendableError, InvalidTransitionError):
        return _render(
            request,
            error="That draft can't be discarded any more: it was sent or its property moved on.",
            status_code=409,
        )
    return RedirectResponse("/inquiries?done=discarded", status_code=303)
