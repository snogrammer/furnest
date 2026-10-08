"""Dashboard and API authentication with the single ``AUTH_TOKEN``.

A request is authenticated in one of two ways:

* ``Authorization: Bearer <AUTH_TOKEN>``, for scripts and the JSON API;
* the ``furnest_session`` cookie, set by ``POST /login`` after the token was typed
  into the login form.

The session cookie is signed with ``AUTH_SESSION_SECRET`` (itsdangerous) and
carries a keyed fingerprint of the token, so rotating either ``AUTH_TOKEN`` or the
secret logs every browser out. Sessions are stateless: logout deletes the cookie,
it does not revoke it server side. Token comparison is constant time.

Routers opt in with ``dependencies=[Depends(require_auth)]``. An unauthenticated
browser page load is redirected to ``/login``; everything else gets a 401. The JSON
API uses :func:`require_bearer` instead: Bearer only, so the session cookie (and
with it CSRF) never comes into play there.

A wrong Bearer token counts against the client's rate limit, the same budget as
failed logins on ``/login``. Once it is spent, Bearer requests get a 429 with
``Retry-After`` without the token being checked, so a lockout can't be guessed past.
"""

import hashlib
import hmac
import ipaddress
import math
from typing import Literal
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import SecretStr
from starlette.responses import Response

from furnest.config import AuthSettings, Settings
from furnest.logging import get_logger
from furnest.web.ratelimit import SlidingWindowLimiter

log = get_logger(__name__)

SESSION_COOKIE = "furnest_session"
_SESSION_SALT = "furnest.session"
_SESSION_VERSION = 1
_MIN_SECRET_LENGTH = 16
_PLACEHOLDER_PREFIX = "change-me"

AuthMethod = Literal["bearer", "session"]


class AuthConfigError(RuntimeError):
    """``AUTH_TOKEN`` or ``AUTH_SESSION_SECRET`` is missing, too short or a placeholder."""


def _required_secret(name: str, secret: SecretStr | None) -> str:
    value = secret.get_secret_value() if secret is not None else ""
    if len(value) < _MIN_SECRET_LENGTH or value.startswith(_PLACEHOLDER_PREFIX):
        msg = (
            f"{name} must be set to a random value of at least {_MIN_SECRET_LENGTH} "
            'characters, e.g. python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
        raise AuthConfigError(msg)
    return value


class Authenticator:
    """Checks tokens and issues/verifies session cookies. Lives on ``app.state``."""

    def __init__(self, auth: AuthSettings) -> None:
        # Fail closed: no dashboard without usable secrets.
        self._token = _required_secret("AUTH_TOKEN", auth.token).encode()
        secret = _required_secret("AUTH_SESSION_SECRET", auth.session_secret)
        self._fingerprint = hmac.new(secret.encode(), self._token, hashlib.sha256).hexdigest()
        self._serializer = URLSafeTimedSerializer(secret, salt=_SESSION_SALT)
        self.max_age = auth.session_max_age_seconds
        self.secret = secret

    def token_matches(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), self._token)

    def issue_session(self) -> str:
        return self._serializer.dumps({"v": _SESSION_VERSION, "tf": self._fingerprint})

    def session_valid(self, cookie: str | None) -> bool:
        if not cookie:
            return False
        try:
            payload = self._serializer.loads(cookie, max_age=self.max_age)
        except BadSignature:  # also covers SignatureExpired
            return False
        return (
            isinstance(payload, dict)
            and payload.get("v") == _SESSION_VERSION
            and isinstance(payload.get("tf"), str)
            and hmac.compare_digest(payload["tf"], self._fingerprint)
        )


def _authenticator(request: Request) -> Authenticator:
    authenticator: Authenticator = request.app.state.authenticator
    return authenticator


def bearer_token(request: Request) -> str | None:
    """The token from an ``Authorization: Bearer`` header, if one was sent."""
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return credentials.strip()


