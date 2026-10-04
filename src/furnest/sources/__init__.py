"""Listing sources: the SourceAdapter contract, per-adapter config, the registry, the
HTTP layer adapters make requests through, and the optional browser layer.
"""

from furnest.sources.base import AdapterConfig, Capabilities, SourceAdapter
from furnest.sources.browser import (
    BrowserUnavailableError,
    SourceBrowser,
    browser_unavailable_reason,
)
from furnest.sources.config import SourceConfigError, resolve_config
from furnest.sources.errors import SourceBlockedError, SourceError
from furnest.sources.http import SourceHttpClient
from furnest.sources.ratelimit import TokenBucket
from furnest.sources.registry import ConfiguredSource, SourceRegistry, discover
from furnest.sources.robots import RobotsCache

__all__ = [
    "AdapterConfig",
    "BrowserUnavailableError",
    "Capabilities",
    "ConfiguredSource",
    "RobotsCache",
    "SourceAdapter",
    "SourceBlockedError",
    "SourceBrowser",
    "SourceConfigError",
    "SourceError",
    "SourceHttpClient",
    "SourceRegistry",
    "TokenBucket",
    "browser_unavailable_reason",
    "discover",
    "resolve_config",
]
