"""Zumper (zumper.com): long-term rentals, via a data provider.

Every Zumper page, robots.txt included, sits behind a JavaScript "Client Challenge",
and its API only takes partner listings in. furnest doesn't try to get past that, so
this adapter doesn't fetch the site. It runs the Apify actor ``memo23/zumper-cheerio``
(through :class:`~furnest.sources.apify.ApifyAdapter`), which needs an Apify API
token, and maps the items with :mod:`.parse`. ``docs/adapters.md`` has the research.

The actor takes Zumper URLs: each search location becomes Zumper's furnished search,
``/apartments-for-rent/<city>-<st>/furnished``; price, beds and the move-in date are
left to matching. Most rows are apartment buildings, so one row can yield several
listings, one per unit, and only the units marked furnished are kept.

Configure it on the dashboard's Sources page (the Apify token under Credentials,
the rest in its settings), or in ``SOURCES_ADAPTERS``::

    {"zumper": {"api_key": "<apify token>",
                "search_locations": ["Springfield, IL"],
                "interval_minutes": 1440, "max_results": 40}}

Without an Apify token it is disabled with a reason on the dashboard. Without
``search_locations`` each run fails with a message saying so. An empty dataset is a
normal result. Items that can't be mapped are logged and skipped, and units that
aren't furnished are dropped, so one odd listing doesn't fail the run.
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
    # $0.005 a run and $0.0025 a row (docs/adapters.md, Cost).
    start_cost_usd: ClassVar[float] = 0.005
    result_cost_usd: ClassVar[float] = 0.0025
    search_locations_hint: ClassVar[str] = (
        'City and state, one per line, e.g. "Springfield, IL". Zumper shares much of its '
        "inventory with PadMapper."
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
        today = self._today()
        listings: list[NormalizedListing] = []
        for item in items:
            try:
                listings.extend(parse_item(item, today=today))
            except (ValueError, ValidationError) as exc:
                log.warning(
                    "zumper.item_skipped",
                    source_id=_record_id(item),
                    error=str(exc).splitlines()[0],
                )
        return listings


def _record_id(item: dict[str, Any]) -> object:
    data = item.get("entity")
    record = data.get("data") if isinstance(data, dict) else None
    source = record if isinstance(record, dict) else item
    return source.get("listing_id") or source.get("building_id")
