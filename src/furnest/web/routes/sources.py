"""The source health page: per-source status, the enable switch, each source's
settings, the shared credentials, and "run now".

All need auth and CSRF. Forms post normally and redirect back with a flash message,
so the page works without JavaScript (the switch then needs its Apply button). An
action the source can't take (toggling a source pinned in ``SOURCES_ADAPTERS``,
running one that is off or can't run, editing a stub or a field env pins) answers
409 with the page and the reason, a settings value that doesn't validate answers 422
with the field marked, and an unknown source or credential is a 404.

The settings form takes ``search_locations`` (one per line, since place names carry
commas), ``interval_minutes`` and ``max_results``; an empty field means "use the default",
and "Reset to defaults" clears all three. A credential is a token entered once and
shared by every source that declares it (see
:class:`furnest.sources.base.Credential`); the page never shows it back, only
whether one is set.

"Run now" queues a ``run_source`` job for the worker (see
:class:`furnest.pipeline.schedule.RunSourceNow`). A source with one already queued
or running gets no second job.

Handlers are plain ``def`` so FastAPI runs the blocking SQLite calls in its threadpool.
"""

from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import Response

from furnest.config import SourceOverrides
from furnest.db.session import make_session_factory, session_scope
from furnest.db.source_store import SqlSourceStore
from furnest.pipeline.schedule import RUN_SOURCE
from furnest.sources.base import Credential
from furnest.sources.registry import SourceRegistry
from furnest.web.auth import require_auth
from furnest.web.csrf import require_csrf
from furnest.web.source_health import SourceRow, build_credentials, build_row, build_rows
from furnest.web.templating import templates
from furnest.worker import queue

router = APIRouter(prefix="/sources", dependencies=[Depends(require_auth), Depends(require_csrf)])

# The settings form's fields, in the order the form lists them.
SETTINGS_FIELDS = ("search_locations", "interval_minutes", "max_results")
MAX_FIELD_LENGTH = 2000


def _factory(request: Request) -> sessionmaker[Session]:
    return make_session_factory(request.app.state.engine)


def _render(
    request: Request,
    *,
    flash: str | None = None,
    error: str | None = None,
    status_code: int = 200,
    errors: Mapping[str, str] | None = None,
    form_source: str | None = None,
    form: Mapping[str, str] | None = None,
) -> Response:
    """``errors``, ``form_source`` and ``form`` re-show a settings form that was refused:
    the messages per field, which source's form, and what was typed."""
    registry = request.app.state.source_registry
    settings = request.app.state.settings.sources
    factory = _factory(request)
    context = {
        "credentials": build_credentials(registry, settings, factory),
        "rows": build_rows(registry, settings, factory),
        "flash": flash,
        "error": error,
        "errors": errors or {},
        "form_source": form_source,
        "form": form or {},
    }
    return templates.TemplateResponse(
        request, "sources/index.html", context, status_code=status_code
    )


