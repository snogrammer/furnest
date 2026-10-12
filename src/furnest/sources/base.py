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

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from pydantic import SecretStr

from furnest.config import ADAPTER_NAME_PATTERN
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
class Credential:
    """A token a source needs, entered once on the dashboard and shared by every
    adapter that declares the same ``key`` (the Apify token, say).

    ``key`` names it in storage and URLs, ``label`` is what the dashboard calls it,
    and ``help_url`` is where to get one.
    """

    key: str
    label: str
    help_url: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(ADAPTER_NAME_PATTERN, self.key):
            msg = f"Credential.key must match {ADAPTER_NAME_PATTERN}, got {self.key!r}"
            raise ValueError(msg)


class Blocker(StrEnum):
    """Why an adapter can't run whatever the toggles say."""

    NOT_BUILT = "not_built"
    NO_BROWSER = "no_browser"
    NO_CREDENTIAL = "no_credential"
    BROKEN = "broken"


@dataclass(frozen=True)
class AdapterConfig:
    """One adapter's settings after ``SOURCES_ADAPTERS`` overrides, the dashboard's
    stored settings and the dashboard toggle are applied (see
    :func:`furnest.sources.config.resolve_config`).

    ``pinned`` holds the names of the fields ``SOURCES_ADAPTERS`` sets, which the
    dashboard can't change (``enabled``, ``api_key``, ``search_locations``,
    ``interval_minutes``, ``max_results``). ``blocker`` and ``disabled_reason`` are set
    when the adapter can't run whatever the toggles say, e.g. it needs a browser and
    none is available; ``enabled`` is then false. ``api_key`` stays a ``SecretStr`` so it
    never shows up in a repr or a log line.
    """

    name: str
    enabled: bool
    interval_minutes: int
    jitter_seconds: int
    requests_per_minute: float
    user_agent: str
    request_timeout_seconds: int
    respect_robots: bool = True
    allow_private_addresses: bool = False
    max_retries: int = 3
    run_timeout_seconds: int = 900
    search_locations: tuple[str, ...] = ()
    max_results: int = 100
    # ``maxTotalChargeUsd`` for a pay-per-event provider run; ``None`` lets the adapter
    # pick (see :meth:`furnest.sources.apify.ApifyAdapter.max_charge`).
    max_charge_usd: float | None = None
    proxy_url: str | None = None
    api_key: SecretStr | None = None
    pinned: frozenset[str] = frozenset()
    blocker: Blocker | None = None
    disabled_reason: str | None = None

    @property
    def enabled_pinned(self) -> bool:
        """``SOURCES_ADAPTERS`` sets ``enabled``, so the dashboard toggle has no effect."""
        return "enabled" in self.pinned


class SourceAdapter(ABC):
    name: ClassVar[str]
    display_name: ClassVar[str]
    capabilities: ClassVar[Capabilities] = Capabilities()
    # Whether the adapter runs when neither env nor the dashboard says otherwise.
    default_enabled: ClassVar[bool] = False
    # False for a registered placeholder (see furnest.sources.stub): it can't be enabled.
    implemented: ClassVar[bool] = True
    # Set while a built adapter is known not to work (and would cost money failing):
    # it's off whatever env or the dashboard say, with this shown as the reason.
    broken_reason: ClassVar[str | None] = None
    # This source's own ``interval_minutes`` and ``max_results`` defaults, used when
    # neither ``SOURCES_ADAPTERS`` nor the dashboard sets them; ``None`` falls back to
    # the ``SOURCES_*`` default. Sources billed per run set them (see ApifyAdapter).
    default_interval_minutes: ClassVar[int | None] = None
    default_max_results: ClassVar[int | None] = None
    # The shared token this adapter's ``api_key`` comes from when ``SOURCES_ADAPTERS``
    # doesn't set one. Required when ``capabilities.requires_api_key`` and ``implemented``.
    credential: ClassVar[Credential | None] = None
    # What ``search_locations`` means for this source, shown next to the dashboard field.
    search_locations_hint: ClassVar[str] = 'Place names, one per line, e.g. "Springfield, IL".'

    # Whether ``search_locations`` are plain place names ("Austin, TX"), which the
    # criteria's cities can fill in; False for slugs and site names.
    search_locations_are_places: ClassVar[bool] = True

    def __init__(self, config: AdapterConfig) -> None:
        self.config = config

    @abstractmethod
    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        """Listings the source has right now that may match ``criteria``."""

    def fetch_detail(self, listing: NormalizedListing) -> NormalizedListing:
        """``listing`` completed from its detail page. The default returns it as is."""
        return listing
