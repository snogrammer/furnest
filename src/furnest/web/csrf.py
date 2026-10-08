"""CSRF protection: a signed double-submit token.

The ``furnest_csrf`` cookie holds a random token signed with ``AUTH_SESSION_SECRET``.
Pages echo the raw token back in a hidden ``csrf_token`` form field (``csrf_field``
macro) and, for htmx requests, in the ``X-CSRF-Token`` header (``hx-headers`` on
``<body>``). An unsafe request passes only when what it sends matches the token in
its cookie. The signature stops a made-up cookie value from being accepted. It
does not stop a sibling subdomain from planting a validly signed pair fetched from
``/login``, since the token isn't bound to the session (#155).

The same mechanism guards the login form, so it works before there is a session.
Requests authenticated with a valid Bearer token are exempt: browsers never attach
that header on their own, so there is nothing to forge. A wrong one gets no pass.

``CsrfMiddleware`` reads the cookie at the start of each request. A template that
needs a token and finds none mints one, and the middleware then sets the cookie
on that response. Nothing else gets a cookie, so health checks and static files stay
cookie-free.
"""

import hmac
import secrets

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer
from starlette.datastructures import MutableHeaders
from starlette.requests import cookie_parser
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from furnest.web.auth import auth_method, cookie_secure

CSRF_COOKIE = "furnest_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
_SALT = "furnest.csrf"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Keys in the ASGI scope's state dict (what ``request.state`` reads and writes).
_FROM_COOKIE = "csrf_cookie_token"  # token from a validly signed cookie, or None
_CURRENT = "csrf_token"  # token templates render; minted if the cookie had none
_ISSUE = "csrf_issue"  # set when a new token must be sent as a cookie


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


def csrf_token(request: Request) -> str:
    """The token to embed in this page; mints one (and its cookie) when needed."""
    token: str | None = getattr(request.state, _CURRENT, None)
    if token is None:
        token = new_token()
        setattr(request.state, _CURRENT, token)
        setattr(request.state, _ISSUE, True)
    return token


def _set_cookie(response: Response, signer: CsrfSigner, token: str, *, secure: bool) -> None:
    response.set_cookie(
        CSRF_COOKIE, signer.sign(token), path="/", httponly=True, samesite="strict", secure=secure
    )


def set_csrf_cookie(request: Request, response: Response, token: str) -> None:
    """Send ``token`` as the CSRF cookie, e.g. to rotate it on login and logout."""
    _set_cookie(response, request.app.state.csrf_signer, token, secure=cookie_secure(request))


async def require_csrf(request: Request) -> None:
    """Dependency: reject unsafe requests whose token does not match their cookie."""
    if request.method in _SAFE_METHODS or auth_method(request) == "bearer":
        # A valid Bearer token can't be forged cross-site (browsers never attach
        # it on their own). A wrong one gets no exemption.
        return
    expected: str | None = getattr(request.state, _FROM_COOKIE, None)
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
        token = self.signer.unsign(_read_cookie(scope, CSRF_COOKIE))
        state[_FROM_COOKIE] = token
        state[_CURRENT] = token
        state[_ISSUE] = False

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start" and state.get(_ISSUE):
                carrier = Response()
                secure = self.cookie_secure or scope.get("scheme") == "https"
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
