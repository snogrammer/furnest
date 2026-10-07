"""Placeholders for sources that are researched but not built yet.

A stub is registered like any adapter, so it has a ``sources`` row and shows up on
the dashboard, but :func:`furnest.sources.config.resolve_config` always disables it
with a "not built yet" reason. Nothing schedules it, the toggle refuses it, and
pinning it on in ``SOURCES_ADAPTERS`` has no effect. Its ``capabilities`` describe
the access route the research found, and its docstring holds the TODO for whoever
picks it up. ``docs/adapters.md`` (Candidates) has the findings.

Building one means deleting its class from ``furnest.sources.adapters.stubs`` and
adding ``furnest.sources.adapters.<name>``. This base lives outside that package, so
:func:`furnest.sources.registry.discover` never registers it.
"""

from __future__ import annotations

from typing import ClassVar

from furnest.domain import Criteria, NormalizedListing
from furnest.sources.base import SourceAdapter
from furnest.sources.errors import SourceError


class StubAdapter(SourceAdapter):
    implemented: ClassVar[bool] = False
    # The source's homepage, for the error below and for whoever builds it.
    site_url: ClassVar[str]

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        # Unreachable through the scheduler, which never runs a disabled source.
        msg = f"{self.display_name} is a stub and can't search yet"
        raise SourceError(msg, url=self.site_url)
