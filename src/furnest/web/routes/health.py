"""``/healthz`` (liveness) and ``/readyz`` (readiness). Both are unauthenticated.

Both also answer ``HEAD``, which some uptime monitors and proxies probe with.
Handlers are plain ``def`` so FastAPI runs the blocking SQLite calls in its threadpool.
"""

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from furnest.config import Settings
from furnest.web.health import check_readiness

router = APIRouter(tags=["health"])


@router.api_route("/healthz", methods=["GET", "HEAD"])
def healthz() -> dict[str, str]:
    """The process is up and serving. Never touches the database."""
    return {"status": "ok"}


@router.api_route("/readyz", methods=["GET", "HEAD"])
def readyz(request: Request) -> JSONResponse:
    """200 when the database, schema and worker are all healthy, else 503."""
    settings: Settings = request.app.state.settings
    report = check_readiness(
        request.app.state.engine,
        max_heartbeat_age=timedelta(seconds=settings.worker.heartbeat_max_age_seconds),
    )
    body: dict[str, Any] = {
        "status": "ok" if report.ok else "fail",
        "checks": {name: {"ok": c.ok, "detail": c.detail} for name, c in report.checks.items()},
    }
    return JSONResponse(body, status_code=200 if report.ok else 503)
