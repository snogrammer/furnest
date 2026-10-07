"""Adapters backed by a data provider: a service that fetches a source and returns JSON.

Some sources can't be fetched directly (Furnished Finder and Zillow sit behind bot
checks, Airbnb's robots.txt disallows its search). furnest doesn't work around that
itself; a data provider does the fetching, and the adapter makes an authenticated API
call to it. See ``docs/adapters.md`` ("Data providers and self-hosted alternatives").

Two pieces:

* :class:`DataProvider` is the backend: given a *job* (the provider's own name for
  one extraction, such as an Apify actor ``"user/name"`` or a robot id on a
  self-hosted scraper) and its input, it runs the job and returns the result items.
  :class:`~furnest.sources.apify.ApifyProvider` is the one backend today.
* :class:`ProviderAdapter` is the adapter half. It checks the config (an API token
  and ``search_locations``), runs :attr:`~ProviderAdapter.job` on
  :attr:`~ProviderAdapter.provider` with the input a subclass builds, and hands the
  items back to the subclass to map. Each provider-backed source is then just its
  input builder and its item mapping.

The job's input and output schemas belong to the job, so moving a source to another
provider means a new :meth:`~ProviderAdapter.build_input` and
:meth:`~ProviderAdapter.parse_items`; what stays the same is everything around them.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any, ClassVar, Protocol

import httpx2

from furnest.domain import Criteria, NormalizedListing
from furnest.sources.base import AdapterConfig, Capabilities, SourceAdapter
from furnest.sources.errors import SourceError
from furnest.sources.http import SourceHttpClient

JsonObject = dict[str, Any]

# The provider gets this much less time than the whole run, so it stops the job (and
# we read why) before the worker's own run timeout fires.
PROVIDER_TIMEOUT_MARGIN_SECONDS = 60
MIN_PROVIDER_TIMEOUT_SECONDS = 60


class DataProvider(Protocol):
    """A service that runs a named extraction job and returns its items.

    :meth:`run` runs ``job`` on ``run_input`` within ``timeout_seconds`` and returns
    its items, or raises :class:`SourceError` when the job fails or doesn't finish in
    time.
    """

    #: Shown in messages, e.g. ``set "api_key" (an Apify API token)``.
    name: str

    def run(
        self,
        http: SourceHttpClient,
        job: str,
        run_input: Mapping[str, Any],
        *,
        token: str,
        timeout_seconds: int,
        clock: Callable[[], float],
    ) -> list[JsonObject]: ...


def _today() -> date:
    return datetime.now(UTC).date()


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


class ProviderAdapter(SourceAdapter):
    """A source read through a data provider job that searches by place name.

    Subclasses set :attr:`provider`, :attr:`job` and :attr:`site` (the source's
    origin, used in error messages) and implement :meth:`build_input` and
    :meth:`parse_items`. The token comes from the adapter's ``api_key``, the places
    from its ``search_locations``.
    """

    provider: ClassVar[DataProvider]
    job: ClassVar[str]
    site: ClassVar[str]
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

    @abstractmethod
    def build_input(self, criteria: Criteria) -> dict[str, Any]:
        """The job's input for ``criteria`` and this adapter's config."""

    @abstractmethod
    def parse_items(
        self, items: list[JsonObject], run_input: Mapping[str, Any]
    ) -> list[NormalizedListing]:
        """The job's result items as listings. Raise :class:`SourceError` to fail the run.

        ``run_input`` is what :meth:`build_input` sent, for a mapping that depends on the
        search (the stay dates an Airbnb price covers, say).
        """

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        config = self.config
        provider = self.provider.name
        if config.api_key is None:
            msg = (
                f'set "api_key" ({_article(provider)} {provider} API token) for {self.name} '
                "in SOURCES_ADAPTERS"
            )
            raise SourceError(msg, url=self.site)
        if not config.search_locations:
            msg = (
                f'set "search_locations" (e.g. ["Springfield, IL"]) for {self.name} '
                "in SOURCES_ADAPTERS"
            )
            raise SourceError(msg, url=self.site)

        run_input = self.build_input(criteria)
        timeout = max(
            MIN_PROVIDER_TIMEOUT_SECONDS,
            config.run_timeout_seconds - PROVIDER_TIMEOUT_MARGIN_SECONDS,
        )
        # robots.txt is for crawlers; this is an authenticated API call to the provider.
        http_config = replace(config, respect_robots=False)
        with SourceHttpClient(
            http_config, transport=self._transport, clock=self._clock, sleep=self._sleep
        ) as http:
            items = self.provider.run(
                http,
                self.job,
                run_input,
                token=config.api_key.get_secret_value(),
                timeout_seconds=timeout,
                clock=self._clock,
            )
        return self.parse_items(items, run_input)
