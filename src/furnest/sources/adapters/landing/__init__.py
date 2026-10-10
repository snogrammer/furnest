"""Landing (hellolanding.com): furnished monthly stays, through its public API.

Landing publishes a read-only REST API that needs no auth (``/api/public``). Its
robots.txt allows that path and disallows the HTML search pages, and its terms forbid
spiders on the site, so this adapter only calls the API, through
:class:`~furnest.sources.http.SourceHttpClient` (robots.txt, the rate limit, retries
and block detection apply). ``llms.txt`` asks callers to rate-limit bulk requests; the
default limit does. :mod:`.parse` maps the JSON. ``docs/adapters.md`` has the research
and the trade-offs.

Configure it on the dashboard's Sources page, or in ``SOURCES_ADAPTERS``, with Landing
*market* or *display-city* slugs, as ``GET /api/public/markets`` lists them::

    {"landing": {"search_locations": ["austin", "round-rock-tx"],
                 "interval_minutes": 360, "max_results": 50}}

Without ``search_locations`` each run fails with a message saying so, and so does a
slug Landing doesn't list.

One run:

1. fetches the market list once and checks the configured slugs against it;
2. searches each slug over a stay of at least a month from the earliest move-in (the
   minimum lease, if longer), with the criteria's bedrooms and top price. Landing
   returns the first 25 homes per search, so name display cities to go deeper;
3. takes rows from those searches in turn, dropping repeats, up to ``max_results``;
4. completes each row from ``homes/{slug}`` and ``homes/{slug}/quote``. Both are
   cached in the worker process per home and stay (:class:`DetailCache`), so a run
   fetches only homes it hasn't seen.

How many homes one run completes is capped so the run fits in ``run_timeout_seconds``
at ``requests_per_minute``. The rest, and homes whose pages fail to load, are returned
from the search row alone and completed on a later run. A home the API no longer knows
(404) or whose quote says it's unavailable for the stay is dropped. A blocked request
fails the run, so nothing is marked missing.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from datetime import date
from itertools import zip_longest
from typing import Any, ClassVar

import httpx2
from pydantic import ValidationError

from furnest.domain import Criteria, NormalizedListing
from furnest.logging import get_logger
from furnest.sources.adapters.airbnb.parse import StayWindow
from furnest.sources.adapters.landing.parse import (
    API,
    SLUG,
    Detail,
    home_url,
    landing_window,
    markets_url,
    parse_detail,
    parse_markets,
    parse_search,
    quote_url,
    search_url,
    to_listing,
)
from furnest.sources.base import AdapterConfig, Capabilities, SourceAdapter
from furnest.sources.errors import SourceBlockedError, SourceError
from furnest.sources.http import SourceHttpClient

log = get_logger(__name__)

# Statuses that mean the home is gone rather than that fetching it failed.
_GONE = frozenset({404, 410})
# Share of the run timeout spent on home pages; the rest is slack for retries.
_DETAIL_TIME_SHARE = 0.8
# Requests to complete one home: its page and its quote.
_REQUESTS_PER_HOME = 2

CacheKey = tuple[str, str, str]


class DetailCache:
    """Homes and quotes by slug and stay, shared by every run in the worker process.

    Bounded (least recently used goes first) and expiring: a quote is for dates and
    prices move, so an entry lives ``ttl_seconds``. Runs of one source don't overlap,
    but the worker runs sources on several threads, so access is locked.
    """

    def __init__(
        self,
        *,
        max_entries: int = 5000,
        ttl_seconds: float = 24 * 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entries: OrderedDict[CacheKey, tuple[float, Detail]] = OrderedDict()
        self._max = max_entries
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()

    def get(self, key: CacheKey) -> Detail | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stored_at, detail = entry
            if self._clock() - stored_at > self._ttl:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return detail

    def put(self, key: CacheKey, detail: Detail) -> None:
        with self._lock:
            self._entries[key] = (self._clock(), detail)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


_shared_cache = DetailCache()


class Landing(SourceAdapter):
    name: ClassVar[str] = "landing"
    display_name: ClassVar[str] = "Landing"
    capabilities: ClassVar[Capabilities] = Capabilities(has_api=True)
    search_locations_are_places: ClassVar[bool] = False
    search_locations_hint: ClassVar[str] = (
        "Landing market or city slugs, one per line, e.g. austin or round-rock-tx."
    )

    def __init__(
        self,
        config: AdapterConfig,
        *,
        transport: httpx2.BaseTransport | None = None,
        cache: DetailCache | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = date.today,
    ) -> None:
        super().__init__(config)
        self._transport = transport
        self._cache = cache if cache is not None else _shared_cache
        self._clock = clock
        self._sleep = sleep
        self._today = today

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        locations = self._locations()
        window = landing_window(criteria, today=self._today())
        with SourceHttpClient(
            self.config, transport=self._transport, clock=self._clock, sleep=self._sleep
        ) as http:
            self._check_known(http, locations)
            pages = [self._search_page(http, criteria, slug, window) for slug in locations]
            rows = _take(_interleave(pages), self.config.max_results)
            budget = self._detail_budget(len(locations) + 1)
            listings: list[NormalizedListing] = []
            deferred = 0
            for row in rows:
                key = (
                    str(row["slug"]).strip(),
                    window.check_in.isoformat(),
                    window.check_out.isoformat(),
                )
                detail = self._cache.get(key)
                if detail is None and budget >= _REQUESTS_PER_HOME:
                    budget -= _REQUESTS_PER_HOME
                    found, gone = self._fetch_detail(http, key[0], window)
                    if gone:
                        continue
                    detail = found
                    if detail is not None:
                        self._cache.put(key, detail)
                elif detail is None:
                    deferred += 1
                if detail is not None and not detail.available:
                    continue
                if (listing := _listing(row, detail, window)) is not None:
                    listings.append(listing)
        if deferred:
            log.info("landing.details_deferred", count=deferred)
        return listings

    def _locations(self) -> tuple[str, ...]:
        slugs = self.config.search_locations
        if not slugs:
            msg = (
                f"no search locations for {self.name}: set Landing market or city slugs "
                '(e.g. "austin") on the Sources page, or "search_locations" in SOURCES_ADAPTERS'
            )
            raise SourceError(msg, url=API)
        bad = [slug for slug in slugs if not SLUG.fullmatch(slug)]
        if bad:
            msg = (
                f"search_locations for {self.name} must be Landing market or city slugs "
                f"(e.g. 'austin', 'austin-tx'); got {', '.join(map(repr, bad))}"
            )
            raise SourceError(msg, url=API)
        return slugs

    def _check_known(self, http: SourceHttpClient, locations: tuple[str, ...]) -> None:
        url = markets_url()
        try:
            known = parse_markets(http.get(url).json())
        except ValueError as exc:
            raise SourceError(f"{exc} ({url})", url=url) from exc
        unknown = [slug for slug in locations if slug not in known]
        if unknown:
            msg = (
                f"Landing has no market or city {', '.join(map(repr, unknown))} for "
                f"{self.name}; use slugs from {url}"
            )
            raise SourceError(msg, url=url)

    def _search_page(
        self, http: SourceHttpClient, criteria: Criteria, slug: str, window: StayWindow
    ) -> list[dict[str, Any]]:
        url = search_url(slug, window, criteria)
        try:
            return parse_search(http.get(url).json())
        except ValueError as exc:
            # A response we can't read must fail the run: as "no results" it would mark
            # every stored listing missing.
            raise SourceError(f"{exc} ({url})", url=url) from exc

    def _detail_budget(self, requests_made: int) -> int:
        """How many requests this run can still make in its time at the rate limit."""
        config = self.config
        allowed = config.run_timeout_seconds * _DETAIL_TIME_SHARE * config.requests_per_minute / 60
        return max(0, int(allowed) - requests_made)

    def _fetch_detail(
        self, http: SourceHttpClient, slug: str, window: StayWindow
    ) -> tuple[Detail | None, bool]:
        """The home and its quote, and whether it is gone. ``(None, False)`` if it failed."""
        try:
            detail = parse_detail(
                http.get(home_url(slug)).json(), http.get(quote_url(slug, window)).json()
            )
        except SourceBlockedError:
            raise
        except SourceError as exc:
            if exc.status in _GONE:
                return None, True
            log.warning("landing.detail_failed", slug=slug, error=str(exc))
            return None, False
        except ValueError as exc:
            log.warning("landing.detail_unreadable", slug=slug, error=str(exc))
            return None, False
        return detail, False


def _interleave(pages: list[list[dict[str, Any]]]) -> Iterator[dict[str, Any]]:
    """Rows from each search in turn, so one busy market doesn't crowd out the rest."""
    for rank in zip_longest(*pages):
        yield from (row for row in rank if row is not None)


def _take(rows: Iterator[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        if len(seen) >= limit:
            break
        seen.setdefault(str(row["slug"]).strip(), row)
    return list(seen.values())


def _listing(
    row: dict[str, Any], detail: Detail | None, window: StayWindow
) -> NormalizedListing | None:
    try:
        return to_listing(row, detail, window=window)
    except (ValueError, ValidationError) as exc:
        log.warning("landing.row_skipped", slug=row.get("slug"), error=str(exc).splitlines()[0])
        return None
