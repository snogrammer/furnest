"""``/login`` and ``/logout``. Both are reachable without a session, both need CSRF.

Failed logins count against the client IP's rate limit. Once it is exhausted,
``POST /login`` answers 429 without even looking at the submitted token, so a
lockout cannot be used to keep guessing. A successful login clears the count and
rotates the CSRF token.
"""

import math
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from furnest.logging import get_logger
from furnest.web.auth import (
    auth_method,
    clear_session_cookie,
    safe_next,
    set_session_cookie,
)
from furnest.web.csrf import new_token, require_csrf, set_csrf_cookie
from furnest.web.ratelimit import SlidingWindowLimiter
from furnest.web.templating import templates

log = get_logger(__name__)

router = APIRouter(dependencies=[Depends(require_csrf)])


def _client_ip(request: Request) -> str:
    # The socket peer, or the forwarded client when it came through a trusted proxy
    # (ProxyHeadersMiddleware rewrites the scope before we get here).
    return request.client.host if request.client else "unknown"


def _login_page(request: Request, next_path: str, *, error: str | None, status: int) -> Response:
    return templates.TemplateResponse(
        request, "login.html", {"next": next_path, "error": error}, status_code=status
    )


@router.api_route("/login", methods=["GET", "HEAD"], response_class=HTMLResponse)
def login_form(
    request: Request, next_path: Annotated[str | None, Query(alias="next")] = None
) -> Response:
    target = safe_next(next_path)
    if auth_method(request) is not None:
        return RedirectResponse(target, status_code=303)
    return _login_page(request, target, error=None, status=200)


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    token: Annotated[str, Form()] = "",
    next_path: Annotated[str | None, Form(alias="next")] = None,
) -> Response:
    target = safe_next(next_path)
    limiter: SlidingWindowLimiter = request.app.state.login_limiter
    ip = _client_ip(request)

    retry_after = limiter.retry_after(ip)
    if retry_after is not None:
        log.warning("login_rate_limited", client_ip=ip)
        response = _login_page(
            request, target, error="Too many failed attempts. Try again later.", status=429
        )
        response.headers["Retry-After"] = str(max(1, math.ceil(retry_after)))
        return response

    if not request.app.state.authenticator.token_matches(token):
        limiter.hit(ip)
        log.warning("login_failed", client_ip=ip)
        return _login_page(request, target, error="That token is not valid.", status=401)

    limiter.reset(ip)
    log.info("login_succeeded", client_ip=ip)
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(request, response)
    set_csrf_cookie(request, response, new_token())
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(request, response)
    set_csrf_cookie(request, response, new_token())
    log.info("logout", client_ip=_client_ip(request))
    return response
