"""Dashboard pages. Only the shell exists until P4 fills it in."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from furnest.web.templating import templates

router = APIRouter()


@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index(request: Request) -> Response:
    return templates.TemplateResponse(request, "index.html")
