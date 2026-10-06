"""The dashboard: properties as a list or a board, by status tab, and one
property's detail page, where notes are added and a property can be denied with
a reason (saved as a note). All need auth, and the posts need the CSRF token.

Filters live in the query string (see :class:`furnest.web.property_views.Filters`),
so every view has a URL and the filter form works as a plain GET without
JavaScript. With htmx the form and the tab, page and view links ask for the
results fragment only (``HX-Request``) and push the URL, so back and reload keep
working. A page past the last one shows the last page.

A post redirects back to the detail page (303). An empty or too long note
re-renders the page with the error (422); a deny the state machine forbids
re-renders it with 409.

The handlers are plain ``def`` so FastAPI runs the blocking SQLite calls in its
threadpool.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from furnest.db.base import utcnow
from furnest.db.property_store import (
    MAX_NOTE_LENGTH,
    PropertyNotFoundError,
    PropertySort,
    SqlPropertyStore,
)
from furnest.db.session import make_session_factory
from furnest.domain import Actor, InvalidTransitionError, PropertyStatus
from furnest.domain.status import TRANSITIONS
from furnest.web.auth import require_auth
from furnest.web.csrf import require_csrf
from furnest.web.html import render_markdown
from furnest.web.property_views import (
    DEFAULT_TAB,
    SORT_LABELS,
    TAB_KEYS,
    TABS,
    Filters,
    View,
    audit_label,
    beds_baths,
    build_dashboard,
    status_label,
)
from furnest.web.templating import templates


def _source_namer(request: Request) -> Callable[[str], str]:
    registry = request.app.state.source_registry

    def source_name(name: str) -> str:
        adapter = registry.get(name)
        return adapter.display_name if adapter is not None else name

    return source_name


router = APIRouter(dependencies=[Depends(require_auth), Depends(require_csrf)])

_TAB_PATTERN = "^(" + "|".join(TAB_KEYS) + ")$"


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
    dashboard = build_dashboard(
        make_session_factory(request.app.state.engine), filters, now=utcnow()
    )
    context = {
        "d": dashboard,
        "f": filters,
        "tabs": TABS,
        "sorts": SORT_LABELS,
        "beds_baths": beds_baths,
        "status_label": status_label,
        "source_name": _source_namer(request),
    }
    if request.headers.get("HX-Request") == "true":
        # The tab lives in the filter form, outside the swapped results; update it too.
        context["oob"] = True
        response = templates.TemplateResponse(request, "dashboard/_results.html", context)
        # The form sends every field; push the short, canonical URL instead.
        response.headers["HX-Push-Url"] = filters.url(page=dashboard.page)
        return response
    return templates.TemplateResponse(request, "index.html", context)


def _store(request: Request) -> SqlPropertyStore:
    return SqlPropertyStore(make_session_factory(request.app.state.engine))


def _render_detail(
    request: Request,
    property_id: int,
    *,
    error: str | None = None,
    note: str = "",
    reason: str = "",
    status_code: int = 200,
) -> Response:
    detail = _store(request).detail(property_id)
    if detail is None:
        raise HTTPException(status_code=404)
    context = {
        "d": detail,
        "p": detail.property,
        "can_deny": PropertyStatus.DENIED in TRANSITIONS[detail.property.status],
        "error": error,
        "note": note,
        "reason": reason,
        "max_note_length": MAX_NOTE_LENGTH,
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
def property_detail(request: Request, property_id: int) -> Response:
    return _render_detail(request, property_id)


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
    except PropertyNotFoundError:
        raise HTTPException(status_code=404) from None
    except InvalidTransitionError as e:
        return _render_detail(
            request, property_id, error=f"Can't deny: {e.reason}.", status_code=409
        )
    except ValueError as e:
        return _render_detail(request, property_id, error=str(e), reason=reason, status_code=422)
    return RedirectResponse(f"/properties/{property_id}", status_code=303)
