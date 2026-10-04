"""SourceRegistry, discovery and the SourceAdapter contract, with fake adapters."""

import sys
import textwrap
from pathlib import Path

import pytest

from furnest.config import AdapterOverrides, SourcesSettings
from furnest.domain import Criteria, NormalizedListing
from furnest.sources import (
    Capabilities,
    SourceAdapter,
    SourceConfigError,
    SourceRegistry,
    discover,
)


def _listing(source_id: str, title: str = "Furnished 1BR") -> NormalizedListing:
    return NormalizedListing(
        source="fake",
        source_id=source_id,
        url=f"https://listings.example.com/{source_id}",  # type: ignore[arg-type]
        title=title,
    )


class FakeAdapter(SourceAdapter):
    name = "fake"
    display_name = "Fake Source"
    capabilities = Capabilities(has_api=True, provides_contact=True)
    default_enabled = True

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        return [_listing("1"), _listing("2")]

    def fetch_detail(self, listing: NormalizedListing) -> NormalizedListing:
        return listing.model_copy(update={"description": "Detail text"})


class BrowserAdapter(SourceAdapter):
    name = "browser_only"
    display_name = "Browser Only"
    capabilities = Capabilities(needs_browser=True)

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        return []


def _browser_available(settings: SourcesSettings) -> str | None:
    return None


@pytest.fixture
def registry() -> SourceRegistry:
    registry = SourceRegistry(browser_check=_browser_available)
    registry.register(FakeAdapter)
    registry.register(BrowserAdapter)
    return registry


def test_register_get_and_names(registry: SourceRegistry) -> None:
    assert registry.names() == ["browser_only", "fake"]
    assert registry.get("fake") is FakeAdapter
    assert registry.get("missing") is None


def test_register_works_as_a_decorator() -> None:
    registry = SourceRegistry()

    @registry.register
    class Decorated(SourceAdapter):
        name = "decorated"
        display_name = "Decorated"

        def search(self, criteria: Criteria) -> list[NormalizedListing]:
            return []

    assert registry.get("decorated") is Decorated


def test_registering_the_same_class_twice_is_harmless(registry: SourceRegistry) -> None:
    registry.register(FakeAdapter)

    assert registry.names() == ["browser_only", "fake"]


def test_duplicate_name_is_rejected(registry: SourceRegistry) -> None:
    class Impostor(FakeAdapter):
        pass

    with pytest.raises(SourceConfigError, match="used by both"):
        registry.register(Impostor)


@pytest.mark.parametrize("name", ["", "Fake", "1fake", "fake-source", "fake source"])
def test_invalid_names_are_rejected(name: str) -> None:
    class Bad(SourceAdapter):
        display_name = "Bad"

        def search(self, criteria: Criteria) -> list[NormalizedListing]:
            return []

    Bad.name = name

    with pytest.raises(SourceConfigError, match="name must match"):
        SourceRegistry().register(Bad)


def test_missing_name_or_display_name_is_rejected() -> None:
    class Nameless(SourceAdapter):
        display_name = "Nameless"

        def search(self, criteria: Criteria) -> list[NormalizedListing]:
            return []

    class Unlabelled(SourceAdapter):
        name = "unlabelled"

        def search(self, criteria: Criteria) -> list[NormalizedListing]:
            return []

    with pytest.raises(SourceConfigError, match="name must match"):
        SourceRegistry().register(Nameless)
    with pytest.raises(SourceConfigError, match="display_name"):
        SourceRegistry().register(Unlabelled)


def test_abstract_adapter_is_rejected() -> None:
    class Incomplete(SourceAdapter):
        name = "incomplete"
        display_name = "Incomplete"

    with pytest.raises(SourceConfigError, match="abstract"):
        SourceRegistry().register(Incomplete)


# --- Configuration ---------------------------------------------------------------


def test_configured_resolves_every_adapter(registry: SourceRegistry) -> None:
    settings = SourcesSettings(adapters={"fake": AdapterOverrides(interval_minutes=15)})

    configured = registry.configured(settings)

    assert [(c.adapter, c.config.enabled) for c in configured] == [
        (BrowserAdapter, False),
        (FakeAdapter, True),
    ]
    assert configured[1].config.interval_minutes == 15
    assert configured[0].config.interval_minutes == settings.default_interval_minutes


