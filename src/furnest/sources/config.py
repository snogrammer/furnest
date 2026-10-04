"""Resolving one adapter's :class:`AdapterConfig` from env settings and the DB toggle.

Every field falls back from the adapter's ``SOURCES_ADAPTERS`` entry to the global
``SOURCES_*`` value. ``enabled`` follows the same precedence as
:class:`furnest.config.RuntimeSettings`:

1. ``enabled`` set in the adapter's ``SOURCES_ADAPTERS`` entry pins it;
2. otherwise the dashboard toggle stored in ``sources.enabled``, if any;
3. otherwise the adapter's ``default_enabled``.
"""

from __future__ import annotations

from furnest.config import AdapterOverrides, SourcesSettings
from furnest.sources.base import AdapterConfig, SourceAdapter


class SourceConfigError(ValueError):
    """``SOURCES_ADAPTERS`` or an adapter class is misconfigured."""


def resolve_config(
    adapter: type[SourceAdapter],
    settings: SourcesSettings,
    stored_enabled: bool | None = None,
) -> AdapterConfig:
    overrides = settings.adapters.get(adapter.name, AdapterOverrides())

    if overrides.enabled is not None:
        enabled, pinned = overrides.enabled, True
    elif stored_enabled is not None:
        enabled, pinned = stored_enabled, False
    else:
        enabled, pinned = adapter.default_enabled, False

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
        max_retries=_pick(overrides.max_retries, settings.max_retries),
        proxy_url=str(proxy) if proxy is not None else None,
        api_key=overrides.api_key,
    )


def _pick[T](override: T | None, default: T) -> T:
    return default if override is None else override
