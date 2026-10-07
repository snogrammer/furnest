"""The dashboard: properties as a list or a board, by status tab, and one
property's detail page, where notes are added, the status is changed, and a
property can be denied with a reason (saved as a note). All need auth, and the
posts need the CSRF token.

Filters live in the query string (see :class:`furnest.web.property_views.Filters`),
so every view has a URL and the filter form works as a plain GET without
JavaScript. With htmx the form and the tab, page and view links ask for the
results fragment only (``HX-Request``) and push the URL, so back and reload keep
working. A page past the last one shows the last page.

The detail page also asks for an inquiry (Inquire, :func:`inquire`): it approves a
suggested property and queues the job that drafts the email, as the Discord button
does. While the property is ``inquiry_drafted`` the page links to the draft in the
review queue (``/inquiries``), and the email log lists every email about it.

A property flagged ``manual_contact_required`` (its listing gives no email address)
gets a prominent link to the listing, where the lister is messaged. While its draft
waits, the page shows the message ready to copy and a **Mark as sent** form, which
posts to the review queue's ``/inquiries/{id}/mark-sent`` and comes back here.

A post redirects back to the detail page (303). An empty or too long note
re-renders the page with the error (422); a deny or status change the state
machine forbids re-renders it with 409. The status buttons only offer allowed
moves, so a 409 means the page was stale.

Grouping mistakes are fixed here too: **Merge** takes another property's number and
merges it into this one, which keeps its status, and each listing of a property
with several has a **Split off** button that moves it to a new property. An unknown
number or a merge with itself re-renders the page with 422.

The list view's bulk deny/archive moves what it can and, with htmx, re-renders the
results with a notice of what it skipped; without htmx it redirects back to the
list (303). An empty or oversized selection is a 422, and one where nothing could
be moved a 409. Every change is recorded with the ``dashboard`` actor.

The handlers are plain ``def`` so FastAPI runs the blocking SQLite calls in its
threadpool.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from furnest.config import INQUIRY_MODE, InquiryMode, RuntimeSettings
from furnest.db.base import utcnow
from furnest.db.email_store import SqlEmailStore, StoredEmail
from furnest.db.models import EmailDirection, Property
from furnest.db.property_store import (
    MAX_NOTE_LENGTH,
    GroupingError,
    ListingNotFoundError,
    PropertyMergedError,
    PropertyNotFoundError,
    PropertySort,
    PropertyState,
    SqlPropertyStore,
)
from furnest.db.session import make_session_factory, session_scope
from furnest.db.settings_store import SqlSettingsStore
from furnest.domain import Actor, InvalidTransitionError, PropertyStatus, beds_baths
from furnest.domain.status import TRANSITIONS
from furnest.email.inquiry import request_inquiry_in
from furnest.email.templates import MAX_BODY_LENGTH, MAX_SUBJECT_LENGTH
from furnest.notify.embeds import INQUIRABLE
from furnest.sources.registry import SourceRegistry
from furnest.web.auth import require_auth
from furnest.web.csrf import require_csrf
from furnest.web.html import render_markdown
from furnest.web.property_views import (
    DEFAULT_TAB,
    SORT_LABELS,
    TAB_KEYS,
    TABS,
    BulkAction,
    Filters,
    View,
    apply_bulk,
    audit_label,
    build_dashboard,
    status_actions,
    status_label,
)
from furnest.web.templating import templates


def _source_namer(request: Request) -> Callable[[str], str]:
    registry: SourceRegistry = request.app.state.source_registry
    return registry.display_name


router = APIRouter(dependencies=[Depends(require_auth), Depends(require_csrf)])

_TAB_PATTERN = "^(" + "|".join(TAB_KEYS) + ")$"

# One bulk action moves at most this many properties (a few pages' worth).
MAX_BULK = 100


@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index(
    request: Request,
    status: Annotated[str, Query(pattern=_TAB_PATTERN)] = DEFAULT_TAB,
    q: Annotated[str, Query(max_length=200)] = "",
    sort: PropertySort = PropertySort.SCORE,
    view: View = View.LIST,
    page: Annotated[int, Query(ge=1)] = 1,
    snoozed: bool = False,
) -> Response:
    filters = Filters(status=status, q=q.strip(), sort=sort, view=view, page=page, snoozed=snoozed)
    return _render_dashboard(request, filters)


def _render_dashboard(
    request: Request,
    filters: Filters,
    *,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    dashboard = build_dashboard(
        make_session_factory(request.app.state.engine), filters, now=utcnow()
    )
    context = {
        "d": dashboard,
        "f": filters,
        "tabs": TABS,
        "sorts": SORT_LABELS,
        "notice": notice,
        "error": error,
        "beds_baths": beds_baths,
        "status_label": status_label,
        "source_name": _source_namer(request),
    }
    if request.headers.get("HX-Request") == "true":
        # The tab lives in the filter form, outside the swapped results; update it too.
        context["oob"] = True
        response = templates.TemplateResponse(
            request, "dashboard/_results.html", context, status_code=status_code
        )
        # The form sends every field; push the short, canonical URL instead.
        response.headers["HX-Push-Url"] = filters.url(page=dashboard.page)
        return response
    return templates.TemplateResponse(request, "index.html", context, status_code=status_code)


def _store(request: Request) -> SqlPropertyStore:
    return SqlPropertyStore(make_session_factory(request.app.state.engine))


def _render_detail(
    request: Request,
    property_id: int,
    *,
    error: str | None = None,
    notice: str | None = None,
    note: str = "",
    reason: str = "",
    status_code: int = 200,
) -> Response:
    detail = _store(request).detail(property_id)
    if detail is None:
        raise HTTPException(status_code=404)
    if detail.property.merged_into_id is not None:
        return _merged(PropertyMergedError(property_id, detail.property.merged_into_id))
    emails = SqlEmailStore(make_session_factory(request.app.state.engine)).for_property(property_id)
    status = detail.property.status
    inquiry = _open_inquiry(status, emails)
    context = {
        "d": detail,
        "p": detail.property,
        "can_deny": PropertyStatus.DENIED in TRANSITIONS[detail.property.status],
        "actions": status_actions(status),
        "can_inquire": status in INQUIRABLE,
        "smtp_configured": request.app.state.settings.smtp.configured,
        "emails": emails,
        # The draft (or failed send) waiting in the review queue.
        "inquiry": inquiry,
        # ...shown here to copy, when it goes out by hand.
        "manual_inquiry": inquiry if inquiry and _by_hand(detail.property, inquiry) else None,
        "notice": notice,
        "archived": PropertyStatus.ARCHIVED,
        "error": error,
        "note": note,
        "reason": reason,
        "max_note_length": MAX_NOTE_LENGTH,
        "max_subject": MAX_SUBJECT_LENGTH,
        "max_body": MAX_BODY_LENGTH,
        "beds_baths": beds_baths,
        "status_label": status_label,
        "audit_label": audit_label,
        "render_markdown": render_markdown,
        "source_name": _source_namer(request),
    }
    return templates.TemplateResponse(
        request, "properties/detail.html", context, status_code=status_code
    )


@router.api_route("/properties/{property_id}", methods=["GET", "HEAD"], response_class=HTMLResponse)
def property_detail(
    request: Request,
    property_id: int,
    inquiry: Annotated[str | None, Query(max_length=20)] = None,
    grouping: Annotated[str | None, Query(max_length=20)] = None,
    new: int | None = None,
) -> Response:
    notice = None
    if grouping == "merged":
        notice = "Properties merged."
    elif grouping == "split" and new is not None:
        notice = f"Listing split off into property #{new}."
    elif inquiry == "marked":
        notice = "Inquiry marked as sent."
    elif inquiry == "requested":
        settings = request.app.state.settings
        factory = make_session_factory(request.app.state.engine)
        mode = RuntimeSettings(settings, SqlSettingsStore(factory)).get(INQUIRY_MODE)
        notice = (
            "Inquiry requested. It is sent as soon as it's written."
            if mode is InquiryMode.AUTO
            else "Inquiry requested. The draft shows up under Inquiries in a moment."
        )
    return _render_detail(request, property_id, notice=notice)


def _merged(e: PropertyMergedError) -> Response:
    """Send the browser to the property ``e``'s was merged into."""
    return RedirectResponse(f"/properties/{e.merged_into}", status_code=303)


