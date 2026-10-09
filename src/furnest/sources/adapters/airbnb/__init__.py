"""Airbnb (airbnb.com): monthly stays, via a data provider.

Airbnb has no public search API: its APIs are for approved software partners who
manage hosts' listings. Plain HTTP does get its search pages, but its robots.txt
disallows search (``/s/*/*``) and the stays API for every crawler, and its terms
forbid scraping. So this adapter doesn't fetch the site. It runs the Apify actor
``tri_angle/airbnb-scraper`` (through :class:`~furnest.sources.apify.ApifyAdapter`),
which needs an Apify API token, over a stay of a month or more, which is Airbnb's
monthly-stay search, and maps the items with :mod:`.parse`. ``docs/adapters.md`` has
the research and the trade-offs.

Configure it on the dashboard's Sources page (the Apify token under Credentials,
the rest in its settings), or in ``SOURCES_ADAPTERS``::

    {"airbnb": {"api_key": "<apify token>",
                "search_locations": ["Springfield, IL"],
                "interval_minutes": 360, "max_results": 50}}

Without an Apify token it is disabled with a reason on the dashboard. Without
``search_locations`` each run fails with a message saying so. The actor bills per
listing, so keep the interval long and ``max_results`` low.

An empty dataset is a normal result (nothing free for the whole stay). Items that
can't be mapped are logged and skipped, so one odd listing doesn't fail the run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import ValidationError

from furnest.domain import Criteria, NormalizedListing
from furnest.logging import get_logger
from furnest.sources.adapters.airbnb.parse import ACTOR, SITE, build_input, parse_item, window_of
from furnest.sources.apify import ApifyAdapter

log = get_logger(__name__)


class Airbnb(ApifyAdapter):
    name: ClassVar[str] = "airbnb"
    display_name: ClassVar[str] = "Airbnb"
    job: ClassVar[str] = ACTOR
    site: ClassVar[str] = SITE

    def build_input(self, criteria: Criteria) -> dict[str, Any]:
        return build_input(
            criteria,
            locations=self.config.search_locations,
            max_results=self.config.max_results,
            today=self._today(),
        )

    def parse_items(
        self, items: list[dict[str, Any]], run_input: Mapping[str, Any]
    ) -> list[NormalizedListing]:
        window = window_of(dict(run_input))
        listings: list[NormalizedListing] = []
        for item in items:
            try:
                listings.append(parse_item(item, window=window))
            except (ValueError, ValidationError) as exc:
                log.warning(
                    "airbnb.item_skipped",
                    source_id=item.get("id"),
                    error=str(exc).splitlines()[0],
                )
        return listings
