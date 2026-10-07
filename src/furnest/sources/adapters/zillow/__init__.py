"""Zillow (zillow.com): furnished rentals, via a data provider.

Zillow has no public API for searching rentals: its partner APIs carry MLS and
valuation data for approved partners, and its rental feeds only take listings in.
Its search pages sit behind a PerimeterX check: plain HTTP gets a 403
"press & hold" captcha page, and a browser is shown the same challenge. furnest
doesn't try to get past that, so this adapter doesn't fetch the site. It runs the
Apify actor ``igolaizola/zillow-scraper-ppe`` (through
:class:`~furnest.sources.apify.ApifyAdapter`), which needs an Apify API token, with
Zillow's own "Furnished" filter on, and maps the items with :mod:`.parse`.
``docs/adapters.md`` has the research and the trade-offs.

Configure it in ``SOURCES_ADAPTERS``::

    {"zillow": {"api_key": "<apify token>",
                "search_locations": ["Springfield, IL"],
                "interval_minutes": 360, "max_results": 50}}

Without ``api_key`` it is disabled with a reason on the dashboard. Without
``search_locations`` each run fails with a message saying so. The actor bills per
result and per detail fetch, so keep the interval long and ``max_results`` low.

An empty dataset is a normal result: this actor has no "nothing was scanned" row,
and a refused search ends the run ``FAILED`` instead, which fails ours. Items that
can't be mapped are logged and skipped, so one odd listing doesn't fail the run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import ValidationError

from furnest.domain import Criteria, NormalizedListing
from furnest.logging import get_logger
from furnest.sources.adapters.zillow.parse import ACTOR, SITE, build_input, parse_item
from furnest.sources.apify import ApifyAdapter

log = get_logger(__name__)


class Zillow(ApifyAdapter):
    name: ClassVar[str] = "zillow"
    display_name: ClassVar[str] = "Zillow"
    actor: ClassVar[str] = ACTOR
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
        today = self._today()
        listings: list[NormalizedListing] = []
        for item in items:
            try:
                listings.extend(parse_item(item, today=today))
            except (ValueError, ValidationError) as exc:
                log.warning(
                    "zillow.item_skipped",
                    source_id=item.get("zpid") or item.get("lotId"),
                    error=str(exc).splitlines()[0],
                )
        return listings