def test_unknown_adapter_in_overrides_is_an_error(registry: SourceRegistry) -> None:
    settings = SourcesSettings(adapters={"faek": AdapterOverrides(enabled=True)})

    with pytest.raises(
        SourceConfigError, match=r"unknown adapter.*faek.*known: browser_only, fake"
    ):
        registry.configured(settings)


def test_enabled_instantiates_only_enabled_adapters(registry: SourceRegistry) -> None:
    adapters = registry.enabled(SourcesSettings(), {"fake": None, "browser_only": False})

    assert [type(a) for a in adapters] == [FakeAdapter]
    assert adapters[0].config.name == "fake"


def test_dashboard_toggle_flows_through_enabled(registry: SourceRegistry) -> None:
    adapters = registry.enabled(SourcesSettings(), {"fake": False, "browser_only": True})

    assert [type(a) for a in adapters] == [BrowserAdapter]


def test_needs_browser_adapter_is_disabled_with_reason_when_browser_unavailable() -> None:
    checked: list[SourcesSettings] = []

    def unavailable(settings: SourcesSettings) -> str | None:
        checked.append(settings)
        return "Playwright is not installed"

    registry = SourceRegistry(browser_check=unavailable)
    registry.register(FakeAdapter)
    registry.register(BrowserAdapter)
    settings = SourcesSettings(adapters={"browser_only": AdapterOverrides(enabled=True)})

    browser, fake = registry.configured(settings, {"browser_only": True})

    assert checked == [settings]
    assert browser.config.enabled is False
    assert browser.config.enabled_pinned is True
    assert browser.config.disabled_reason == "needs a browser: Playwright is not installed"
    assert fake.config.enabled is True
    assert fake.config.disabled_reason is None
    assert [type(a) for a in registry.enabled(settings)] == [FakeAdapter]


def test_default_browser_check_uses_sources_browser_enabled() -> None:
    registry = SourceRegistry()
    registry.register(BrowserAdapter)

    (configured,) = registry.configured(SourcesSettings(browser_enabled=False))

    assert configured.config.disabled_reason == "needs a browser: SOURCES_BROWSER_ENABLED is false"


def test_browser_check_skipped_when_no_adapter_needs_a_browser() -> None:
    def explode(settings: SourcesSettings) -> str | None:
        raise AssertionError("browser check should not run")

    registry = SourceRegistry(browser_check=explode)
    registry.register(FakeAdapter)

    (configured,) = registry.configured(SourcesSettings())

    assert configured.config.enabled is True


def test_fake_adapter_search_and_detail(registry: SourceRegistry) -> None:
    (adapter,) = registry.enabled(SourcesSettings())

    results = adapter.search(Criteria())
    detailed = adapter.fetch_detail(results[0])

    assert [r.source_id for r in results] == ["1", "2"]
    assert detailed.description == "Detail text"
    assert adapter.capabilities.provides_contact is True
    assert adapter.capabilities.needs_browser is False


def test_default_fetch_detail_returns_the_listing() -> None:
    registry = SourceRegistry(browser_check=_browser_available)
    registry.register(BrowserAdapter)
    (configured,) = registry.configured(SourcesSettings())
    listing = _listing("9")

    assert configured.create().fetch_detail(listing) is listing


# --- Discovery -------------------------------------------------------------------


def test_discover_built_in_package_loads() -> None:
    registry = discover()

    # No real adapters ship yet (#24 onwards); every name must still be valid.
    for name in registry.names():
        assert registry.get(name) is not None


def test_discover_finds_adapters_in_a_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "fake_sources"
    (package / "nested").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "nested" / "__init__.py").write_text("")
    (package / "alpha.py").write_text(
        textwrap.dedent(
            """
            from furnest.sources import SourceAdapter

            class AlphaBase(SourceAdapter):
                pass

            class Alpha(AlphaBase):
                name = "alpha"
                display_name = "Alpha"

                def search(self, criteria):
                    return []
            """
        )
    )
    # Re-imports Alpha; it must not be registered twice or under this module.
    (package / "nested" / "beta.py").write_text(
        textwrap.dedent(
            """
            from fake_sources.alpha import Alpha
            from furnest.sources import SourceAdapter

            class Beta(SourceAdapter):
                name = "beta"
                display_name = "Beta"

                def search(self, criteria):
                    return []
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    for module in [m for m in sys.modules if m.startswith("fake_sources")]:
        monkeypatch.delitem(sys.modules, module)

    registry = discover("fake_sources")

    assert registry.names() == ["alpha", "beta"]
