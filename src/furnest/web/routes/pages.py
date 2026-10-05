"""The dashboard: properties as a list or a board, by status tab, and one
property's detail page. All need auth.

Filters live in the query string (see :class:`furnest.web.property_views.Filters`),
so every view has a URL and the filter form works as a plain GET without
JavaScript. With htmx the form and the tab, page and view links ask for the
results fragment only (``HX-Request``) and push the URL, so back and reload keep
working. A page past the last one shows the last page.

The handler is a plain ``def`` so FastAPI runs the blocking SQLite calls in its
threadpool.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from furnest.db.base import utcnow
from furnest.db.property_store import PropertySort, SqlPropertyStore
from furnest.db.session import make_session_factory
from furnest.web.auth import require_auth
from furnest.web.csrf import require_csrf
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


@router.api_route("/properties/{property_id}", methods=["GET", "HEAD"], response_class=HTMLResponse)
def property_detail(request: Request, property_id: int) -> Response:
    store = SqlPropertyStore(make_session_factory(request.app.state.engine))
    detail = store.detail(property_id)
    if detail is None:
        raise HTTPException(status_code=404)
    context = {
        "d": detail,
        "p": detail.property,
        "beds_baths": beds_baths,
        "status_label": status_label,
        "audit_label": audit_label,
        "source_name": _source_namer(request),
    }
    return templates.TemplateResponse(request, "properties/detail.html", context)
