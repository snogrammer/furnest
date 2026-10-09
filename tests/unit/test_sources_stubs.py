"""Stub adapters (#48): registered, but always disabled with a "not built yet" reason."""

import pytest

from furnest.config import AdapterOverrides, SourcesSettings
from furnest.domain import Criteria
from furnest.sources import Capabilities, SourceError, StubAdapter, discover, resolve_config
from furnest.sources.adapters.stubs import ApartmentsCom, Blueground, HotPads, Zumper
from furnest.sources.config import NOT_BUILT_REASON

STUBS: list[type[StubAdapter]] = [ApartmentsCom, Blueground, HotPads, Zumper]


class NeedsEverything(StubAdapter):
    name = "needs_everything"
    display_name = "Needs Everything"
    capabilities = Capabilities(needs_browser=True, requires_api_key=True)
    default_enabled = True
    site_url = "https://stub.example.com/"


def test_discover_registers_the_stubs_only() -> None:
    registry = discover()

    for stub in STUBS:
        assert registry.get(stub.name) is stub
        assert registry.display_name(stub.name) == stub.display_name
    assert registry.get("kopa") is None
    assert StubAdapter not in {registry.get(name) for name in registry.names()}


@pytest.mark.parametrize("stub", STUBS, ids=lambda s: s.name)
def test_stub_is_off_by_default_with_reason(stub: type[StubAdapter]) -> None:
    config = resolve_config(stub, SourcesSettings())

    assert config.enabled is False
    assert config.enabled_pinned is False
    assert config.disabled_reason == NOT_BUILT_REASON


@pytest.mark.parametrize("stub", STUBS, ids=lambda s: s.name)
def test_stub_stays_off_when_pinned_on_with_a_key(stub: type[StubAdapter]) -> None:
    settings = SourcesSettings(
        adapters={stub.name: AdapterOverrides(enabled=True, api_key="test-token")}  # type: ignore[arg-type]
    )

    config = resolve_config(stub, settings, stored_enabled=True)

    assert config.enabled is False
    assert config.disabled_reason == NOT_BUILT_REASON


def test_not_built_beats_browser_and_api_key_reasons() -> None:
    config = resolve_config(NeedsEverything, SourcesSettings(), browser_unavailable="no Playwright")

    assert config.enabled is False
    assert config.disabled_reason == NOT_BUILT_REASON


def test_registry_never_enables_a_stub() -> None:
    registry = discover()
    pinned = SourcesSettings(adapters={stub.name: AdapterOverrides(enabled=True) for stub in STUBS})
    stored = {stub.name: True for stub in STUBS}

    enabled = {adapter.name for adapter in registry.enabled(pinned, stored)}

    assert enabled.isdisjoint(stub.name for stub in STUBS)


@pytest.mark.parametrize("stub", STUBS, ids=lambda s: s.name)
def test_stub_search_raises_source_error(stub: type[StubAdapter]) -> None:
    adapter = stub(resolve_config(stub, SourcesSettings()))

    with pytest.raises(SourceError, match="is a stub") as exc:
        adapter.search(Criteria())

    assert exc.value.url == stub.site_url
