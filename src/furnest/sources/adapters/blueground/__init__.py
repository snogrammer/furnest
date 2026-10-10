"""Blueground (theblueground.com): furnished apartments, fetched directly.

Blueground has no API. Its city pages (``/furnished-apartments-<city>-usa``) answer
plain HTTP with no bot check, and robots.txt allows them (it disallows ``/book``,
``/users``, ``/payments`` and similar, which are never fetched). This adapter reads
the pages through :class:`~furnest.sources.http.SourceHttpClient`, so robots.txt, the
rate limit, retries and block detection all apply, and maps them with :mod:`.parse`.
Blueground's terms forbid automated access, so it is off by default.
``docs/adapters.md`` has the research and the trade-offs.

Configure it on the dashboard's Sources page, or in ``SOURCES_ADAPTERS``, with
Blueground *city slugs*, the middle of the city page's address (``chicago`` for
``/furnished-apartments-chicago-usa``)::

    {"blueground": {"search_locations": ["chicago", "new-york"],
                    "interval_minutes": 360, "max_results": 50}}

Without ``search_locations`` each run fails with a message saying so.

One run asks each city page for a stay (the minimum lease, from the earliest move-in)
so that the homes come back priced, 18 to a page, and takes pages from the cities in
turn until ``max_results`` homes are in, the pages run out, or the run's request
budget (what fits in ``run_timeout_seconds`` at ``requests_per_minute``) is spent. A
page without a home list fails the run, and so does a block or an unknown city (404),
so nothing is marked missing.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import date
from typing import ClassVar

import httpx2
from pydantic import ValidationError

from furnest.domain import Criteria, NormalizedListing
from furnest.logging import get_logger
from furnest.sources.adapters.airbnb.parse import StayWindow
from furnest.sources.adapters.blueground.parse import (
    PAGE_SIZE,
    SITE,
    SLUG,
    CityPage,
    blueground_window,
    city_url,
    parse_city_page,
    to_listing,
)
from furnest.sources.base import AdapterConfig, Capabilities, SourceAdapter
from furnest.sources.errors import SourceError
from furnest.sources.http import SourceHttpClient

log = get_logger(__name__)

# Share of the run timeout spent fetching pages; the rest is slack for retries.
_PAGE_TIME_SHARE = 0.8


class Blueground(SourceAdapter):
    name: ClassVar[str] = "blueground"
    display_name: ClassVar[str] = "Blueground"
    capabilities: ClassVar[Capabilities] = Capabilities()
    search_locations_are_places: ClassVar[bool] = False
    search_locations_hint: ClassVar[str] = (
        "Blueground city slugs as in /furnished-apartments-<city>-usa, one per line, "
        "e.g. chicago or new-york."
    )

    def __init__(
        self,
        config: AdapterConfig,
        *,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = date.today,
    ) -> None:
        super().__init__(config)
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._today = today

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        cities = self._cities()
        window = blueground_window(criteria, today=self._today())
        wanted_pages = math.ceil(self.config.max_results / PAGE_SIZE)
        budget = self._page_budget()
        listings: dict[str, NormalizedListing] = {}
        # (city, page number) still to fetch, one round of cities at a time.
        queue = list(cities)
        number = 1
        with SourceHttpClient(
            self.config, transport=self._transport, clock=self._clock, sleep=self._sleep
        ) as http:
            while queue and number <= wanted_pages and len(listings) < self.config.max_results:
                still_going: list[str] = []
                for city in queue:
                    # Every city gets its first page, whatever the budget.
                    if number > 1 and budget <= 0:
                        break
                    budget -= 1
                    page = self._fetch_page(http, city, window, number)
                    for listing in self._listings(page):
                        if len(listings) < self.config.max_results:
                            listings.setdefault(listing.source_id, listing)
                    if page.rows and number * PAGE_SIZE < page.total:
                        still_going.append(city)
                queue = still_going
                number += 1
        return list(listings.values())

    def _cities(self) -> tuple[str, ...]:
        slugs = self.config.search_locations
        if not slugs:
            msg = (
                f"no search locations for {self.name}: set Blueground city slugs "
                '(e.g. "chicago") on the Sources page, or "search_locations" in SOURCES_ADAPTERS'
            )
            raise SourceError(msg, url=SITE)
        bad = [slug for slug in slugs if not SLUG.fullmatch(slug)]
        if bad:
            msg = (
                f"search_locations for {self.name} must be Blueground city slugs, as in "
                f"/furnished-apartments-<city>-usa (e.g. 'new-york'); got "
                f"{', '.join(map(repr, bad))}"
            )
            raise SourceError(msg, url=SITE)
        return slugs

    def _page_budget(self) -> int:
        """How many pages this run can fetch in its time at the rate limit."""
        config = self.config
        allowed = config.run_timeout_seconds * _PAGE_TIME_SHARE * config.requests_per_minute / 60
        return max(1, int(allowed))

    def _fetch_page(
        self, http: SourceHttpClient, city: str, window: StayWindow, number: int
    ) -> CityPage:
        url = city_url(city, window, number)
        try:
            return parse_city_page(http.get(url).text)
        except ValueError as exc:
            # A page we can't read must fail the run: as "no results" it would mark
            # every stored listing missing.
            raise SourceError(f"{exc} ({url})", url=url) from exc

    @staticmethod
    def _listings(page: CityPage) -> list[NormalizedListing]:
        found: list[NormalizedListing] = []
        for row in page.rows:
            try:
                found.append(to_listing(row, page))
            except (ValueError, ValidationError) as exc:
                log.warning(
                    "blueground.row_skipped", path=row.get("path"), error=str(exc).splitlines()[0]
                )
        return found
