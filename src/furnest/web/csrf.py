"""CSRF protection, in two layers.

1. A token. With a session, the token is an HMAC of the session id (see
   ``Authenticator.csrf_token_for``): it only works for that session, so a pair
   fetched elsewhere and planted as cookies (cookie tossing from a sibling
   subdomain) is useless. Nothing is stored; the server recomputes it. Before
   there is a session (the login form) a signed double-submit cookie is used:
   ``furnest_csrf`` holds a random token signed with ``AUTH_SESSION_SECRET``. Over
   HTTPS the cookie is named ``__Host-furnest_csrf``, which browsers refuse to
   accept from a sibling subdomain; plain-HTTP local dev falls back to the bare
   name. Pages echo the token in a hidden ``csrf_token`` form field (``csrf_field``
   macro) and, for htmx requests, in the ``X-CSRF-Token`` header (``hx-headers`` on
   ``<body>``).
2. ``Sec-Fetch-Site`` / ``Origin``: an unsafe request that a browser marks as
   cross-site, or whose ``Origin`` is not this host, is refused before the token
   is looked at. Requests carrying neither header (non-browser clients) rely on
   the token alone.

Requests authenticated with a valid Bearer token are exempt: browsers never attach
that header on their own, so there is nothing to forge. A wrong one gets no pass.

``CsrfMiddleware`` reads the pre-session cookie at the start of each request. A
template that needs a pre-session token and finds none mints one, and the middleware
then sets the cookie on that response. Nothing else gets a cookie, so health checks
and static files stay cookie-free.
"""

import hmac
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer
from starlette.datastructures import MutableHeaders
from starlette.requests import cookie_parser
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from furnest.web.auth import (
    HOST_PREFIX,
    auth_method,
    cookie_secure,
    session_cookie_value,
)

CSRF_COOKIE = "furnest_csrf"
_CROSS_SITE_OK = frozenset({"same-origin", "none"})
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
_SALT = "furnest.csrf"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Keys in the ASGI scope's state dict (what ``request.state`` reads and writes).
_FROM_COOKIE = "csrf_cookie_token"  # token from a validly signed cookie, or None
_CURRENT = "csrf_token"  # token templates render; minted if the cookie had none
_ISSUE = "csrf_issue"  # set when a new token must be sent as a cookie


def csrf_cookie_name(secure: bool) -> str:
    return HOST_PREFIX + CSRF_COOKIE if secure else CSRF_COOKIE


class CsrfSigner:
    def __init__(self, secret: str) -> None:
        self._serializer = URLSafeSerializer(secret, salt=_SALT)

    def sign(self, token: str) -> str:
        return self._serializer.dumps(token)

    def unsign(self, cookie: str | None) -> str | None:
        if not cookie:
            return None
        try:
            token = self._serializer.loads(cookie)
        except BadSignature:
            return None
        return token if isinstance(token, str) and token else None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _session_token(request: Request) -> str | None:
    token: str | None = request.app.state.authenticator.csrf_token_for(
        session_cookie_value(request)
    )
    return token


def csrf_token(request: Request) -> str:
    """The token to embed in this page; mints a pre-session one (and cookie) if needed."""
    bound = _session_token(request)
    if bound is not None:
        return bound
    token: str | None = getattr(request.state, _CURRENT, None)
    if token is None:
        token = new_token()
        setattr(request.state, _CURRENT, token)
        setattr(request.state, _ISSUE, True)
    return token


def _set_cookie(response: Response, signer: CsrfSigner, token: str, *, secure: bool) -> None:
    response.set_cookie(
        csrf_cookie_name(secure),
        signer.sign(token),
        path="/",
        httponly=True,
        samesite="strict",
        secure=secure,
    )


def set_csrf_cookie(request: Request, response: Response, token: str) -> None:
    """Send ``token`` as the CSRF cookie, e.g. to rotate it on login and logout."""
    _set_cookie(response, request.app.state.csrf_signer, token, secure=cookie_secure(request))


def clear_csrf_cookie(request: Request, response: Response) -> None:
    """Drop the pre-session cookie, e.g. once login has bound the token to a session."""
    secure = cookie_secure(request)
    response.delete_cookie(
        csrf_cookie_name(secure), path="/", httponly=True, samesite="strict", secure=secure
    )


def _same_origin(request: Request) -> bool:
    """The browser-supplied provenance headers, when present, say same-origin."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site.lower() not in _CROSS_SITE_OK:
        return False
    origin = request.headers.get("origin")
    if origin is not None:
        host = request.headers.get("host", "")
        return urlsplit(origin).netloc.lower() == host.lower() and bool(host)
    return True


async def require_csrf(request: Request) -> None:
    """Dependency: reject unsafe requests whose token does not match their cookie."""
    if request.method in _SAFE_METHODS or auth_method(request) == "bearer":
        # A valid Bearer token can't be forged cross-site (browsers never attach
        # it on their own). A wrong one gets no exemption.
        return
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="Cross-site request refused")
    expected = _session_token(request) or getattr(request.state, _FROM_COOKIE, None)
    supplied = request.headers.get(CSRF_HEADER)
    if supplied is None and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        value = (await request.form()).get(CSRF_FIELD)
        supplied = value if isinstance(value, str) else None
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


class CsrfMiddleware:
    """Loads the CSRF cookie into request state and sets it when a page minted one."""

    def __init__(self, app: ASGIApp, signer: CsrfSigner, *, cookie_secure: bool) -> None:
        self.app = app
        self.signer = signer
        self.cookie_secure = cookie_secure

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        secure = self.cookie_secure or scope.get("scheme") == "https"
        token = self.signer.unsign(_read_cookie(scope, csrf_cookie_name(secure)))
        state[_FROM_COOKIE] = token
        state[_CURRENT] = token
        state[_ISSUE] = False

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start" and state.get(_ISSUE):
                carrier = Response()
                _set_cookie(carrier, self.signer, state[_CURRENT], secure=secure)
                headers = MutableHeaders(scope=message)
                for value in carrier.headers.getlist("set-cookie"):
                    headers.append("set-cookie", value)
            await send(message)

        await self.app(scope, receive, send_with_cookie)


def _read_cookie(scope: Scope, name: str) -> str | None:
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            return cookie_parser(value.decode("latin-1")).get(name)
    return None
