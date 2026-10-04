"""Listing sources: the SourceAdapter contract, per-adapter config and the registry.

The HTTP and browser layers (#20, #21) and the adapters themselves build on this.
"""

from furnest.sources.base import AdapterConfig, Capabilities, SourceAdapter
from furnest.sources.config import SourceConfigError, resolve_config
from furnest.sources.registry import ConfiguredSource, SourceRegistry, discover

__all__ = [
    "AdapterConfig",
    "Capabilities",
    "ConfiguredSource",
    "SourceAdapter",
    "SourceConfigError",
    "SourceRegistry",
    "discover",
    "resolve_config",
]
