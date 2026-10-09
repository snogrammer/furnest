"""Sources researched on #48 but not built yet, registered as disabled stubs.

Each class records what the research found (October 2026) and what building it
would take. Its ``capabilities`` describe that planned route. The stubs never run:
see :mod:`furnest.sources.stub`. ``docs/adapters.md`` (Candidates) has the table.
To build one, delete its class here and add ``adapters/<name>/``.

Kopa isn't here: it has shut down.
"""

from __future__ import annotations

from typing import ClassVar

from furnest.sources.base import Capabilities
from furnest.sources.stub import StubAdapter

# What a source behind a bot check gets: a data provider job (see furnest.sources.provider).
_PROVIDER = Capabilities(has_api=True, requires_api_key=True)


class Zumper(StubAdapter):
    """Zumper: long-term rentals with a furnished filter.

    TODO: every page, including robots.txt, is behind a JavaScript "Client Challenge",
    so it would go through a data provider (Apify actors such as
    ``memo23/zumper-cheerio``). Zumper's API only takes listings in, for partners.
    Compare the actors' furnished filters and output first (see
    ``docs/adapters.md``, Data providers).
    """

    name = "zumper"
    display_name = "Zumper"
    capabilities: ClassVar[Capabilities] = _PROVIDER
    site_url = "https://www.zumper.com/"


class ApartmentsCom(StubAdapter):
    """Apartments.com (CoStar): long-term rentals with a furnished amenity filter.

    TODO: Akamai answers 403 to every request, robots.txt and the terms page
    included, and there is no public API. Route it through a data provider. The
    most-used Apify actor is ``epctex/apartments-scraper``. Check that its furnished
    filter maps onto criteria before picking it.
    """

    name = "apartments_com"
    display_name = "Apartments.com"
    capabilities: ClassVar[Capabilities] = _PROVIDER
    site_url = "https://www.apartments.com/"


class HotPads(StubAdapter):
    """HotPads (Zillow Group): rentals, much of them shared with Zillow.

    TODO: search pages answer 403 with the same PerimeterX "Press & Hold" check as
    Zillow. robots.txt allows them, defers to Zillow's terms, and disallows the
    ``/api/`` backends. A data provider would do it (Apify
    ``benthepythondev/hotpads-rental-scraper`` and others), but check first how much
    it adds over the ``zillow`` adapter after dedup.
    """

    name = "hotpads"
    display_name = "HotPads"
    capabilities: ClassVar[Capabilities] = _PROVIDER
    site_url = "https://hotpads.com/"
