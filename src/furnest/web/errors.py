"""Error responses: HTML pages for browsers, JSON for everything else (and for
the JSON API under ``/api/v1``, whoever asks).

Neither ever includes a stack trace. Unhandled exceptions are logged here (and
reach Sentry through its FastAPI integration when it is enabled).

Starlette runs the 500 handler in ``ServerErrorMiddleware``, which sits outside all
user middleware, so that response gets its security headers set here directly.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response

from furnest.logging import get_logger
from furnest.web.headers import apply_security_headers
from furnest.web.templating import templates

log = get_logger(__name__)

_PAGES = {404: "errors/404.html"}


# The JSON API answers in JSON whatever the client accepts.
_API_PREFIX = "/api/v1/"


def _wants_html(request: Request) -> bool:
    if request.url.path.startswith(_API_PREFIX):
        return False
    return "text/html" in request.headers.get("accept", "")


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, HTTPException):  # pragma: no cover - registration guarantees it
        raise exc
    if _wants_html(request) and exc.status_code in _PAGES:
        return templates.TemplateResponse(
            request, _PAGES[exc.status_code], status_code=exc.status_code, headers=exc.headers
        )
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    log.error("unhandled_exception", method=request.method, path=request.url.path, exc_info=exc)
    response: Response
    if _wants_html(request):
        response = templates.TemplateResponse(request, "errors/500.html", status_code=500)
    else:
        response = JSONResponse({"detail": "Internal Server Error"}, status_code=500)
    apply_security_headers(response.headers)
    return response


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
