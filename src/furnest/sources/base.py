"""The contract every listing source implements.

An adapter is a class with some static metadata (``name``, ``display_name``,
``capabilities``, ``default_enabled``) and two operations:

* ``search(criteria)`` returns the listings the source currently has for the
  active criteria, already mapped onto :class:`NormalizedListing`;
* ``fetch_detail(listing)`` fills in what a search result page leaves out. Sources
  whose search results are already complete keep the default, which returns the
  listing unchanged.

Adapters are synchronous: they run in worker threads (see ``furnest.worker``).
The registry reads the class metadata before anything is instantiated, which is
why this is an abstract base class rather than a ``typing.Protocol``. Each
instance gets its resolved :class:`AdapterConfig`, and
:class:`furnest.sources.http.SourceHttpClient` builds its client, rate limiter and
proxy from it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from pydantic import SecretStr

from furnest.domain import Criteria, NormalizedListing


@dataclass(frozen=True)
class Capabilities:
    """What an adapter can do and needs; used for scheduling, enrichment and the UI."""

    needs_browser: bool = False
    has_api: bool = False
    supports_geo_search: bool = False
    provides_contact: bool = False
    requires_api_key: bool = False


@dataclass(frozen=True)
class AdapterConfig:
    """One adapter's settings after ``SOURCES_ADAPTERS`` overrides and the
    dashboard toggle are applied (see :func:`furnest.sources.config.resolve_config`).

    ``enabled_pinned`` is true when ``SOURCES_ADAPTERS`` sets ``enabled``, so the
    dashboard toggle has no effect. ``disabled_reason`` is set when the adapter
    can't run whatever the toggles say, e.g. it needs a browser and none is
    available; ``enabled`` is then false. ``api_key`` stays a ``SecretStr`` so it never
    shows up in a repr or a log line.
    """

    name: str
    enabled: bool
    enabled_pinned: bool
    interval_minutes: int
    jitter_seconds: int
    requests_per_minute: float
    user_agent: str
    request_timeout_seconds: int
    respect_robots: bool = True
    max_retries: int = 3
    run_timeout_seconds: int = 900
    search_locations: tuple[str, ...] = ()
    max_results: int = 100
    proxy_url: str | None = None
    api_key: SecretStr | None = None
    disabled_reason: str | None = None


class SourceAdapter(ABC):
    name: ClassVar[str]
    display_name: ClassVar[str]
    capabilities: ClassVar[Capabilities] = Capabilities()
    # Whether the adapter runs when neither env nor the dashboard says otherwise.
    default_enabled: ClassVar[bool] = False
    # False for a registered placeholder (see furnest.sources.stub): it can't be enabled.
    implemented: ClassVar[bool] = True

    def __init__(self, config: AdapterConfig) -> None:
        self.config = config

    @abstractmethod
    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        """Listings the source has right now that may match ``criteria``."""

    def fetch_detail(self, listing: NormalizedListing) -> NormalizedListing:
        """``listing`` completed from its detail page. The default returns it as is."""
        return listing
