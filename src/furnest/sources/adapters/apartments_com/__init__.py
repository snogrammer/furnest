"""Apartments.com (apartments.com, CoStar): long-term rentals, via a data provider.

Apartments.com has no public API, and its Akamai bot protection answers 403 to every
request, robots.txt and the terms page included. furnest doesn't try to get past
that, so this adapter doesn't fetch the site. It runs the Apify actor
``epctex/apartments-scraper-api`` (through
:class:`~furnest.sources.apify.ApifyAdapter`), which needs an Apify API token, and
maps the items with :mod:`.parse`. ``docs/adapters.md`` has the research.

**Terms of use.** CoStar enforces its terms against scrapers, and they forbid
automated collection. Using the source is the operator's decision and risk, so it is
off by default. Keep the interval long.

The actor has no furnished filter, so the adapter keeps only the buildings that say
they are furnished (amenities, lease and fee lists, or description), and makes a
listing of each of their available units. It takes Apartments.com URLs: each search
location becomes ``/<city>-<st>/``, with the criteria's minimum beds and price range
in the site's filter segment.

**Apify's Free plan** runs this actor in demo mode after 5 runs a month: the dataset
holds ``{"demo": true}`` placeholders instead of buildings. A run that returns them
fails with a message saying so, rather than looking like a search with no results.

Configure it on the dashboard's Sources page (the Apify token under Credentials,
the rest in its settings), or in ``SOURCES_ADAPTERS``::

    {"apartments_com": {"api_key": "<apify token>",
                        "search_locations": ["Springfield, IL"],
                        "interval_minutes": 1440, "max_results": 40}}

Without an Apify token it is disabled with a reason on the dashboard. Without
``search_locations`` each run fails with a message saying so. An empty dataset is a
normal result. Items that can't be mapped, or aren't furnished, are skipped, so one
odd listing doesn't fail the run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import ValidationError

from furnest.domain import Criteria, NormalizedListing
from furnest.logging import get_logger
from furnest.sources.adapters.apartments_com.parse import (
    ACTOR,
    SITE,
    NotFurnishedError,
    build_input,
    parse_item,
)
from furnest.sources.apify import ApifyAdapter
from furnest.sources.errors import SourceError

log = get_logger(__name__)


class ApartmentsCom(ApifyAdapter):
    name: ClassVar[str] = "apartments_com"
    display_name: ClassVar[str] = "Apartments.com"
    job: ClassVar[str] = ACTOR
    site: ClassVar[str] = SITE
    search_locations_hint: ClassVar[str] = (
        'City and state, one per line, e.g. "Springfield, IL" (or a ZIP code). '
        "CoStar's terms forbid scraping: you use this source at your own risk."
    )

    def build_input(self, criteria: Criteria) -> dict[str, Any]:
        return build_input(
            criteria,
            locations=self.locations(criteria),
            max_results=self.config.max_results,
        )

    def parse_items(
        self, items: list[dict[str, Any]], run_input: Mapping[str, Any]
    ) -> list[NormalizedListing]:
        if any(item.get("demo") is True for item in items):
            msg = (
                f"{ACTOR} ran in demo mode and returned placeholders, not listings: "
                "Apify's Free plan allows it 5 runs a month. Use a paid Apify plan, or "
                "wait for the monthly reset"
            )
            raise SourceError(msg, url=SITE)
        today = self._today()
        listings: list[NormalizedListing] = []
        for item in items:
            try:
                listings.extend(parse_item(item, today=today))
            except NotFurnishedError:
                continue
            except (ValueError, ValidationError) as exc:
                log.warning(
                    "apartments_com.item_skipped",
                    source_id=item.get("id"),
                    error=str(exc).splitlines()[0],
                )
        return listings
