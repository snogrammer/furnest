"""Listing sources: the SourceAdapter contract, per-adapter config, the registry, the
HTTP layer adapters make requests through, the optional browser layer, and the base
for stubs of sources not built yet.
"""

from furnest.sources.base import AdapterConfig, Blocker, Capabilities, Credential, SourceAdapter
from furnest.sources.browser import (
    BrowserUnavailableError,
    SourceBrowser,
    browser_unavailable_reason,
)
from furnest.sources.config import (
    SourceConfigError,
    StoredSource,
    StoredSources,
    resolve_config,
)
from furnest.sources.errors import SourceBlockedError, SourceError
from furnest.sources.http import SourceHttpClient
from furnest.sources.ratelimit import TokenBucket
from furnest.sources.registry import ConfiguredSource, SourceRegistry, discover
from furnest.sources.robots import RobotsCache
from furnest.sources.stub import StubAdapter

__all__ = [
    "AdapterConfig",
    "Blocker",
    "BrowserUnavailableError",
    "Capabilities",
    "ConfiguredSource",
    "Credential",
    "RobotsCache",
    "SourceAdapter",
    "SourceBlockedError",
    "SourceBrowser",
    "SourceConfigError",
    "SourceError",
    "SourceHttpClient",
    "SourceRegistry",
    "StoredSource",
    "StoredSources",
    "StubAdapter",
    "TokenBucket",
    "browser_unavailable_reason",
    "discover",
    "resolve_config",
]
