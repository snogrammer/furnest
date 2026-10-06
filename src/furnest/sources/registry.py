"""The set of known adapters, and turning them into configured instances.

Built-in adapters live one module (or package) each under
``furnest.sources.adapters``. :func:`discover` imports them all and registers every
concrete :class:`SourceAdapter` subclass it finds, so adding a source means adding a
module, nothing else. Tests build their own :class:`SourceRegistry` with fakes.

Adapters whose capabilities say ``needs_browser`` are disabled, with a
``disabled_reason``, when the browser layer is unavailable. The worker stores
those reasons with :meth:`furnest.db.source_store.SqlSourceStore.set_disabled_reasons`
so the dashboard can say why a source isn't running.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from furnest.config import ADAPTER_NAME_PATTERN, SourcesSettings
from furnest.logging import get_logger
from furnest.sources.base import AdapterConfig, SourceAdapter
from furnest.sources.browser import browser_unavailable_reason
from furnest.sources.config import SourceConfigError, resolve_config

log = get_logger(__name__)

ADAPTERS_PACKAGE = "furnest.sources.adapters"


@dataclass(frozen=True)
class ConfiguredSource:
    adapter: type[SourceAdapter]
    config: AdapterConfig

    def create(self) -> SourceAdapter:
        return self.adapter(self.config)


class SourceRegistry:
    def __init__(
        self,
        *,
        browser_check: Callable[[SourcesSettings], str | None] = browser_unavailable_reason,
    ) -> None:
        self._adapters: dict[str, type[SourceAdapter]] = {}
        self._browser_check = browser_check

    def register[A: type[SourceAdapter]](self, adapter: A) -> A:
        """Add ``adapter``; returns it, so this also works as a class decorator."""
        name = getattr(adapter, "name", None)
        if not isinstance(name, str) or not re.fullmatch(ADAPTER_NAME_PATTERN, name):
            msg = f"{adapter.__qualname__}.name must match {ADAPTER_NAME_PATTERN}, got {name!r}"
            raise SourceConfigError(msg)
        if not getattr(adapter, "display_name", None):
            msg = f"{adapter.__qualname__} must set display_name"
            raise SourceConfigError(msg)
        if inspect.isabstract(adapter):
            msg = f"{adapter.__qualname__} is abstract and can't be registered"
            raise SourceConfigError(msg)
        existing = self._adapters.get(name)
        if existing is not None and existing is not adapter:
            msg = (
                f"adapter name {name!r} is used by both "
                f"{existing.__qualname__} and {adapter.__qualname__}"
            )
            raise SourceConfigError(msg)
        self._adapters[name] = adapter
        return adapter

    def get(self, name: str) -> type[SourceAdapter] | None:
        return self._adapters.get(name)

    def display_name(self, name: str) -> str:
        """The adapter's display name, or ``name`` itself if no adapter has it."""
        adapter = self.get(name)
        return adapter.display_name if adapter is not None else name

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def configured(
        self,
        settings: SourcesSettings,
        stored_enabled: Mapping[str, bool | None] | None = None,
    ) -> list[ConfiguredSource]:
        """Every registered adapter with its resolved config, sorted by name.

        Raises :class:`SourceConfigError` when ``SOURCES_ADAPTERS`` names an adapter
        that doesn't exist, so a typo can't silently leave a source unconfigured.
        """
        unknown = sorted(set(settings.adapters) - set(self._adapters))
        if unknown:
            msg = (
                f"SOURCES_ADAPTERS names unknown adapter(s): {', '.join(unknown)}; "
                f"known: {', '.join(self.names()) or 'none'}"
            )
            raise SourceConfigError(msg)
        stored = stored_enabled or {}
        needs_browser = any(a.capabilities.needs_browser for a in self._adapters.values())
        browser_unavailable = self._browser_check(settings) if needs_browser else None
        sources = [
            ConfiguredSource(
                adapter,
                resolve_config(
                    adapter, settings, stored.get(name), browser_unavailable=browser_unavailable
                ),
            )
            for name, adapter in sorted(self._adapters.items())
        ]
        for source in sources:
            if source.config.disabled_reason is not None:
                log.warning(
                    "source.unavailable",
                    adapter=source.config.name,
                    reason=source.config.disabled_reason,
                )
        return sources

    def enabled(
        self,
        settings: SourcesSettings,
        stored_enabled: Mapping[str, bool | None] | None = None,
    ) -> list[SourceAdapter]:
        """Instances of the adapters that are enabled after config resolution."""
        return [
            source.create()
            for source in self.configured(settings, stored_enabled)
            if source.config.enabled
        ]


def discover(package: str = ADAPTERS_PACKAGE) -> SourceRegistry:
    """Import every module under ``package`` and register the adapters defined there.

    Only classes defined in the scanned modules count, so an adapter module may
    import another adapter (or a base class) without registering it twice.
    """
    registry = SourceRegistry()
    root = importlib.import_module(package)
    modules = [root]
    for info in pkgutil.walk_packages(root.__path__, prefix=f"{package}."):
        modules.append(importlib.import_module(info.name))
    for module in modules:
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, SourceAdapter)
                and obj.__module__ == module.__name__
                and not inspect.isabstract(obj)
            ):
                registry.register(obj)
    return registry
