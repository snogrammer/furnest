"""The dashboard: properties as a list, a board or a map, by status tab, and one
property's detail page, where notes are added, the status is changed, and a
property can be denied with a reason (saved as a note). All need auth, and the
posts need the CSRF token.

Filters live in the query string (see :class:`furnest.web.property_views.Filters`),
so every view has a URL and the filter form works as a plain GET without
JavaScript. With htmx the form and the tab, page and view links ask for the
results fragment only (``HX-Request``) and push the URL, so back and reload keep
working. A page past the last one shows the last page. The map view's page holds
an empty map; its script fetches the pins from ``/properties/map.json`` (JSON, same
filters and auth). With ``MAP_ENABLED`` off, ``view=map`` shows the list and the
JSON is a 404. A shape drawn on the map posts to ``/criteria/outlines``, which saves
it as an outline in a new criteria version and comes back to the map (303).

The detail page also asks for an inquiry (Inquire, :func:`inquire`): it approves a
suggested property and queues the job that drafts the email, as the Discord button
does. While the property is ``inquiry_drafted`` the page links to the draft in the
review queue (``/inquiries``), and the email log lists every email about it.

The detail page shows who is behind the unit (``owner_type``, #113) and has a small
form to set or clear that by hand (:func:`set_owner_type`); the list can be filtered
to one owner type (``owner``).

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

import json
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError
from starlette.responses import Response

from furnest.config import INQUIRY_MODE, InquiryMode, RuntimeSettings
from furnest.db.base import utcnow
from furnest.db.criteria_store import CriteriaStore
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
from furnest.domain import (
    Actor,
    Criteria,
    InvalidTransitionError,
    Outline,
    OwnerType,
    PropertyStatus,
    beds_baths,
)
from furnest.domain.status import TRANSITIONS
from furnest.email.inquiry import request_inquiry_in
from furnest.email.templates import MAX_BODY_LENGTH, MAX_SUBJECT_LENGTH
from furnest.notify.embeds import INQUIRABLE
from furnest.sources.registry import SourceRegistry
from furnest.web.auth import require_auth
from furnest.web.criteria_summary import summarize_criteria
from furnest.web.csrf import require_csrf
from furnest.web.enrichment_views import load_enrichment, price_chart, top_severity
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
    owner_label,
    price_timeline,
    status_actions,
    status_label,
)
from furnest.web.templating import photo_src, templates


def _source_namer(request: Request) -> Callable[[str], str]:
    registry: SourceRegistry = request.app.state.source_registry
    return registry.display_name


router = APIRouter(dependencies=[Depends(require_auth), Depends(require_csrf)])

_TAB_PATTERN = "^(" + "|".join(TAB_KEYS) + ")$"
# The dashboard filter takes any owner type; the override form only the known ones, or
# nothing to clear it.
_OWNER_FILTER_PATTERN = "^(|" + "|".join(k.value for k in OwnerType) + ")$"
_OWNER_CHOICES = [k for k in OwnerType if k is not OwnerType.UNKNOWN]
_OWNER_CHOICE_PATTERN = "^(|" + "|".join(k.value for k in _OWNER_CHOICES) + ")$"

# One bulk action moves at most this many properties (a few pages' worth).
MAX_BULK = 100


def _filters(
    request: Request,
    status: Annotated[str, Query(pattern=_TAB_PATTERN)] = DEFAULT_TAB,
    q: Annotated[str, Query(max_length=200)] = "",
    sort: PropertySort = PropertySort.SCORE,
    view: View = View.LIST,
    page: Annotated[int, Query(ge=1)] = 1,
    snoozed: bool = False,
    owner: Annotated[str, Query(pattern=_OWNER_FILTER_PATTERN)] = "",
) -> Filters:
    """The dashboard's query string, shared by the page and the map's pins."""
    if view is View.MAP and not request.app.state.settings.map.enabled:
        view = View.LIST
    return Filters(
        status=status,
        q=q.strip(),
        sort=sort,
        view=view,
        page=page,
        snoozed=snoozed,
        owner=OwnerType(owner) if owner else None,
    )


@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index(request: Request, filters: Annotated[Filters, Depends(_filters)]) -> Response:
    return _render_dashboard(request, filters)


# Declared before /properties/{property_id}, which would otherwise claim the path.
@router.get("/properties/map.json")
def map_points(request: Request, filters: Annotated[Filters, Depends(_filters)]) -> Response:
    """The map view's pins: every matching property with coordinates, newest first."""
    settings = request.app.state.settings.map
    if not settings.enabled:
        raise HTTPException(status_code=404)
    result = _store(request).map_points(
        now=utcnow(),
        limit=settings.max_points,
        statuses=filters.tab.statuses,
        text=filters.q or None,
        include_snoozed=filters.snoozed,
        owner_type=filters.owner,
    )
    points: list[dict[str, Any]] = []
    for row in result.rows:
        prop, listing = row.property, row.listing
        price = listing.monthly_price if listing else None
        photo = listing.photo_url if listing else None
        points.append(
            {
                "id": prop.id,
                "lat": prop.lat,
                "lng": prop.lng,
                "title": listing.title if listing else f"Property #{prop.id}",
                "rent": f"{price.format()}/mo" if price else None,
                "status": prop.status.value,
                "status_label": status_label(prop.status),
                "url": f"/properties/{prop.id}",
                "photo": photo_src(request, photo) if photo else None,
            }
        )
    return JSONResponse(
        {"points": points, "unlocated": result.unlocated, "truncated": result.truncated}
    )


