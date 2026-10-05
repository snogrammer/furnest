"""The source health page: per-source status, the enable toggle and "run now".

All need auth and CSRF. Forms post normally and redirect back with a flash message,
so the page works without JavaScript. An action the source can't take (toggling a
source pinned in ``SOURCES_ADAPTERS``, running one that is off or can't run) answers
409 with the page and the reason. An unknown source is a 404.

"Run now" queues a ``run_source`` job for the worker (see
:class:`furnest.pipeline.schedule.RunSourceNow`). A source with one already queued
or running gets no second job.

Handlers are plain ``def`` so FastAPI runs the blocking SQLite calls in its threadpool.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import Response

from furnest.db.session import make_session_factory, session_scope
from furnest.db.source_store import SqlSourceStore
from furnest.pipeline.schedule import RUN_SOURCE
from furnest.web.auth import require_auth
from furnest.web.csrf import require_csrf
from furnest.web.source_health import SourceRow, build_row, build_rows
from furnest.web.templating import templates
from furnest.worker import queue

router = APIRouter(prefix="/sources", dependencies=[Depends(require_auth), Depends(require_csrf)])


def _factory(request: Request) -> sessionmaker[Session]:
    return make_session_factory(request.app.state.engine)


def _render(
    request: Request, *, flash: str | None = None, error: str | None = None, status_code: int = 200
) -> Response:
    rows = build_rows(
        request.app.state.source_registry, request.app.state.settings.sources, _factory(request)
    )
    context = {"rows": rows, "flash": flash, "error": error}
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


@router.get("", response_class=HTMLResponse)
def index(
    request: Request,
    toggled: Annotated[str | None, Query(max_length=64)] = None,
    queued: Annotated[int | None, Query()] = None,
    pending: Annotated[int | None, Query()] = None,
) -> Response:
    flash = None
    if toggled is not None:
        flash = f"Updated {toggled}."
    elif queued is not None:
        flash = f"Run queued (job #{queued})."
    elif pending is not None:
        flash = f"A run is already queued (job #{pending})."
    return _render(request, flash=flash)


@router.post("/{name}/enabled", response_class=HTMLResponse)
def set_enabled(request: Request, name: str, enabled: Annotated[bool, Form()]) -> Response:
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
