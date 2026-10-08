"""FastAPI application factory.

``create_app`` builds a fresh app per call so tests can inject their own settings
and engine. The lifespan owns the database engine: it creates one from settings
(unless one was injected), runs migrations first when ``DB_AUTO_MIGRATE`` is set,
and disposes it on shutdown. Routes reach the engine, settings and the source
adapter registry (injectable, for tests) through ``request.app.state``.

Every route needs auth (``furnest.web.auth``) except health probes, static files,
``/favicon.ico`` (browsers fetch it from the root, even on the login page),
login/logout and ``POST /discord/interactions``, which Discord calls and which
checks Discord's signature instead (``routes.discord``). ``create_app`` refuses to
build an app without usable ``AUTH_TOKEN``/``AUTH_SESSION_SECRET``.

``GET /metrics`` exists only when ``METRICS_ENABLED`` is set, and then needs the Bearer token.

Middleware, outermost first: trusted-proxy header rewriting (only when
``WEB_TRUSTED_PROXIES`` is set, so the client IP and scheme are right for everything
below), security headers, CSRF cookie handling.

FastAPI's built-in docs routes are off: their Swagger UI loads from a CDN, which
the self-only CSP forbids, and they would be public. ``routes.api`` serves the schema
and a vendored Swagger UI behind auth instead. Only the JSON API is in the schema;
the HTML routes are left out of it.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from furnest import __version__
from furnest.config import Settings, get_settings
from furnest.db.engine import create_engine_from_settings
from furnest.db.migrate import upgrade
from furnest.logging import get_logger
from furnest.sources.registry import SourceRegistry, discover
from furnest.web.auth import Authenticator, register_auth_handlers
from furnest.web.csrf import CsrfMiddleware, CsrfSigner
from furnest.web.discord_actions import register_buttons
from furnest.web.discord_dispatch import InteractionDispatcher
from furnest.web.errors import register_error_handlers
from furnest.web.headers import SecurityHeadersMiddleware
from furnest.web.ratelimit import SlidingWindowLimiter
from furnest.web.routes import (
    api,
    auth,
    criteria,
    discord,
    email_templates,
    health,
    inquiries,
    metrics,
    pages,
    sources,
)
from furnest.web.templating import STATIC_DIR

log = get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    source_registry: SourceRegistry | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    authenticator = Authenticator(settings.auth)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = engine is None
        app_engine = create_engine_from_settings(settings) if engine is None else engine
        try:
            if settings.db.auto_migrate:
                upgrade(app_engine)
                log.info("migrations_applied_on_start")
            app.state.engine = app_engine
            yield
        finally:
            if owned:
                app_engine.dispose()

    app = FastAPI(
        title="furnest",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.authenticator = authenticator
    app.state.source_registry = source_registry or discover()
    app.state.discord_dispatcher = dispatcher = InteractionDispatcher()
    register_buttons(dispatcher, settings)
    app.state.csrf_signer = csrf_signer = CsrfSigner(authenticator.secret)
    app.state.login_limiter = SlidingWindowLimiter(
        settings.auth.login_max_attempts, settings.auth.login_window_seconds
    )
    app.add_middleware(
        CsrfMiddleware, signer=csrf_signer, cookie_secure=settings.auth.cookie_secure
    )
    app.add_middleware(SecurityHeadersMiddleware)
    if settings.web.trusted_proxies:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.web.trusted_proxies)
    register_error_handlers(app)
    register_auth_handlers(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_api_route("/favicon.ico", favicon, include_in_schema=False)
    for html_router in (
        health.router,
        auth.router,
        pages.router,
        criteria.router,
        email_templates.router,
        inquiries.router,
        sources.router,
    ):
        app.include_router(html_router, include_in_schema=False)
    app.include_router(discord.router, include_in_schema=False)
    if settings.metrics.enabled:
        app.include_router(metrics.router, include_in_schema=False)
    app.include_router(api.router)
    app.include_router(api.docs_router)
    return app


def favicon() -> FileResponse:
    """The brand icon at the root path, where browsers look for it by default."""
    return FileResponse(STATIC_DIR / "brand" / "favicon.ico", media_type="image/x-icon")
