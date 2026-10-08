"""``GET /metrics``: Prometheus scrape endpoint, mounted only when ``METRICS_ENABLED``."""

from fastapi import APIRouter, Depends, Request, Response

from furnest.web.auth import require_bearer
from furnest.web.metrics import CONTENT_TYPE, render_metrics

router = APIRouter(tags=["metrics"], dependencies=[Depends(require_bearer)])


@router.get("/metrics")
def metrics(request: Request) -> Response:
    return Response(render_metrics(request.app.state.engine), media_type=CONTENT_TYPE)