def _row(request: Request, name: str) -> SourceRow:
    row = build_row(
        name,
        request.app.state.source_registry,
        request.app.state.settings.sources,
        _factory(request),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no such source")
    return row


def _credential(request: Request, key: str) -> Credential:
    registry: SourceRegistry = request.app.state.source_registry
    for credential in registry.credentials():
        if credential.key == key:
            return credential
    raise HTTPException(status_code=404, detail="no such credential")


@router.get("", response_class=HTMLResponse)
def index(
    request: Request,
    toggled: Annotated[str | None, Query(max_length=64)] = None,
    queued: Annotated[int | None, Query()] = None,
    pending: Annotated[int | None, Query()] = None,
    saved: Annotated[str | None, Query(max_length=64)] = None,
    reset: Annotated[str | None, Query(max_length=64)] = None,
    credential: Annotated[str | None, Query(max_length=64)] = None,
    cleared: Annotated[str | None, Query(max_length=64)] = None,
) -> Response:
    flash = None
    if toggled is not None:
        flash = f"Updated {toggled}."
    elif queued is not None:
        flash = f"Run queued (job #{queued})."
    elif pending is not None:
        flash = f"A run is already queued (job #{pending})."
    elif saved is not None:
        flash = f"Saved the settings for {saved}."
    elif reset is not None:
        flash = f"{reset} is back to its defaults."
    elif credential is not None:
        flash = f"Saved the {_credential(request, credential).label}."
    elif cleared is not None:
        flash = f"Cleared the {_credential(request, cleared).label}."
    return _render(request, flash=flash)


# --- enable switch ------------------------------------------------------------------


@router.post("/{name}/enabled", response_class=HTMLResponse)
def set_enabled(request: Request, name: str, enabled: Annotated[bool, Form()] = False) -> Response:
    """``enabled`` is absent when the switch's checkbox is off, which means off."""
    row = _row(request, name)
    if row.pinned:
        return _render(
            request,
            error=f"{name} is turned {'on' if row.enabled else 'off'} in SOURCES_ADAPTERS.",
            status_code=409,
        )
    if row.disabled_reason is not None:
        return _render(request, error=f"{name} can't run: {row.disabled_reason}", status_code=409)
    store = SqlSourceStore(_factory(request))
    store.ensure([name])
    store.set_enabled(name, enabled)
    return RedirectResponse(f"/sources?toggled={name}", status_code=303)


# --- credentials --------------------------------------------------------------------


@router.post("/credentials/{key}", response_class=HTMLResponse)
def save_credential(
    request: Request, key: str, value: Annotated[str, Form(max_length=500)]
) -> Response:
    credential = _credential(request, key)
    token = value.strip()
    if not token:
        return _render(request, error=f"Enter the {credential.label}.", status_code=422)
    SqlSourceStore(_factory(request)).set_credential(key, token)
    return RedirectResponse(f"/sources?credential={key}", status_code=303)


@router.post("/credentials/{key}/clear", response_class=HTMLResponse)
def clear_credential(request: Request, key: str) -> Response:
    _credential(request, key)
    SqlSourceStore(_factory(request)).delete_credential(key)
    return RedirectResponse(f"/sources?cleared={key}", status_code=303)


# --- per-source settings ------------------------------------------------------------


@router.post("/{name}/settings", response_class=HTMLResponse)
def save_settings(
    request: Request,
    name: str,
    search_locations: Annotated[str, Form(max_length=MAX_FIELD_LENGTH)] = "",
    interval_minutes: Annotated[str, Form(max_length=20)] = "",
    max_results: Annotated[str, Form(max_length=20)] = "",
) -> Response:
    row = _row(request, name)
    form = {
        "search_locations": search_locations,
        "interval_minutes": interval_minutes.strip(),
        "max_results": max_results.strip(),
    }
    if not row.can_edit:
        return _render(
            request, error=f"{name} can't be configured: {row.disabled_reason}", status_code=409
        )
    pinned = [
        field for field in SETTINGS_FIELDS if form[field].strip() and field in row.config.pinned
    ]
    if pinned:
        return _render(
            request,
            error=f"{', '.join(pinned)} for {name} is set in SOURCES_ADAPTERS.",
            status_code=409,
            form_source=name,
            form=form,
        )
    try:
        overrides = _parse_settings(form)
    except ValidationError as exc:
        errors = {".".join(str(part) for part in e["loc"][:1]): _message(e) for e in exc.errors()}
        return _render(
            request,
            error=f"The settings for {name} were not saved. Fix the marked fields.",
            status_code=422,
            errors=errors,
            form_source=name,
            form=form,
        )
    store = SqlSourceStore(_factory(request))
    store.ensure([name])
    store.set_overrides(name, overrides)
    return RedirectResponse(f"/sources?saved={name}", status_code=303)


@router.post("/{name}/settings/use-criteria-cities", response_class=HTMLResponse)
def use_criteria_cities(request: Request, name: str) -> Response:
    """Fill an empty ``search_locations`` with the criteria's cities. A click, never implicit."""
    row = _row(request, name)
    if not row.can_edit:
        return _render(
            request, error=f"{name} can't be configured: {row.disabled_reason}", status_code=409
        )
    if not row.criteria_cities:
        return _render(
            request,
            error=f"No criteria cities to use for {name}: it already has locations, "
            "takes site names, or the criteria have no city areas.",
            status_code=409,
        )
    store = SqlSourceStore(_factory(request))
    store.ensure([name])
    store.set_overrides(
        name, row.overrides.model_copy(update={"search_locations": row.criteria_cities})
    )
    return RedirectResponse(f"/sources?saved={name}", status_code=303)


@router.post("/{name}/settings/reset", response_class=HTMLResponse)
def reset_settings(request: Request, name: str) -> Response:
    row = _row(request, name)
    if not row.can_edit:
        return _render(
            request, error=f"{name} can't be configured: {row.disabled_reason}", status_code=409
        )
    store = SqlSourceStore(_factory(request))
    store.ensure([name])
    store.set_overrides(name, None)
    return RedirectResponse(f"/sources?reset={name}", status_code=303)


def _parse_settings(form: Mapping[str, str]) -> SourceOverrides:
    """The form's fields as :class:`SourceOverrides`; an empty field is unset."""
    # One per line: place names carry commas ("Springfield, IL").
    locations = tuple(
        line.strip() for line in form["search_locations"].splitlines() if line.strip()
    )
    values: dict[str, object] = {}
    if locations:
        values["search_locations"] = locations
    for field in ("interval_minutes", "max_results"):
        if form[field]:
            values[field] = form[field]
    return SourceOverrides.model_validate(values)


def _message(error: Mapping[str, object]) -> str:
    kind = str(error.get("type", ""))
    if kind in ("int_parsing", "int_type", "int_from_float"):
        return "enter a whole number"
    if kind == "greater_than":
        return "enter a number of 1 or more"
    return str(error.get("msg", "invalid value"))


# --- run now --------------------------------------------------------------------------


@router.post("/{name}/run", response_class=HTMLResponse)
def run_now(request: Request, name: str) -> Response:
    row = _row(request, name)
    if not row.runnable:
        reason = row.disabled_reason or "it is turned off"
        return _render(request, error=f"{name} can't run now: {reason}.", status_code=409)
    with session_scope(_factory(request)) as session:
        existing = queue.pending(session, RUN_SOURCE, source=name)
        if existing is not None:
            return RedirectResponse(f"/sources?pending={existing.id}", status_code=303)
        job = queue.enqueue(session, RUN_SOURCE, {"source": name}, max_attempts=1)
        job_id = job.id
    return RedirectResponse(f"/sources?queued={job_id}", status_code=303)
