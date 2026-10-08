"""Reverse image search: where else on the web do a property's photos appear?

A listing whose photos show up on unrelated pages is often reused or stolen. Enrichment
asks a :class:`ReverseImageSearch` about the property's photos and stores the pages
found as ``reverse_image`` rows in ``enrichment_results``. It is optional:

* ``none`` (the default), or any provider without ``REVERSE_IMAGE_API_KEY``, is
  :class:`NullReverseImageSearch`. It finds nothing and never touches the network.
* ``serpapi`` is SerpApi's Google Lens engine, ``tineye`` is TinEye and ``bing`` is
  Bing Visual Search.

A failure (timeout, HTTP error, garbage reply) raises :class:`ReverseImageError`.
Pages on the listing's own sources are not reuse and are dropped by the caller via
``ignore_hosts``.
"""

from __future__ import annotations

import hashlib
import json
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol, Self
from urllib.parse import urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, HttpUrl, ValidationError
from sqlalchemy import select

from furnest.config import ReverseImageProvider, ReverseImageSettings
from furnest.db.models import EnrichmentResult, Listing
from furnest.db.models.enrichment import REVERSE_IMAGE
from furnest.db.session import session_scope
from furnest.domain import NormalizedListing
from furnest.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

log = get_logger(__name__)

SERPAPI_URL = "https://serpapi.com/search.json"
TINEYE_URL = "https://api.tineye.com/rest/search/"
BING_URL = "https://api.bing.microsoft.com/v7.0/images/visualsearch"


class ReverseImageMatch(BaseModel):
    """A page on the web that shows (a version of) the searched photo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_url: HttpUrl
    title: str = ""
    provider: str


class ReverseImageError(Exception):
    """The search failed. ``reason`` is short enough to log or store."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReverseImageSearch(Protocol):
    def search(self, image_url: str) -> list[ReverseImageMatch]:
        """Pages showing the photo at ``image_url``. Raises :class:`ReverseImageError`."""
        ...


class NullReverseImageSearch:
    """No provider or no key: finds nothing."""

    def search(self, image_url: str) -> list[ReverseImageMatch]:
        return []


