"""The criteria editor, version history and "re-run criteria". All need auth and CSRF.

Saving goes through :class:`~furnest.db.criteria_store.CriteriaStore`, so every save
is a new version and nothing is overwritten. A save that changes nothing is
reported, not stored. Forms post normally and work without JavaScript. With htmx,
the editor swaps in just the form partial and the re-run button just a status line.
Validation failures answer 422 and a re-run with nothing saved answers 409.

An area is entered as "City, State" (#179). ``/criteria/places`` fills the name
input's datalist as the user types, ``/criteria/rows/area/lookup`` re-renders one row
with the place's coordinates once a name is picked, and a form posted by a row's
"Look up" button (``lookup=<i>``) is re-rendered resolved but not saved, which is the
same thing without JavaScript. Under the ZIP and neighborhood lists, "Add ZIP codes"
and the neighborhood chips are buttons of the same kind (``add_zips=<i>``,
``add_neighborhood=<name>``): they fill the lists and re-render without saving (#180). The
htmx config in ``base.html`` swaps both, so the messages show in place.

Handlers are plain ``def`` so FastAPI runs the blocking SQLite calls in its threadpool.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from furnest.db.criteria_store import CriteriaStore, CriteriaVersionNotFoundError
from furnest.db.session import make_session_factory, session_scope
from furnest.domain import (
    Amenity,
    Criteria,
    DistanceUnit,
    FilterRule,
    OwnerType,
    PropertyType,
    amenity_label,
    diff_criteria,
    find_places,
    owner_type_label,
)
from furnest.pipeline.rerun import RERUN_CRITERIA
from furnest.web.auth import require_auth
from furnest.web.criteria_form import FormState, look_up_row, parse_form, state_from_criteria
from furnest.web.criteria_hints import build_hints
from furnest.web.criteria_summary import summarize_criteria
from furnest.web.csrf import require_csrf
from furnest.web.templating import templates
from furnest.worker import queue

router = APIRouter(prefix="/criteria", dependencies=[Depends(require_auth), Depends(require_csrf)])

# Choices the form offers, in display order.
CHOICES: dict[str, Any] = {
    "amenities": [(tag.value, amenity_label(tag)) for tag in Amenity],
    "property_type_options": [(kind.value, kind.value.replace("_", " ")) for kind in PropertyType],
    # Unknown isn't offered: it is "missing data", handled by the unknown_fails group.
    "owner_type_options": [
        (kind.value, owner_type_label(kind)) for kind in OwnerType if kind is not OwnerType.UNKNOWN
    ],
    "rule_options": [(rule.value, rule.value.replace("_", " ")) for rule in FilterRule],
    "units": [unit.value for unit in DistanceUnit],
}


# Fields inside the "Advanced" details, which opens by itself when one has an error.
ADVANCED_FIELDS = frozenset(
    {"keywords_allow", "keywords_deny", "price_fit_weight", "extra_beds_weight", "unknown_fails"}
)

PLACE_SUGGESTIONS = 10


def _store(request: Request) -> CriteriaStore:
    return CriteriaStore(make_session_factory(request.app.state.engine))


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _render_editor(
    request: Request,
    store: CriteriaStore,
    *,
    state: FormState | None = None,
    errors: dict[str, str] | None = None,
    flash: str | None = None,
    flash_error: bool = False,
    saved_version: int | None = None,
    status_code: int = 200,
) -> Response:
    active = store.active()
    if state is None:
        state = state_from_criteria(active.criteria if active else Criteria())
    context = {
        "state": state,
        "errors": errors or {},
        "flash": flash,
        "flash_error": flash_error,
        "saved_version": saved_version,
        "active": active,
        "summary": summarize_criteria(active.criteria) if active else None,
        "choices": CHOICES,
        "advanced_fields": ADVANCED_FIELDS,
        "hints": build_hints(make_session_factory(request.app.state.engine), state),
    }
    template = "criteria/_form.html" if _is_htmx(request) else "criteria/edit.html"
    return templates.TemplateResponse(request, template, context, status_code=status_code)


@router.get("", response_class=HTMLResponse)
def edit(
    request: Request,
    saved: Annotated[int | None, Query()] = None,
    rerun: Annotated[int | None, Query()] = None,
) -> Response:
    flash = f"Re-run queued (job #{rerun})." if rerun is not None else None
    return _render_editor(request, _store(request), flash=flash, saved_version=saved)


@router.post("", response_class=HTMLResponse)
async def save(request: Request) -> Response:
    parsed = parse_form(await request.form())
    store = _store(request)
    if parsed.lookup:
        return _render_editor(
            request,
            store,
            state=parsed.state,
            errors=parsed.errors,
            flash=None if parsed.errors else parsed.notice or "Looked up. Review and save.",
            status_code=422 if parsed.errors else 200,
        )
    if parsed.criteria is None:
        return _render_editor(
            request, store, state=parsed.state, errors=parsed.errors, status_code=422
        )
    active = store.active()
    if active is not None and active.criteria == parsed.criteria:
        return _render_editor(
            request, store, flash=f"No changes; version {active.version} is still active."
        )
    record = store.save(parsed.criteria, created_by="dashboard")
    if _is_htmx(request):
        return _render_editor(request, store, saved_version=record.version)
    return RedirectResponse(f"/criteria?saved={record.version}", status_code=303)


@router.get("/places", response_class=HTMLResponse)
def places(
    request: Request,
    index: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> Response:
    # htmx sends the typed name under the row's own input name.
    query = q if q is not None else request.query_params.get(f"areas-{index}-name", "")
    matches = find_places(query[:200], limit=PLACE_SUGGESTIONS)
    return templates.TemplateResponse(request, "criteria/_place_options.html", {"places": matches})


@router.get("/rows/area/lookup", response_class=HTMLResponse)
def lookup_area_row(request: Request, index: Annotated[int, Query(ge=0)]) -> Response:
    row, message = look_up_row(request.query_params, index)
    errors = {f"areas-{index}-name": message} if message else {}
    context = {"index": index, "row": row, "choices": CHOICES, "errors": errors, "add_next": False}
    return templates.TemplateResponse(request, "criteria/_area_row.html", context)


@router.get("/rows/{kind}", response_class=HTMLResponse)
def new_row(request: Request, kind: str, index: Annotated[int, Query(ge=0)]) -> Response:
    if kind not in ("area", "amenity"):
        raise HTTPException(status_code=404)
    context = {"index": index, "choices": CHOICES, "errors": {}, "add_next": True}
    if kind == "area":
        context["row"] = {"unit": DistanceUnit.MILES.value}
    else:
        context["row"] = ("", "")
    return templates.TemplateResponse(request, f"criteria/_{kind}_row.html", context)


@router.get("/history", response_class=HTMLResponse)
def history(request: Request) -> Response:
    records = _store(request).history()
    # Newest first, each diffed against the version saved before it.
    versions = [
        {
            "record": record,
            "changes": (
                diff_criteria(records[i + 1].criteria, record.criteria)
                if i + 1 < len(records)
                else None
            ),
        }
        for i, record in enumerate(records)
    ]
    return templates.TemplateResponse(request, "criteria/history.html", {"versions": versions})


@router.post("/history/{version}/activate")
def activate(request: Request, version: int) -> Response:
    try:
        _store(request).activate(version)
    except CriteriaVersionNotFoundError:
        raise HTTPException(status_code=404, detail="no such criteria version") from None
    return RedirectResponse("/criteria/history", status_code=303)


@router.post("/rerun", response_class=HTMLResponse)
def rerun(request: Request) -> Response:
    store = _store(request)
    active = store.active()
    if active is None:
        message, status_code = "Save criteria first; there is nothing to re-run.", 409
        if not _is_htmx(request):
            return _render_editor(
                request, store, flash=message, flash_error=True, status_code=status_code
            )
    else:
        with session_scope(make_session_factory(request.app.state.engine)) as session:
            job = queue.enqueue(session, RERUN_CRITERIA, {"criteria_version": active.version})
            job_id = job.id
        if not _is_htmx(request):
            return RedirectResponse(f"/criteria?rerun={job_id}", status_code=303)
        message, status_code = f"Re-run queued (job #{job_id}).", 200
    return templates.TemplateResponse(
        request,
        "criteria/_status.html",
        {"message": message, "error": status_code != 200},
        status_code=status_code,
    )
