"""Resolving one adapter's :class:`AdapterConfig` from env settings and the dashboard.

Every field falls back from the adapter's ``SOURCES_ADAPTERS`` entry to the global
``SOURCES_*`` value. Five of them can also be set on the dashboard, and follow the
same precedence as :class:`furnest.config.RuntimeSettings`: a value in
``SOURCES_ADAPTERS`` pins the field, otherwise the stored dashboard value applies,
otherwise the default.

* ``enabled``: pinned by env, else the toggle stored in ``sources.enabled``, else the
  adapter's ``default_enabled``;
* ``search_locations``, ``interval_minutes``, ``max_results``: pinned by env, else the
  :class:`~furnest.config.SourceOverrides` stored in ``sources.overrides``, else the
  adapter's own ``default_interval_minutes`` / ``default_max_results`` if it has one,
  else the ``SOURCES_*`` default (no locations);
* ``api_key``: pinned by env, else the shared :class:`~furnest.sources.base.Credential`
  the adapter declares, as stored from the dashboard, else none.

The names of the fields env pins are in ``AdapterConfig.pinned``, so the dashboard
can show them read-only.

An adapter that can't run is disabled whatever the above says, with the cause in
``blocker`` and the text in ``disabled_reason``: a stub that isn't built yet
(``implemented`` false, see :mod:`furnest.sources.stub`), one known to be broken
(``broken_reason`` set), one with ``needs_browser``
while the browser layer is unavailable (see
:func:`furnest.sources.browser.browser_unavailable_reason`), or one with
``requires_api_key`` and no ``api_key`` from either place.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import SecretStr

from furnest.config import DASHBOARD_FIELDS, AdapterOverrides, SourceOverrides, SourcesSettings
from furnest.sources.base import AdapterConfig, Blocker, SourceAdapter

NOT_BUILT_REASON = "not built yet: a stub, see Candidates in docs/adapters.md"


class SourceConfigError(ValueError):
    """``SOURCES_ADAPTERS`` or an adapter class is misconfigured."""


@dataclass(frozen=True)
class StoredSource:
    """What the dashboard stored for one source: its toggle and its settings."""

    enabled: bool | None = None
    overrides: SourceOverrides = field(default_factory=SourceOverrides)


@dataclass(frozen=True)
class StoredSources:
    """Everything the dashboard stored that config resolution reads: each source's
    :class:`StoredSource` by name, and each shared credential's token by key."""

    sources: Mapping[str, StoredSource] = field(default_factory=dict)
    credentials: Mapping[str, str] = field(default_factory=dict)


def resolve_config(
    adapter: type[SourceAdapter],
    settings: SourcesSettings,
    stored: StoredSource | None = None,
    *,
    credentials: Mapping[str, str] | None = None,
    browser_unavailable: str | None = None,
) -> AdapterConfig:
    """``stored`` is the dashboard's toggle and settings for this source, ``credentials``
    the dashboard's tokens by credential key, and ``browser_unavailable`` why no browser
    can be used, or ``None`` if one can."""
    overrides = settings.adapters.get(adapter.name, AdapterOverrides())
    stored = stored or StoredSource()
    pinned = frozenset(
        name
        for name in ("enabled", "api_key", *DASHBOARD_FIELDS)
        if getattr(overrides, name) is not None
    )

    if overrides.enabled is not None:
        enabled = overrides.enabled
    elif stored.enabled is not None:
        enabled = stored.enabled
    else:
        enabled = adapter.default_enabled

    api_key = overrides.api_key
    if api_key is None and adapter.credential is not None and credentials:
        token = credentials.get(adapter.credential.key)
        api_key = SecretStr(token) if token else None

    blocker = disabled_reason = None
    if not adapter.implemented:
        blocker, disabled_reason = Blocker.NOT_BUILT, NOT_BUILT_REASON
    elif adapter.broken_reason is not None:
        blocker, disabled_reason = Blocker.BROKEN, f"turned off: {adapter.broken_reason}"
    elif adapter.capabilities.needs_browser and browser_unavailable is not None:
        blocker, disabled_reason = Blocker.NO_BROWSER, f"needs a browser: {browser_unavailable}"
    elif adapter.capabilities.requires_api_key and api_key is None:
        blocker = Blocker.NO_CREDENTIAL
        if adapter.credential is not None:
            disabled_reason = (
                f"needs the {adapter.credential.label}: add it under Credentials on the "
                "Sources page"
            )
        else:
            disabled_reason = (
                f'needs an API key: set "api_key" for {adapter.name} in SOURCES_ADAPTERS'
            )
    if blocker is not None:
        enabled = False

    proxy = overrides.proxy_url or settings.proxy_url
    return AdapterConfig(
        name=adapter.name,
        enabled=enabled,
        interval_minutes=_pick(
            overrides.interval_minutes,
            stored.overrides.interval_minutes,
            adapter.default_interval_minutes,
            settings.default_interval_minutes,
        ),
        jitter_seconds=_pick(overrides.jitter_seconds, settings.jitter_seconds),
        requests_per_minute=_pick(overrides.requests_per_minute, settings.requests_per_minute),
        user_agent=_pick(overrides.user_agent, settings.user_agent),
        request_timeout_seconds=_pick(
            overrides.request_timeout_seconds, settings.request_timeout_seconds
        ),
        respect_robots=_pick(overrides.respect_robots, settings.respect_robots),
        allow_private_addresses=settings.allow_private_addresses,
        max_retries=_pick(overrides.max_retries, settings.max_retries),
        run_timeout_seconds=_pick(overrides.run_timeout_seconds, settings.run_timeout_seconds),
        search_locations=_pick(overrides.search_locations, stored.overrides.search_locations, ()),
        max_results=_pick(
            overrides.max_results,
            stored.overrides.max_results,
            adapter.default_max_results,
            settings.max_results,
        ),
        max_charge_usd=overrides.max_charge_usd,
        proxy_url=str(proxy) if proxy is not None else None,
        api_key=api_key,
        pinned=pinned,
        blocker=blocker,
        disabled_reason=disabled_reason,
    )


def _pick[T](*candidates: T | None) -> T:
    """The first candidate that is set; the last one is the default and must be set."""
    for candidate in candidates[:-1]:
        if candidate is not None:
            return candidate
    default = candidates[-1]
    assert default is not None  # noqa: S101 - the default is always a value
    return default
