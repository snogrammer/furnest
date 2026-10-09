"""The settings page: integrations, dashboard-editable settings and every env setting.

All need auth and CSRF. Only settings in :data:`furnest.config.RUNTIME_SETTINGS` can
be changed here. Env settings are shown read-only, because each process reads them
once at start. Forms post normally and redirect back with a flash message. A setting
pinned by its env var answers 409 with the page and the reason, an invalid value
answers 422, and an unknown setting is a 404.

Handlers are plain ``def`` so FastAPI runs the blocking SQLite calls in its threadpool.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from starlette.responses import Response

from furnest.config import (
    RUNTIME_SETTINGS,
    RuntimeSetting,
    RuntimeSettings,
    env_groups,
    env_var_name,
)
from furnest.db.session import make_session_factory
from furnest.db.settings_store import SqlSettingsStore
from furnest.logging import get_logger
from furnest.web.auth import clear_session_cookie, client_key, require_auth
from furnest.web.csrf import require_csrf
from furnest.web.settings_view import build_groups, build_integrations, build_runtime_rows
from furnest.web.templating import templates

log = get_logger(__name__)

router = APIRouter(prefix="/settings", dependencies=[Depends(require_auth), Depends(require_csrf)])


def _runtime(request: Request) -> RuntimeSettings:
    factory = make_session_factory(request.app.state.engine)
    return RuntimeSettings(request.app.state.settings, SqlSettingsStore(factory))


def _render(
    request: Request, *, flash: str | None = None, error: str | None = None, status_code: int = 200
) -> Response:
    settings = request.app.state.settings
    context = {
        "integrations": build_integrations(settings),
        "runtime_rows": build_runtime_rows(_runtime(request)),
        "groups": build_groups(settings),
        "flash": flash,
        "error": error,
    }
    return templates.TemplateResponse(
        request, "settings/index.html", context, status_code=status_code
    )


def _setting(key: str) -> RuntimeSetting[Any]:
    setting = RUNTIME_SETTINGS.get(key)
    if setting is None:
        raise HTTPException(status_code=404, detail="no such setting")
    return setting


def _env_var(setting: RuntimeSetting[Any]) -> str:
    return env_var_name(env_groups()[setting.group], setting.field)


@router.get("", response_class=HTMLResponse)
def index(
    request: Request,
    saved: Annotated[str | None, Query(max_length=64)] = None,
    reset: Annotated[str | None, Query(max_length=64)] = None,
) -> Response:
    flash = None
    if saved is not None:
        flash = f"Saved {saved}."
    elif reset is not None:
        flash = f"{reset} is back to its default."
    return _render(request, flash=flash)


@router.post("/sessions/revoke")
def revoke_sessions(request: Request) -> Response:
    """Log out everywhere: every session cookie issued so far stops working."""
    request.app.state.authenticator.revoke_all()
    log.info("sessions_revoked", client_ip=client_key(request))
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(request, response)
    return response


@router.post("/runtime/{key}", response_class=HTMLResponse)
def save(request: Request, key: str, value: Annotated[str, Form(max_length=200)]) -> Response:
    setting = _setting(key)
    runtime = _runtime(request)
    if runtime.env_pinned(setting):
        return _render(request, error=f"{key} is set by {_env_var(setting)}.", status_code=409)
    try:
        runtime.set(setting, value)
    except ValidationError:
        return _render(request, error=f"{value!r} is not a valid {key}.", status_code=422)
    return RedirectResponse(f"/settings?saved={key}", status_code=303)


@router.post("/runtime/{key}/reset", response_class=HTMLResponse)
def reset_setting(request: Request, key: str) -> Response:
    setting = _setting(key)
    _runtime(request).reset(setting)
    return RedirectResponse(f"/settings?reset={key}", status_code=303)