def _by_hand(state: PropertyState, inquiry: StoredEmail) -> bool:
    """Whether the waiting inquiry is to be sent by hand, through the listing's site."""
    return state.manual_contact_required or inquiry.to_address is None


def _open_inquiry(status: PropertyStatus, emails: list[StoredEmail]) -> StoredEmail | None:
    """The draft (or failed send) waiting in the review queue, while there is one."""
    if status is not PropertyStatus.INQUIRY_DRAFTED:
        return None
    outbound = [e for e in emails if e.direction is EmailDirection.OUTBOUND]
    return outbound[-1] if outbound else None


# The form fields allow some slack over MAX_NOTE_LENGTH for surrounding whitespace;
# the store enforces the limit on the stripped text.
_FORM_MAX = MAX_NOTE_LENGTH + 1_000


@router.post("/properties/{property_id}/notes", response_class=HTMLResponse)
def add_note(
    request: Request,
    property_id: int,
    body: Annotated[str, Form(max_length=_FORM_MAX)] = "",
) -> Response:
    try:
        _store(request).add_note(property_id, body, actor=Actor.DASHBOARD, at=utcnow())
    except PropertyMergedError as e:
        return _merged(e)
    except PropertyNotFoundError:
        raise HTTPException(status_code=404) from None
    except ValueError as e:
        return _render_detail(request, property_id, error=str(e), note=body, status_code=422)
    return RedirectResponse(f"/properties/{property_id}#notes", status_code=303)


