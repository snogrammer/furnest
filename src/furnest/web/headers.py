"""Security headers added to every response.

The CSP allows only same-origin scripts, styles and form targets, and forbids
framing. That is why htmx is vendored instead of loaded from a CDN, and why
``base.html`` tells htmx not to inject inline indicator styles or ``eval`` code.
Images may also come from any ``https:`` origin. Pages normally load listing photos
through the same-origin ``/photo`` proxy (#212), but with ``IMAGES_PROXY_ENABLED``
off they hot-link the sources' CDNs, with ``referrerpolicy="no-referrer"``. HSTS is
the reverse proxy's job, since only it knows whether the site is served over HTTPS;
``deploy/Caddyfile.example`` sets it, along with a request body size limit.

Every response except static files (``/static``, the favicon) is also
``Cache-Control: no-store``: pages and API answers hold the user's data and should
not linger in browser or proxy caches after logout.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data: https:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


# Paths whose responses may be cached: the same files for every user.
_CACHEABLE = ("/static/", "/favicon.ico")


def apply_security_headers(headers: MutableHeaders, *, cacheable: bool = False) -> None:
    """Set each security header unless the response already chose its own value."""
    for name, value in SECURITY_HEADERS.items():
        headers.setdefault(name, value)
    if not cacheable:
        headers.setdefault("Cache-Control", "no-store")


class SecurityHeadersMiddleware:
    """Pure ASGI middleware, so streaming and static responses are not buffered."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cacheable = scope["path"].startswith(_CACHEABLE)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                apply_security_headers(MutableHeaders(scope=message), cacheable=cacheable)
            await send(message)

        await self.app(scope, receive, send_with_headers)
