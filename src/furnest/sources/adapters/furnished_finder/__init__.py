"""Furnished Finder (furnishedfinder.com): furnished monthly rentals, via a data provider.

Furnished Finder has no public API, and its search and property pages sit behind a
Cloudflare check: plain HTTP gets a 403, and even a real browser is asked to verify
it's human. furnest doesn't try to get past that, so this adapter doesn't fetch the
site. It runs the Apify actor ``memo23/furnishedfinder-scraper-cheerio`` (through
:class:`~furnest.sources.apify.ApifyAdapter`), which needs an Apify API token, and maps
the items with :mod:`.parse`. ``docs/adapters.md`` has the research and the trade-offs.

The actor takes Furnished Finder URLs: each search location ("City, ST") becomes the
city's search page, with the criteria's monthly price range in its query. A location
without a two-letter state can't become a URL, so it is skipped with a warning, and a
run with no usable location fails.

Configure it on the dashboard's Sources page (the Apify token under Credentials,
the rest in its settings), or in ``SOURCES_ADAPTERS``::

    {"furnished_finder": {"api_key": "<apify token>",
                          "search_locations": ["Springfield, IL"],
                          "interval_minutes": 1440, "max_results": 25}}

Without an Apify token it is disabled with a reason on the dashboard. Without
``search_locations`` each run fails with a message saying so. An empty dataset is a
normal result. Items that can't be mapped are logged and skipped, so one odd listing
doesn't fail the run.
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
    parse_item,
    search_url,
)
from furnest.sources.apify import ApifyAdapter
from furnest.sources.errors import SourceError

log = get_logger(__name__)


class FurnishedFinder(ApifyAdapter):
    name: ClassVar[str] = "furnished_finder"
    display_name: ClassVar[str] = "Furnished Finder"
    job: ClassVar[str] = ACTOR
    site: ClassVar[str] = SITE
    # $0.005 a run and $0.002 a listing (docs/adapters.md, Cost).
    start_cost_usd: ClassVar[float] = 0.005
    result_cost_usd: ClassVar[float] = 0.002
    search_locations_hint: ClassVar[str] = (
        'City and two-letter state, one per line, e.g. "Springfield, IL".'
    )

    def build_input(self, criteria: Criteria) -> dict[str, Any]:
        locations = self.locations(criteria)
        if unusable := [place for place in locations if search_url(place) is None]:
            log.warning("furnished_finder.location_skipped", locations=unusable)
            if len(unusable) == len(locations):
                msg = (
                    f"no search location names a city and two-letter state "
                    f'({", ".join(unusable)}): use the form "Springfield, IL"'
                )
                raise SourceError(msg, url=SITE)
        return build_input(criteria, locations=locations, max_results=self.config.max_results)

    def parse_items(
        self, items: list[dict[str, Any]], run_input: Mapping[str, Any]
    ) -> list[NormalizedListing]:
        today = self._today()
        listings: list[NormalizedListing] = []
        for item in items:
            try:
                listings.append(parse_item(item, today=today))
            except (ValueError, ValidationError) as exc:
                log.warning(
                    "furnished_finder.item_skipped",
                    source_id=item.get("listingId") or item.get("id"),
                    error=str(exc).splitlines()[0],
                )
        return listings