@router.post("/criteria/outlines")
def save_outline(
    request: Request,
    name: Annotated[str, Form(max_length=200)] = "",
    polygon: Annotated[str, Form(max_length=100_000)] = "",
    back: Annotated[str, Form(max_length=1000)] = "",
) -> Response:
    """Save a shape drawn on the map as a search area: a new criteria version.

    ``polygon`` is the drawn GeoJSON geometry (a Polygon) or its outer ring. An
    outline with the same name is replaced. A bad shape or name re-renders the map
    with the reason (422) and saves nothing. ``back`` is the dashboard URL the form
    was on; only a dashboard URL (``/?...``) is followed, so it can't redirect away.
    """
    filters = Filters(view=View.MAP)
    target = back if back == "/" or back.startswith("/?") else filters.url()
    try:
        outline = Outline.model_validate({"name": name, "polygon": _outline_ring(polygon)})
    except (ValueError, ValidationError) as exc:
        message = _outline_error(exc)
        return _render_dashboard(request, filters, error=message, status_code=422)
    store = CriteriaStore(make_session_factory(request.app.state.engine))
    active = store.active()
    current = active.criteria if active else Criteria()
    kept = tuple(o for o in current.outlines if o.name != outline.name)
    criteria = current.model_copy(update={"outlines": (*kept, outline)})
    store.save(Criteria.model_validate(criteria.model_dump()), created_by="dashboard")
    return RedirectResponse(target, status_code=303)


def _outline_ring(text: str) -> object:
    """The outer ring of a submitted GeoJSON Polygon (or a bare ring), unvalidated."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        msg = "The drawn shape couldn't be read. Draw it again."
        raise ValueError(msg) from exc
    if isinstance(data, dict):
        if data.get("type") != "Polygon" or not data.get("coordinates"):
            msg = "Only a polygon or rectangle can be saved."
            raise ValueError(msg)
        return data["coordinates"][0]
    return data


def _outline_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        error = exc.errors()[0]
        field = ".".join(str(part) for part in error["loc"])
        text = str(error["msg"]).removeprefix("Value error, ")
        prefix = "Name: " if field == "name" else ""
        return f"Not saved. {prefix}{text[0].upper()}{text[1:]}."
    return f"Not saved. {exc}"


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
        "owner_label": owner_label,
        "owner_types_all": list(OwnerType),
        "source_name": _source_namer(request),
        "map": request.app.state.settings.map,
    }
    active = CriteriaStore(make_session_factory(request.app.state.engine)).active()
    if filters.view is View.MAP:
        # Saved outlines are drawn on the map (GeoJSON order, as stored).
        outlines = active.criteria.outlines if active else ()
        context["outlines"] = [{"name": o.name, "polygon": o.polygon} for o in outlines]
    if request.headers.get("HX-Request") == "true":
        # The tab lives in the filter form, outside the swapped results; update it too.
        context["oob"] = True
        response = templates.TemplateResponse(
            request, "dashboard/_results.html", context, status_code=status_code
        )
        # The form sends every field; push the short, canonical URL instead.
        response.headers["HX-Push-Url"] = filters.url(page=dashboard.page)
        return response
    context["criteria_summary"] = summarize_criteria(active.criteria) if active else None
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
    enrichment = load_enrichment(make_session_factory(request.app.state.engine), property_id)
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
        "owner_label": owner_label,
        "owner_types": _OWNER_CHOICES,
        "audit_label": audit_label,
        "prices": price_timeline(detail.price_history),
        "price_chart": price_chart(detail.price_history),
        "enrichment": enrichment,
        "risk_level": top_severity(enrichment.risks),
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
    owner: Annotated[str, Form(pattern=_OWNER_FILTER_PATTERN)] = "",
) -> Response:
    filters = Filters(
        status=status,
        q=q.strip(),
        sort=sort,
        page=page,
        snoozed=snoozed,
        owner=OwnerType(owner) if owner else None,
    )
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


@router.post("/properties/{property_id}/owner-type", response_class=HTMLResponse)
def set_owner_type(
    request: Request,
    property_id: int,
    owner_type: Annotated[str, Form(pattern=_OWNER_CHOICE_PATTERN)] = "",
) -> Response:
    """Set who is behind the property by hand; an empty value goes back to the listing's guess."""
    chosen = OwnerType(owner_type) if owner_type else None
    try:
        _store(request).set_owner_type_override(
            property_id, chosen, actor=Actor.DASHBOARD, at=utcnow()
        )
    except PropertyMergedError as e:
        return _merged(e)
    except PropertyNotFoundError:
        raise HTTPException(status_code=404) from None
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
