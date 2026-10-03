"""Dashboard pages. Only the shell exists until P4 fills it in. All need auth."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from furnest.web.auth import require_auth
from furnest.web.csrf import require_csrf
from furnest.web.templating import templates

router = APIRouter(dependencies=[Depends(require_auth), Depends(require_csrf)])


@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index(request: Request) -> Response:
    return templates.TemplateResponse(request, "index.html")