def auth_method(request: Request) -> AuthMethod | None:
    """How the request is authenticated, or ``None``.

    A Bearer header, when present, decides on its own: a wrong token is not rescued
    by a valid cookie, so API clients get a clear 401.
    """
    authenticator = _authenticator(request)
    token = bearer_token(request)
    if token is not None:
        return "bearer" if authenticator.token_matches(token) else None
    if authenticator.session_valid(request.cookies.get(SESSION_COOKIE)):
        return "session"
    return None


def client_key(request: Request) -> str:
    """The rate-limit key for the client: its IP address, or its /64 for IPv6.

    The address is the socket peer, or the forwarded client when the request came
    through a trusted proxy (ProxyHeadersMiddleware rewrites the scope first). One
    IPv6 host usually controls a whole /64, so keying by address would let it
    rotate past the limit.
    """
    host = request.client.host if request.client else "unknown"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((ip, 64), strict=False))
    return str(ip)


def auth_limiter(request: Request) -> SlidingWindowLimiter:
    """Failed token attempts per client, shared by ``/login`` and Bearer auth."""
    limiter: SlidingWindowLimiter = request.app.state.auth_limiter
    return limiter


def retry_after_header(seconds: float) -> dict[str, str]:
    return {"Retry-After": str(max(1, math.ceil(seconds)))}


def _authenticate(request: Request) -> AuthMethod | None:
    """:func:`auth_method`, with wrong Bearer tokens counted against the rate limit."""
    if bearer_token(request) is None:
        return auth_method(request)
    limiter = auth_limiter(request)
    key = client_key(request)
    retry_after = limiter.retry_after(key)
    if retry_after is not None:
        log.warning("bearer_rate_limited", client_ip=key)
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts",
            headers=retry_after_header(retry_after),
        )
    method = auth_method(request)
    if method is None:
        limiter.hit(key)
        log.warning("bearer_failed", client_ip=key)
    return method


class LoginRequired(Exception):  # noqa: N818 - control flow, not an error
    """Raised for a browser page load without a session; handled as a redirect."""

    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def require_auth(request: Request) -> None:
    """Dependency: let authenticated requests through, turn the rest away."""
    method = _authenticate(request)
    if method is not None:
        request.state.auth = method
        return
    if request.method in ("GET", "HEAD") and _wants_html(request):
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise LoginRequired(path)
    raise HTTPException(
        status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"}
    )


def require_bearer(request: Request) -> None:
    """Dependency: a valid ``Authorization: Bearer`` header, or a 401."""
    if _authenticate(request) == "bearer":
        request.state.auth = "bearer"
        return
    raise HTTPException(
        status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"}
    )


def safe_next(target: str | None) -> str:
    """``target`` if it is a same-site path, else ``/`` (no open redirects)."""
    if (
        not target
        or not target.startswith("/")
        or target.startswith(("//", "/\\"))
        or any(c in target for c in "\\\r\n\t")
    ):
        return "/"
    return target


def cookie_secure(request: Request) -> bool:
    """Mark cookies Secure when configured to, or whenever the request came over HTTPS."""
    settings: Settings = request.app.state.settings
    return settings.auth.cookie_secure or request.url.scheme == "https"


def set_session_cookie(request: Request, response: Response) -> None:
    authenticator = _authenticator(request)
    response.set_cookie(
        SESSION_COOKIE,
        authenticator.issue_session(),
        max_age=authenticator.max_age,
        path="/",
        httponly=True,
        samesite="lax",
        secure=cookie_secure(request),
    )


def clear_session_cookie(request: Request, response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=cookie_secure(request)
    )


async def login_required_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, LoginRequired):  # pragma: no cover - registration guarantees it
        raise exc
    return RedirectResponse(f"/login?{urlencode({'next': exc.next_path})}", status_code=303)


def register_auth_handlers(app: FastAPI) -> None:
    app.add_exception_handler(LoginRequired, login_required_handler)
