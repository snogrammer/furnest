"""The JSON API, for scripts and automation: properties, their listings, status
transitions and notes, under ``/api/v1``.

Every route needs ``Authorization: Bearer <AUTH_TOKEN>`` (:func:`require_bearer`);
the dashboard's session cookie is not accepted, so there is no CSRF to handle.
Errors are JSON ``{"detail": ...}``: 401 without a valid token, 404 for an unknown
property, 409 when the state machine forbids a transition (``detail`` says why),
422 for an invalid body or note. Changes are recorded with the ``api`` actor.

The API is described by an OpenAPI schema at ``/api/openapi.json`` and browsable
with Swagger UI at ``/api/docs``. Both need auth too, but also accept the session
cookie so a logged-in browser can open them. Swagger UI is vendored (the CSP
forbids CDNs) and started by ``static/js/api-docs.js`` (no inline scripts).

Handlers are plain ``def`` so FastAPI runs the blocking SQLite calls in its threadpool.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer
from starlette.responses import Response

from furnest.db.base import utcnow
from furnest.db.property_store import (
    PropertyMergedError,
    PropertyNotFoundError,
    PropertySort,
    SqlPropertyStore,
)
from furnest.db.session import make_session_factory
from furnest.domain import Actor, InvalidTransitionError, PropertyStatus
from furnest.web.api_models import (
    ListingOut,
    NoteIn,
    NoteOut,
    PropertyDetailOut,
    PropertyOut,
    PropertyPageOut,
    TransitionIn,
)
from furnest.web.auth import require_auth, require_bearer
from furnest.web.templating import templates

# The most properties one GET /properties returns.
MAX_LIMIT = 100

# Documents the Bearer scheme in the OpenAPI schema; require_bearer does the checking.
_bearer_scheme = HTTPBearer(auto_error=False, description="The AUTH_TOKEN setting.")


def _error(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {"detail": {"type": "string"}},
                    "required": ["detail"],
                }
            }
        },
    }


Responses = dict[int | str, dict[str, Any]]

_UNAUTHORIZED: Responses = {401: _error("Missing or wrong Bearer token")}
_NOT_FOUND: Responses = {404: _error("No such property")}

router = APIRouter(
    prefix="/api/v1",
    tags=["properties"],
    dependencies=[Depends(_bearer_scheme), Depends(require_bearer)],
    responses=_UNAUTHORIZED,
)


def _store(request: Request) -> SqlPropertyStore:
    return SqlPropertyStore(make_session_factory(request.app.state.engine))


def _not_found(property_id: int) -> HTTPException:
    return HTTPException(status_code=404, detail=f"no property with id {property_id}")


def _merged(e: PropertyMergedError) -> HTTPException:
    return HTTPException(
        status_code=409, detail=f"property {e.property_id} was merged into {e.merged_into}"
    )


@router.get("/properties", response_model=PropertyPageOut)
def list_properties(
    request: Request,
    status: Annotated[
        list[PropertyStatus] | None, Query(description="Repeat for several; all if omitted.")
    ] = None,
    q: Annotated[str, Query(max_length=200, description="Substring of the title or address.")] = "",
    sort: PropertySort = PropertySort.SCORE,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_snoozed: bool = False,
) -> PropertyPageOut:
    """Properties with their primary listing, one page at a time."""
    page = _store(request).search(
        now=utcnow(),
        limit=limit,
        offset=offset,
        statuses=status,
        text=q.strip() or None,
        sort=sort,
        include_snoozed=include_snoozed,
    )
    return PropertyPageOut.build(page, limit=limit, offset=offset)


@router.get("/properties/{property_id}", response_model=PropertyDetailOut, responses=_NOT_FOUND)
def get_property(request: Request, property_id: int) -> PropertyDetailOut:
    """Everything stored about one property: match, listings, audit trail and notes."""
    detail = _store(request).detail(property_id)
    if detail is None:
        raise _not_found(property_id)
    return PropertyDetailOut.build(detail)


@router.get(
    "/properties/{property_id}/listings", response_model=list[ListingOut], responses=_NOT_FOUND
)
def list_listings(request: Request, property_id: int) -> list[ListingOut]:
    """The property's listings, primary (active) first, with their price history."""
    detail = _store(request).detail(property_id)
    if detail is None:
        raise _not_found(property_id)
    return [ListingOut.build_detail(listing) for listing in detail.listings]


@router.post(
    "/properties/{property_id}/transitions",
    response_model=PropertyOut,
    responses={
        **_NOT_FOUND,
        409: _error("The state machine forbids this transition, or the property was merged"),
    },
)
def transition(request: Request, property_id: int, body: TransitionIn) -> PropertyOut:
    """Move the property to another status.

    ``allowed_transitions`` on the property lists what is accepted. Denying may
    carry a ``reason``, saved as a note; other transitions don't take one.
    """
    if body.reason is not None and body.to is not PropertyStatus.DENIED:
        raise HTTPException(status_code=422, detail="only a deny takes a reason")
    store = _store(request)
    try:
        if body.to is PropertyStatus.DENIED:
            state = store.deny(property_id, actor=Actor.API, at=utcnow(), reason=body.reason)
        else:
            state = store.transition(property_id, body.to, actor=Actor.API, at=utcnow())
    except PropertyMergedError as e:
        raise _merged(e) from None
    except PropertyNotFoundError:
        raise _not_found(property_id) from None
    except InvalidTransitionError as e:
        raise HTTPException(status_code=409, detail=e.reason) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return PropertyOut.build(state)


@router.post(
    "/properties/{property_id}/notes",
    response_model=NoteOut,
    status_code=201,
    responses={**_NOT_FOUND, 409: _error("The property was merged into another")},
)
def add_note(request: Request, property_id: int, body: NoteIn) -> NoteOut:
    """Add a markdown note to the property."""
    try:
        note = _store(request).add_note(property_id, body.body, actor=Actor.API, at=utcnow())
    except PropertyMergedError as e:
        raise _merged(e) from None
    except PropertyNotFoundError:
        raise _not_found(property_id) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return NoteOut.build(note)


docs_router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


@docs_router.get("/openapi.json", include_in_schema=False)
def openapi(request: Request) -> dict[str, Any]:
    schema: dict[str, Any] = request.app.openapi()
    return schema


@docs_router.get("/docs", response_class=HTMLResponse, include_in_schema=False)
def docs(request: Request) -> Response:
    return templates.TemplateResponse(request, "api/docs.html")