@router.post("/properties/{property_id}/deny", response_class=HTMLResponse)
def deny(
    request: Request,
    property_id: int,
    reason: Annotated[str, Form(max_length=_FORM_MAX)] = "",
) -> Response:
    try:
        _store(request).deny(property_id, actor=Actor.DASHBOARD, at=utcnow(), reason=reason)
    except PropertyMergedError as e:
        return _merged(e)
    except PropertyNotFoundError:
        raise HTTPException(status_code=404) from None
    except InvalidTransitionError as e:
        return _render_detail(
            request, property_id, error=f"Can't deny: {e.reason}.", status_code=409
        )
    except ValueError as e:
        return _render_detail(request, property_id, error=str(e), reason=reason, status_code=422)
    return RedirectResponse(f"/properties/{property_id}", status_code=303)


@router.post("/properties/bulk", response_class=HTMLResponse)
def bulk(
    request: Request,
    action: Annotated[BulkAction, Form()],
    ids: Annotated[list[int] | None, Form()] = None,
    status: Annotated[str, Form(pattern=_TAB_PATTERN)] = DEFAULT_TAB,
    q: Annotated[str, Form(max_length=200)] = "",
    sort: Annotated[PropertySort, Form()] = PropertySort.SCORE,
    page: Annotated[int, Form(ge=1)] = 1,
    snoozed: Annotated[bool, Form()] = False,
) -> Response:
    filters = Filters(status=status, q=q.strip(), sort=sort, page=page, snoozed=snoozed)
    if not ids:
        return _render_dashboard(
            request, filters, error="Select at least one property.", status_code=422
        )
    if len(ids) > MAX_BULK:
        return _render_dashboard(
            request,
            filters,
            error=f"Select at most {MAX_BULK} properties at a time.",
            status_code=422,
        )
    result = apply_bulk(_store(request), ids, action, at=utcnow())
    if not result.moved:
        return _render_dashboard(request, filters, error=result.notice(), status_code=409)
    if request.headers.get("HX-Request") == "true":
        return _render_dashboard(request, filters, notice=result.notice())
    return RedirectResponse(filters.url(), status_code=303)


@router.post("/properties/{property_id}/inquire", response_class=HTMLResponse)
def inquire(request: Request, property_id: int) -> Response:
    with session_scope(make_session_factory(request.app.state.engine)) as session:
        row = session.get(Property, property_id)
        if row is None:
            raise HTTPException(status_code=404)
        if row.merged_into_id is not None:
            return _merged(PropertyMergedError(property_id, row.merged_into_id))
        status = row.status
        if status in INQUIRABLE:
            request_inquiry_in(session, property_id, actor=Actor.DASHBOARD, at=utcnow())
    if status not in INQUIRABLE:
        return _render_detail(
            request,
            property_id,
            error=f"Can't send an inquiry for this property: it is {status_label(status)}.",
            status_code=409,
        )
    return RedirectResponse(f"/properties/{property_id}?inquiry=requested", status_code=303)


@router.post("/properties/{property_id}/status", response_class=HTMLResponse)
def change_status(
    request: Request, property_id: int, to: Annotated[PropertyStatus, Form()]
) -> Response:
    try:
        _store(request).transition(property_id, to, actor=Actor.DASHBOARD, at=utcnow())
    except PropertyMergedError as e:
        return _merged(e)
    except PropertyNotFoundError:
        raise HTTPException(status_code=404) from None
    except InvalidTransitionError as e:
        return _render_detail(
            request,
            property_id,
            error=f"Can't move to {status_label(to)}: {e.reason}.",
            status_code=409,
        )
    return RedirectResponse(f"/properties/{property_id}", status_code=303)


@router.post("/properties/{property_id}/merge", response_class=HTMLResponse)
def merge(request: Request, property_id: int, other: Annotated[int, Form()]) -> Response:
    try:
        _store(request).merge_properties(property_id, other, actor=Actor.DASHBOARD, at=utcnow())
    except PropertyMergedError as e:
        return _merged(e)
    except PropertyNotFoundError as e:
        if e.property_id == property_id:
            raise HTTPException(status_code=404) from None
        return _render_detail(request, property_id, error=f"No property #{other}.", status_code=422)
    except GroupingError as e:
        return _render_detail(request, property_id, error=f"Can't merge: {e}.", status_code=422)
    return RedirectResponse(f"/properties/{property_id}?grouping=merged", status_code=303)


@router.post("/properties/{property_id}/listings/{listing_id}/split", response_class=HTMLResponse)
def split(request: Request, property_id: int, listing_id: int) -> Response:
    try:
        new = _store(request).split_listing(
            property_id, listing_id, actor=Actor.DASHBOARD, at=utcnow()
        )
    except PropertyMergedError as e:
        return _merged(e)
    except (PropertyNotFoundError, ListingNotFoundError):
        raise HTTPException(status_code=404) from None
    except GroupingError as e:
        return _render_detail(request, property_id, error=f"Can't split: {e}.", status_code=422)
    return RedirectResponse(
        f"/properties/{property_id}?grouping=split&new={new.id}", status_code=303
    )
