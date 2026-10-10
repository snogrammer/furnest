"""Zumper (zumper.com): long-term rentals, via a data provider.

Every Zumper page, robots.txt included, sits behind a JavaScript "Client Challenge",
and its API only takes partner listings in. furnest doesn't try to get past that, so
this adapter doesn't fetch the site. It runs the Apify actor ``memo23/zumper-cheerio``
(through :class:`~furnest.sources.apify.ApifyAdapter`), which needs an Apify API
token, and maps the items with :mod:`.parse`. ``docs/adapters.md`` has the research.

The actor has no furnished filter, so the adapter keeps only the items whose title or
description says they are furnished. It takes Zumper URLs: each search location
becomes ``/apartments-for-rent/<city>-<st>``; price, beds and the move-in date are
left to matching.

Configure it on the dashboard's Sources page (the Apify token under Credentials,
the rest in its settings), or in ``SOURCES_ADAPTERS``::

    {"zumper": {"api_key": "<apify token>",
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
from furnest.sources.adapters.zumper.parse import (
    ACTOR,
    SITE,
    NotFurnishedError,
    build_input,
    parse_item,
)
from furnest.sources.apify import ApifyAdapter

log = get_logger(__name__)


class Zumper(ApifyAdapter):
    name: ClassVar[str] = "zumper"
    display_name: ClassVar[str] = "Zumper"
    job: ClassVar[str] = ACTOR
    site: ClassVar[str] = SITE
    search_locations_hint: ClassVar[str] = (
        'City and state, one per line, e.g. "Springfield, IL". Zumper shares much of its '
        "inventory with PadMapper."
    )

    def build_input(self, criteria: Criteria) -> dict[str, Any]:
        return build_input(
            criteria,
            locations=self.config.search_locations,
            max_results=self.config.max_results,
        )

    def parse_items(
        self, items: list[dict[str, Any]], run_input: Mapping[str, Any]
    ) -> list[NormalizedListing]:
        today = self._today()
        listings: list[NormalizedListing] = []
        for item in items:
            try:
                listings.append(parse_item(item, today=today))
            except NotFurnishedError:
                continue
            except (ValueError, ValidationError) as exc:
                log.warning(
                    "zumper.item_skipped",
                    source_id=item.get("listing_id"),
                    error=str(exc).splitlines()[0],
                )
        return listings
