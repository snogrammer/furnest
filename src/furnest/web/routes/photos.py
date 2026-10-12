"""``GET /photo?src=<url>``: a listing photo, served from furnest's own origin.

Pages link photos through here instead of straight at the sources' CDNs. Privacy
blockers drop some of those CDNs (Airbnb's ``muscache.com``) before the request
leaves the browser, which left cards with no photo (#212). This route only serves
URLs some listing actually has (``listing_images``), so it can't be used as an open
proxy, and it fetches through :class:`~furnest.dedup.phash.ImageFetcher`: the SSRF
guard, robots.txt, the size limit and the timeouts all apply.

Only raster images are passed on; an SVG served from this origin could run script.
A photo that can't be fetched answers 502 with no body, and the page's ``photos.js``
swaps in a placeholder. Successful answers may be cached privately by the browser
(``IMAGES_PROXY_CACHE_SECONDS``). The fetcher is built on first use and kept on
``app.state.photo_fetcher``; tests put a fake there.
"""

from __future__ import annotations

from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import Response

from furnest.db.image_store import SqlImageStore
from furnest.db.session import make_session_factory
from furnest.dedup.phash import FetchedImage, ImageFetcher, ImageFetchError
from furnest.logging import get_logger
from furnest.web.auth import require_auth
from furnest.web.csrf import require_csrf

log = get_logger(__name__)

router = APIRouter(dependencies=[Depends(require_auth), Depends(require_csrf)])

RASTER_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"})
# A photo opened on its own still can't run anything or load anything.
PHOTO_CSP = "default-src 'none'; sandbox"


class PhotoFetcher(Protocol):
    """The part of :class:`ImageFetcher` the route needs."""

    def fetch_image(self, url: str) -> FetchedImage: ...


def _fetcher(request: Request) -> PhotoFetcher:
    state = request.app.state
    fetcher: PhotoFetcher | None = getattr(state, "photo_fetcher", None)
    if fetcher is None:
        settings = state.settings
        fetcher = ImageFetcher(
            settings.images,
            settings.sources,
            requests_per_minute=settings.images.proxy_requests_per_minute,
            burst=settings.images.proxy_burst,
        )
        state.photo_fetcher = fetcher
    return fetcher


@router.get("/photo")
def photo(request: Request, src: Annotated[str, Query(max_length=4096)]) -> Response:
    settings = request.app.state.settings.images
    if not settings.proxy_enabled:
        return Response(status_code=404)
    if not SqlImageStore(make_session_factory(request.app.state.engine)).known(src):
        return Response(status_code=404)
    try:
        image = _fetcher(request).fetch_image(src)
    except ImageFetchError as exc:
        log.info("photo_proxy_failed", url=src, reason=exc.reason)
        return Response(status_code=502)
    if image.content_type not in RASTER_TYPES:
        log.info("photo_proxy_failed", url=src, reason=f"not served ({image.content_type})")
        return Response(status_code=502)
    return Response(
        image.body,
        media_type=image.content_type,
        headers={
            "Cache-Control": f"private, max-age={settings.proxy_cache_seconds}",
            "Content-Security-Policy": PHOTO_CSP,
        },
    )
