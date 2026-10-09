"""Resolving one adapter's :class:`AdapterConfig` from env settings and the DB toggle.

Every field falls back from the adapter's ``SOURCES_ADAPTERS`` entry to the global
``SOURCES_*`` value. ``enabled`` follows the same precedence as
:class:`furnest.config.RuntimeSettings`:

1. ``enabled`` set in the adapter's ``SOURCES_ADAPTERS`` entry pins it;
2. otherwise the dashboard toggle stored in ``sources.enabled``, if any;
3. otherwise the adapter's ``default_enabled``.

An adapter that can't run is disabled whatever the above says, with the reason in
``disabled_reason``: a stub that isn't built yet (``implemented`` false, see
:mod:`furnest.sources.stub`), one with ``needs_browser`` while the browser layer is
unavailable (see :func:`furnest.sources.browser.browser_unavailable_reason`), or one
with ``requires_api_key`` and no ``api_key`` in its ``SOURCES_ADAPTERS`` entry.
"""

from __future__ import annotations

from furnest.config import AdapterOverrides, SourcesSettings
from furnest.sources.base import AdapterConfig, SourceAdapter

NOT_BUILT_REASON = "not built yet: a stub, see Candidates in docs/adapters.md"


class SourceConfigError(ValueError):
    """``SOURCES_ADAPTERS`` or an adapter class is misconfigured."""


def resolve_config(
    adapter: type[SourceAdapter],
    settings: SourcesSettings,
    stored_enabled: bool | None = None,
    *,
    browser_unavailable: str | None = None,
) -> AdapterConfig:
    """``browser_unavailable`` is why no browser can be used, or ``None`` if one can."""
    overrides = settings.adapters.get(adapter.name, AdapterOverrides())

    if overrides.enabled is not None:
        enabled, pinned = overrides.enabled, True
    elif stored_enabled is not None:
        enabled, pinned = stored_enabled, False
    else:
        enabled, pinned = adapter.default_enabled, False

    disabled_reason = None
    if not adapter.implemented:
        enabled, disabled_reason = False, NOT_BUILT_REASON
    elif adapter.capabilities.needs_browser and browser_unavailable is not None:
        enabled, disabled_reason = False, f"needs a browser: {browser_unavailable}"
    elif adapter.capabilities.requires_api_key and overrides.api_key is None:
        enabled = False
        disabled_reason = f'needs an API key: set "api_key" for {adapter.name} in SOURCES_ADAPTERS'

    proxy = overrides.proxy_url or settings.proxy_url
    return AdapterConfig(
        name=adapter.name,
        enabled=enabled,
        enabled_pinned=pinned,
        interval_minutes=_pick(overrides.interval_minutes, settings.default_interval_minutes),
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
        search_locations=overrides.search_locations or (),
        max_results=_pick(overrides.max_results, settings.max_results),
        proxy_url=str(proxy) if proxy is not None else None,
        api_key=overrides.api_key,
        disabled_reason=disabled_reason,
    )


def _pick[T](override: T | None, default: T) -> T:
    return default if override is None else override
