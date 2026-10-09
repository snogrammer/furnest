"""Furnished Finder (furnishedfinder.com): furnished monthly rentals, via a data provider.

Furnished Finder has no public API, and its search and property pages sit behind a
Cloudflare check: plain HTTP gets a 403, and even a real browser is asked to verify
it's human. furnest doesn't try to get past that, so this adapter doesn't fetch the
site. It runs the Apify actor
``abotapi/furnishedfinder-scraper`` (through :class:`~furnest.sources.apify.ApifyAdapter`),
which needs an Apify API token, and maps the items with :mod:`.parse`. ``docs/adapters.md`` has the
research and the trade-offs.

Configure it on the dashboard's Sources page (the Apify token under Credentials,
the rest in its settings), or in ``SOURCES_ADAPTERS``::

    {"furnished_finder": {"api_key": "<apify token>",
                          "search_locations": ["Springfield, IL"],
                          "interval_minutes": 360, "max_results": 50}}

Without an Apify token it is disabled with a reason on the dashboard. Without
``search_locations`` each run fails with a message saying so. The actor bills per
run and per listing, so keep the interval long and ``max_results`` low.

If the actor returns its "nothing was scanned" row (an empty search, or a blocked
first page), the run fails rather than returning no listings. Otherwise a block would
read as every listing having disappeared. Items that can't be mapped are logged and
skipped, so one odd listing doesn't fail the run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

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
from furnest.sources.apify import ApifyAdapter
from furnest.sources.errors import SourceError

log = get_logger(__name__)


class FurnishedFinder(ApifyAdapter):
    name: ClassVar[str] = "furnished_finder"
    display_name: ClassVar[str] = "Furnished Finder"
    job: ClassVar[str] = ACTOR
    site: ClassVar[str] = SITE

    def build_input(self, criteria: Criteria) -> dict[str, Any]:
        return build_input(
            criteria, locations=self.config.search_locations, max_results=self.config.max_results
        )

    def parse_items(
        self, items: list[dict[str, Any]], run_input: Mapping[str, Any]
    ) -> list[NormalizedListing]:
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
