"""FastAPI application factory.

``create_app`` builds a fresh app per call so tests can inject their own settings
and engine. The lifespan owns the database engine: it creates one from settings
(unless one was injected), runs migrations first when ``DB_AUTO_MIGRATE`` is set,
and disposes it on shutdown. Routes reach the engine and settings through
``request.app.state``.

The interactive API docs are disabled: Swagger UI loads from a CDN, which the
self-only CSP forbids, and the API is documented in the README instead.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine

from furnest import __version__
from furnest.config import Settings, get_settings
from furnest.db.engine import create_engine_from_settings
from furnest.db.migrate import upgrade
from furnest.logging import get_logger
from furnest.web.errors import register_error_handlers
from furnest.web.headers import SecurityHeadersMiddleware
from furnest.web.routes import health, pages
from furnest.web.templating import STATIC_DIR

log = get_logger(__name__)


def create_app(settings: Settings | None = None, *, engine: Engine | None = None) -> FastAPI:
    settings = settings or get_settings()

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
    app.add_middleware(SecurityHeadersMiddleware)
    register_error_handlers(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health.router)
    app.include_router(pages.router)
    return app
