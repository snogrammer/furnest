"""Furnished Finder (furnishedfinder.com): furnished monthly rentals, via a data provider.

Furnished Finder has no public API, and its search and property pages sit behind a
Cloudflare check: plain HTTP gets a 403, and even a real browser is asked to verify
it's human. furnest doesn't try to get past that, so this adapter doesn't fetch the
site. It runs the Apify actor
``abotapi/furnishedfinder-scraper`` (:mod:`furnest.sources.apify`), which needs an
Apify API token, and maps the items with :mod:`.parse`. ``docs/adapters.md`` has the
research and the trade-offs.

Configure it in ``SOURCES_ADAPTERS``::

    {"furnished_finder": {"api_key": "<apify token>",
                          "search_locations": ["Springfield, IL"],
                          "interval_minutes": 360, "max_results": 50}}

Without ``api_key`` it is disabled with a reason on the dashboard. Without
``search_locations`` each run fails with a message saying so. The actor bills per
run and per listing, so keep the interval long and ``max_results`` low.

If the actor returns its "nothing was scanned" row (an empty search, or a blocked
first page), the run fails rather than returning no listings. Otherwise a block would
read as every listing having disappeared. Items that can't be mapped are logged and
skipped, so one odd listing doesn't fail the run.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import ClassVar

import httpx2
from pydantic import ValidationError

from furnest.domain import Criteria, NormalizedListing
from furnest.logging import get_logger
from furnest.sources.adapters.furnished_finder.parse import (
    ACTOR,
    SITE,
    build_input,
    is_error_row,
    parse_item,
)
from furnest.sources.apify import run_actor
from furnest.sources.base import AdapterConfig, Capabilities, SourceAdapter
from furnest.sources.errors import SourceError
from furnest.sources.http import SourceHttpClient

log = get_logger(__name__)

# The actor gets this much less time than the whole run, so Apify stops it (and we
# read why) before the worker's own run timeout fires.
ACTOR_TIMEOUT_MARGIN_SECONDS = 60
MIN_ACTOR_TIMEOUT_SECONDS = 60


def _today() -> date:
    return datetime.now(UTC).date()


class FurnishedFinder(SourceAdapter):
    name: ClassVar[str] = "furnished_finder"
    display_name: ClassVar[str] = "Furnished Finder"
    capabilities: ClassVar[Capabilities] = Capabilities(has_api=True, requires_api_key=True)

    def __init__(
        self,
        config: AdapterConfig,
        *,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = _today,
    ) -> None:
        super().__init__(config)
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._today = today

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        config = self.config
        if config.api_key is None:
            msg = f'set "api_key" (an Apify API token) for {self.name} in SOURCES_ADAPTERS'
            raise SourceError(msg, url=SITE)
        if not config.search_locations:
            msg = (
                f'set "search_locations" (e.g. ["Springfield, IL"]) for {self.name} '
                "in SOURCES_ADAPTERS"
            )
            raise SourceError(msg, url=SITE)

        run_input = build_input(
            criteria, locations=config.search_locations, max_results=config.max_results
        )
        timeout = max(
            MIN_ACTOR_TIMEOUT_SECONDS, config.run_timeout_seconds - ACTOR_TIMEOUT_MARGIN_SECONDS
        )
        http_config = replace(config, respect_robots=False)
        with SourceHttpClient(
            http_config, transport=self._transport, clock=self._clock, sleep=self._sleep
        ) as http:
            items = run_actor(
                http,
                ACTOR,
                run_input,
                token=config.api_key.get_secret_value(),
                timeout_seconds=timeout,
                clock=self._clock,
            )
        return self._parse(items)

    def _parse(self, items: list[dict[str, object]]) -> list[NormalizedListing]:
        if empty := [item for item in items if is_error_row(item)]:
            where = sorted({str(item.get("searchLocation") or "?") for item in empty})
            msg = (
                f"the provider scanned nothing for {', '.join(where)}: no matching "
                "listings, or Furnished Finder refused the search (see the Apify run log)"
            )
            raise SourceError(msg, url=SITE)
        today = self._today()
        listings: list[NormalizedListing] = []
        for item in items:
            try:
                listings.append(parse_item(item, today=today))
            except (ValueError, ValidationError) as exc:
                log.warning(
                    "furnished_finder.item_skipped",
                    source_id=item.get("id"),
                    error=str(exc).splitlines()[0],
                )
        return listings
