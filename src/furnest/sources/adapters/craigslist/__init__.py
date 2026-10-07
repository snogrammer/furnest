"""Craigslist (craigslist.org): furnished housing, fetched directly.

Craigslist has no API, and its RSS feeds (``?format=rss``) now answer 403
"blocked". Its search and posting pages answer plain HTTP, and robots.txt allows
both (``/search``, ``/view``). It disallows ``/reply``, where the poster's relay
address lives behind a captcha, so that is never fetched. This adapter reads the
pages through :class:`~furnest.sources.http.SourceHttpClient`, so robots.txt, the
rate limit, retries and block detection all apply, and maps them with
:mod:`.parse`. ``docs/adapters.md`` has the research and the trade-offs.

Configure it in ``SOURCES_ADAPTERS`` with Craigslist *site names*, the subdomain
of each region's site::

    {"craigslist": {"search_locations": ["sfbay"],
                    "interval_minutes": 120, "max_results": 50}}

Without ``search_locations`` each run fails with a message saying so.

One run:

1. searches each site in each category the criteria call for (apartments/housing,
   sublets/temporary, rooms/shared), newest first, with Craigslist's furnished
   filter and the criteria's price, beds and baths;
2. takes rows from those pages in turn, dropping repeats, up to ``max_results``;
3. completes each row from its posting page. Postings are cached in the worker
   process (:class:`DetailCache`), so a run fetches only postings it hasn't seen.
   The search row supplies price, title and coordinates fresh every run.

How many postings one run fetches is capped so the run fits in
``run_timeout_seconds`` at ``requests_per_minute``. Rows past the cap, and rows
whose posting fails to load, are returned from the search row alone and completed
on a later run. A posting that has been taken down drops its row. A blocked
request (403 or a captcha page) fails the run, so nothing is marked missing.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from itertools import zip_longest
from typing import ClassVar

import httpx2
from pydantic import ValidationError

from furnest.domain import Criteria, NormalizedListing
from furnest.logging import get_logger
from furnest.sources.adapters.craigslist.parse import (
    SITE_NAME,
    Detail,
    SearchResult,
    categories,
    parse_detail,
    parse_search,
    search_url,
    to_listing,
)
from furnest.sources.base import AdapterConfig, Capabilities, SourceAdapter
from furnest.sources.errors import SourceBlockedError, SourceError
from furnest.sources.http import SourceHttpClient

log = get_logger(__name__)

SITE = "https://www.craigslist.org"
# Craigslist's block page answers 403, which is caught anyway; this catches a 200 one.
BLOCK_MARKERS = ("your request has been blocked",)
# Statuses that mean the posting is gone rather than that fetching it failed.
_GONE = frozenset({404, 410})
# Share of the run timeout spent fetching postings; the rest is slack for retries.
_DETAIL_TIME_SHARE = 0.8


class DetailCache:
    """Postings by token, shared by every run in the worker process.

    Bounded (least recently used goes first) and expiring, so an edited posting is
    picked up within ``ttl_seconds``. Runs of one source don't overlap, but the
    worker runs sources on several threads, so access is locked.
    """

    def __init__(
        self,
        *,
        max_entries: int = 5000,
        ttl_seconds: float = 7 * 24 * 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entries: OrderedDict[str, tuple[float, Detail]] = OrderedDict()
        self._max = max_entries
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()

    def get(self, token: str) -> Detail | None:
        with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                return None
            stored_at, detail = entry
            if self._clock() - stored_at > self._ttl:
                del self._entries[token]
                return None
            self._entries.move_to_end(token)
            return detail

    def put(self, token: str, detail: Detail) -> None:
        with self._lock:
            self._entries[token] = (self._clock(), detail)
            self._entries.move_to_end(token)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


_shared_cache = DetailCache()


class Craigslist(SourceAdapter):
    name: ClassVar[str] = "craigslist"
    display_name: ClassVar[str] = "Craigslist"
    capabilities: ClassVar[Capabilities] = Capabilities(provides_contact=True)

    def __init__(
        self,
        config: AdapterConfig,
        *,
        transport: httpx2.BaseTransport | None = None,
        cache: DetailCache | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(config)
        self._transport = transport
        self._cache = cache if cache is not None else _shared_cache
        self._clock = clock
        self._sleep = sleep

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        sites = self._sites()
        with SourceHttpClient(
            self.config,
            transport=self._transport,
            clock=self._clock,
            sleep=self._sleep,
            block_markers=BLOCK_MARKERS,
        ) as http:
            pages = [
                self._search_page(http, criteria, site, category)
                for site in sites
                for category in categories(criteria)
            ]
            rows = _take(_interleave(pages), self.config.max_results)
            budget = self._detail_budget(len(pages))
            listings: list[NormalizedListing] = []
            deferred = 0
            for row in rows:
                detail = self._cache.get(row.token)
                if detail is None and budget > 0:
                    budget -= 1
                    found, gone = self._fetch_detail(http, row)
                    if gone:
                        continue
                    detail = found
                elif detail is None:
                    deferred += 1
                if (listing := _listing(row, detail)) is not None:
                    listings.append(listing)
        if deferred:
            log.info("craigslist.details_deferred", count=deferred)
        return listings

    def _sites(self) -> tuple[str, ...]:
        sites = self.config.search_locations
        if not sites:
            msg = (
                'set "search_locations" to Craigslist site names (e.g. ["sfbay"]) for '
                f"{self.name} in SOURCES_ADAPTERS"
            )
            raise SourceError(msg, url=SITE)
        bad = [site for site in sites if not SITE_NAME.fullmatch(site)]
        if bad:
            msg = (
                f"search_locations for {self.name} must be Craigslist site names, the "
                f"subdomain of a region's site (e.g. 'sfbay'); got {', '.join(map(repr, bad))}"
            )
            raise SourceError(msg, url=SITE)
        return sites

    def _search_page(
        self, http: SourceHttpClient, criteria: Criteria, site: str, category: str
    ) -> list[SearchResult]:
        url = search_url(site, category, criteria)
        try:
            return parse_search(http.get(url).text, category=category)
        except ValueError as exc:
            # A page we can't read must fail the run: as "no results" it would mark
            # every stored listing missing.
            raise SourceError(f"{exc} ({url})", url=url) from exc

    def _detail_budget(self, requests_made: int) -> int:
        """How many postings this run can fetch in its time at the rate limit."""
        config = self.config
        allowed = config.run_timeout_seconds * _DETAIL_TIME_SHARE * config.requests_per_minute / 60
        return max(0, int(allowed) - requests_made)

    def _fetch_detail(
        self, http: SourceHttpClient, row: SearchResult
    ) -> tuple[Detail | None, bool]:
        """The row's posting, and whether it is gone. ``(None, False)`` if it failed."""
        try:
            page = http.get(row.url).text
            detail = parse_detail(page)
        except SourceBlockedError:
            raise
        except SourceError as exc:
            if exc.status in _GONE:
                return None, True
            log.warning("craigslist.detail_failed", token=row.token, error=str(exc))
            return None, False
        except ValueError as exc:
            log.warning("craigslist.detail_unreadable", token=row.token, error=str(exc))
            return None, False
        if detail is None:
            return None, True
        self._cache.put(row.token, detail)
        return detail, False


def _interleave(pages: list[list[SearchResult]]) -> Iterator[SearchResult]:
    """Rows from each page in turn, so one busy category doesn't crowd out the rest."""
    for rank in zip_longest(*pages):
        yield from (row for row in rank if row is not None)


def _take(rows: Iterator[SearchResult], limit: int) -> list[SearchResult]:
    seen: dict[str, SearchResult] = {}
    for row in rows:
        if len(seen) >= limit:
            break
        seen.setdefault(row.token, row)
    return list(seen.values())


def _listing(row: SearchResult, detail: Detail | None) -> NormalizedListing | None:
    try:
        return to_listing(row, detail)
    except (ValueError, ValidationError) as exc:
        log.warning("craigslist.row_skipped", token=row.token, error=str(exc).splitlines()[0])
        return None
