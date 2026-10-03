"""Security headers added to every response.

The CSP allows only same-origin scripts, styles, images and form targets, and
forbids framing. That is why htmx is vendored instead of loaded from a CDN, and
why ``base.html`` tells htmx not to inject inline indicator styles or ``eval``
code. HSTS is the reverse proxy's job (``deploy/Caddyfile.example``, #9), since
only it knows whether the site is served over HTTPS.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
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


def apply_security_headers(headers: MutableHeaders) -> None:
    """Set each security header unless the response already chose its own value."""
    for name, value in SECURITY_HEADERS.items():
        headers.setdefault(name, value)


class SecurityHeadersMiddleware:
    """Pure ASGI middleware, so streaming and static responses are not buffered."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                apply_security_headers(MutableHeaders(scope=message))
            await send(message)

        await self.app(scope, receive, send_with_headers)