class _HttpSearch:
    provider: ReverseImageProvider

    def __init__(
        self,
        settings: ReverseImageSettings,
        *,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        if settings.api_key is None:
            msg = "reverse image search needs REVERSE_IMAGE_API_KEY"
            raise ValueError(msg)
        self._key = settings.api_key.get_secret_value()
        self._limit = settings.max_results
        self._http = httpx2.Client(
            transport=transport,
            timeout=httpx2.Timeout(settings.timeout_seconds),
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _send(self, method: str, url: str, **kwargs: Any) -> object:  # noqa: ANN401
        try:
            response = self._http.request(method, url, **kwargs)
        except httpx2.TimeoutException as exc:
            raise ReverseImageError("timeout") from exc
        except httpx2.HTTPError as exc:
            raise ReverseImageError(f"transport error: {type(exc).__name__}") from exc
        if response.is_error:
            raise ReverseImageError(f"HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise ReverseImageError("malformed response") from exc

    def _match(self, url: object, title: object) -> ReverseImageMatch | None:
        if not isinstance(url, str):
            return None
        try:
            return ReverseImageMatch(
                page_url=HttpUrl(url),
                title=title.strip() if isinstance(title, str) else "",
                provider=self.provider.value,
            )
        except ValidationError:
            return None


def _dict(body: object) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ReverseImageError("malformed response")
    return body


def _items(body: dict[str, Any], key: str) -> list[Any]:
    value = body.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReverseImageError("malformed response")
    return value


class SerpApiLensSearch(_HttpSearch):
    """SerpApi's ``google_lens`` engine; its ``visual_matches`` are the pages."""

    provider = ReverseImageProvider.SERPAPI

    def search(self, image_url: str) -> list[ReverseImageMatch]:
        body = _dict(
            self._send(
                "GET",
                SERPAPI_URL,
                params={"engine": "google_lens", "url": image_url, "api_key": self._key},
            )
        )
        if isinstance(body.get("error"), str) and not body.get("visual_matches"):
            # SerpApi says "hasn't returned any results" with a 200 when nothing matched.
            if "any results" in body["error"]:
                return []
            raise ReverseImageError("provider error")
        matches = []
        for item in _items(body, "visual_matches"):
            if isinstance(item, dict) and (m := self._match(item.get("link"), item.get("title"))):
                matches.append(m)
        return matches[: self._limit]


class TinEyeSearch(_HttpSearch):
    """TinEye's ``/search``; each match lists the backlinks where the image sits."""

    provider = ReverseImageProvider.TINEYE

    def search(self, image_url: str) -> list[ReverseImageMatch]:
        body = _dict(
            self._send(
                "GET",
                TINEYE_URL,
                params={
                    "image_url": image_url,
                    "api_key": self._key,
                    "limit": self._limit,
                    "sort": "score",
                    "order": "desc",
                },
            )
        )
        results = body.get("results")
        if not isinstance(results, dict):
            raise ReverseImageError("malformed response")
        matches = []
        for item in _items(results, "matches"):
            if not isinstance(item, dict):
                continue
            for link in _items(item, "backlinks"):
                if isinstance(link, dict) and (m := self._match(link.get("backlink"), "")):
                    matches.append(m)
        return matches[: self._limit]


class BingVisualSearch(_HttpSearch):
    """Bing Visual Search's ``PagesIncluding`` action."""

    provider = ReverseImageProvider.BING

    def search(self, image_url: str) -> list[ReverseImageMatch]:
        request = {"imageInfo": {"url": image_url}}
        body = _dict(
            self._send(
                "POST",
                BING_URL,
                headers={"Ocp-Apim-Subscription-Key": self._key},
                files={"knowledgeRequest": (None, json.dumps(request))},
            )
        )
        matches = []
        for tag in _items(body, "tags"):
            if not isinstance(tag, dict):
                continue
            for action in _items(tag, "actions"):
                if not isinstance(action, dict) or action.get("actionType") != "PagesIncluding":
                    continue
                data = action.get("data")
                for page in _items(data, "value") if isinstance(data, dict) else []:
                    if isinstance(page, dict) and (
                        m := self._match(page.get("hostPageUrl"), page.get("name"))
                    ):
                        matches.append(m)
        return matches[: self._limit]


def build_reverse_image_search(
    settings: ReverseImageSettings,
    *,
    transport: httpx2.BaseTransport | None = None,
) -> ReverseImageSearch:
    """The search ``REVERSE_IMAGE_PROVIDER`` selects; a no-op without provider or key."""
    if settings.provider is ReverseImageProvider.NONE or settings.api_key is None:
        return NullReverseImageSearch()
    if settings.provider is ReverseImageProvider.SERPAPI:
        return SerpApiLensSearch(settings, transport=transport)
    if settings.provider is ReverseImageProvider.TINEYE:
        return TinEyeSearch(settings, transport=transport)
    return BingVisualSearch(settings, transport=transport)


# --- Enrichment -----------------------------------------------------------------


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


class ReverseImageEnrichment:
    """Searches a property's photos and stores the pages found, replacing earlier rows."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        search: ReverseImageSearch,
        settings: ReverseImageSettings,
    ) -> None:
        self._factory = session_factory
        self._search = search
        self._settings = settings

    def run(self, property_id: int) -> int:
        """Search up to ``max_photos`` photos; returns how many pages were stored."""
        if isinstance(self._search, NullReverseImageSearch):
            return 0
        with session_scope(self._factory) as session:
            photos, own_hosts = self._photos(session, property_id)
        found: dict[str, tuple[ReverseImageMatch, str]] = {}
        failures = 0
        for photo in photos:
            try:
                matches = self._search.search(photo)
            except ReverseImageError as exc:
                failures += 1
                log.info("reverse_image_failed", property_id=property_id, reason=exc.reason)
                continue
            for match in matches:
                url = str(match.page_url)
                if _host(url) not in own_hosts:
                    found.setdefault(url, (match, photo))
        if failures == len(photos) and photos:
            # Nothing answered; keep what an earlier run found.
            return 0
        self._store(property_id, found)
        return len(found)

    def _photos(self, session: Session, property_id: int) -> tuple[list[str], set[str]]:
        photos: list[str] = []
        hosts: set[str] = set()
        for row in session.scalars(select(Listing).where(Listing.property_id == property_id)):
            item = NormalizedListing.model_validate(row.data)
            hosts.add(_host(str(item.url)))
            photos.extend(str(url) for url in item.images)
        unique = list(dict.fromkeys(photos))[: self._settings.max_photos]
        # A photo hosted by a listing's own site is not worth reporting back to it.
        return unique, hosts

    def _store(self, property_id: int, found: dict[str, tuple[ReverseImageMatch, str]]) -> None:
        with session_scope(self._factory) as session:
            existing = {
                row.source_id: row
                for row in session.scalars(
                    select(EnrichmentResult).where(
                        EnrichmentResult.property_id == property_id,
                        EnrichmentResult.kind == REVERSE_IMAGE,
                    )
                )
            }
            kept: set[str] = set()
            for url, (match, photo) in found.items():
                key = hashlib.sha256(url.encode()).hexdigest()
                kept.add(key)
                row = existing.get(key)
                if row is None:
                    row = EnrichmentResult(
                        property_id=property_id,
                        kind=REVERSE_IMAGE,
                        source=match.provider,
                        source_id=key,
                    )
                    session.add(row)
                row.source = match.provider
                row.url = url
                row.title = match.title
                row.score = 1.0
                row.data = {"photo": photo}
            for key, row in existing.items():
                if key not in kept:
                    session.delete(row)
