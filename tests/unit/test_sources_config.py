"""resolve_config: per-field fallback to SOURCES_* and the enabled precedence."""

import pytest
from pydantic import SecretStr

from furnest.config import AdapterOverrides, SourcesSettings
from furnest.domain import Criteria, NormalizedListing
from furnest.sources import Capabilities, SourceAdapter, resolve_config


class OffByDefault(SourceAdapter):
    name = "off_by_default"
    display_name = "Off by default"

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        return []


class OnByDefault(OffByDefault):
    name = "on_by_default"
    default_enabled = True


class NeedsBrowser(OnByDefault):
    name = "needs_browser"
    capabilities = Capabilities(needs_browser=True)


def test_global_defaults_flow_through() -> None:
    settings = SourcesSettings(proxy_url="http://proxy.example.com:8080")  # type: ignore[arg-type]

    config = resolve_config(OffByDefault, settings)

    assert config.name == "off_by_default"
    assert config.interval_minutes == settings.default_interval_minutes
    assert config.jitter_seconds == settings.jitter_seconds
    assert config.requests_per_minute == settings.requests_per_minute
    assert config.user_agent == settings.user_agent
    assert config.request_timeout_seconds == settings.request_timeout_seconds
    assert config.run_timeout_seconds == settings.run_timeout_seconds == 900
    assert config.max_results == settings.max_results == 100
    assert config.search_locations == ()
    assert config.proxy_url == "http://proxy.example.com:8080/"
    assert config.api_key is None
    assert config.respect_robots is True
    assert config.max_retries == 3


def test_overrides_win_field_by_field() -> None:
    settings = SourcesSettings(
        proxy_url="http://proxy.example.com:8080",  # type: ignore[arg-type]
        adapters={
            "off_by_default": AdapterOverrides(
                interval_minutes=10,
                jitter_seconds=0,
                requests_per_minute=1.5,
                user_agent="custom-agent",
                proxy_url="socks5://socks.example.com:1080",  # type: ignore[arg-type]
                api_key=SecretStr("key-123"),
                request_timeout_seconds=5,
                respect_robots=False,
                max_retries=0,
                run_timeout_seconds=120,
                search_locations=("Exampleville, XX", " Othertown, YY "),
                max_results=25,
            )
        },
    )

    config = resolve_config(OffByDefault, settings)

    assert config.interval_minutes == 10
    assert config.jitter_seconds == 0
    assert config.requests_per_minute == 1.5
    assert config.user_agent == "custom-agent"
    assert config.proxy_url == "socks5://socks.example.com:1080"
    assert config.api_key is not None
    assert config.api_key.get_secret_value() == "key-123"
    assert config.request_timeout_seconds == 5
    assert config.respect_robots is False
    assert config.max_retries == 0
    assert config.run_timeout_seconds == 120
    assert config.search_locations == ("Exampleville, XX", "Othertown, YY")
    assert config.max_results == 25


def test_overrides_for_one_adapter_do_not_leak_to_another() -> None:
    settings = SourcesSettings(adapters={"on_by_default": AdapterOverrides(interval_minutes=5)})

    assert resolve_config(OffByDefault, settings).interval_minutes == 60


@pytest.mark.parametrize(
    ("adapter", "env", "stored", "enabled", "pinned"),
    [
        # Nothing set: the adapter's default.
        (OffByDefault, None, None, False, False),
        (OnByDefault, None, None, True, False),
        # The dashboard toggle beats the default.
        (OffByDefault, None, True, True, False),
        (OnByDefault, None, False, False, False),
        # An explicit env value beats the dashboard toggle and pins it.
        (OffByDefault, True, False, True, True),
        (OnByDefault, False, True, False, True),
        (OnByDefault, False, None, False, True),
    ],
)
def test_enabled_precedence(
    adapter: type[SourceAdapter],
    env: bool | None,
    stored: bool | None,
    enabled: bool,
    pinned: bool,
) -> None:
    overrides = {} if env is None else {adapter.name: AdapterOverrides(enabled=env)}
    settings = SourcesSettings(adapters=overrides)

    config = resolve_config(adapter, settings, stored)

    assert (config.enabled, config.enabled_pinned) == (enabled, pinned)


def test_api_key_is_not_in_the_repr() -> None:
    settings = SourcesSettings(
        adapters={"off_by_default": AdapterOverrides(api_key=SecretStr("super-secret"))}
    )

    config = resolve_config(OffByDefault, settings)

    assert "super-secret" not in repr(config)


@pytest.mark.parametrize("stored", [None, True])
def test_needs_browser_without_browser_is_disabled_with_reason(stored: bool | None) -> None:
    config = resolve_config(
        NeedsBrowser, SourcesSettings(), stored, browser_unavailable="no Playwright"
    )

    assert config.enabled is False
    assert config.disabled_reason == "needs a browser: no Playwright"


def test_needs_browser_disable_beats_an_env_pin() -> None:
    settings = SourcesSettings(adapters={"needs_browser": AdapterOverrides(enabled=True)})

    config = resolve_config(NeedsBrowser, settings, browser_unavailable="no Playwright")

    assert (config.enabled, config.enabled_pinned) == (False, True)
    assert config.disabled_reason == "needs a browser: no Playwright"


def test_needs_browser_with_browser_follows_normal_precedence() -> None:
    config = resolve_config(NeedsBrowser, SourcesSettings(), browser_unavailable=None)

    assert config.enabled is True
    assert config.disabled_reason is None


def test_adapters_without_needs_browser_ignore_browser_unavailable() -> None:
    config = resolve_config(OnByDefault, SourcesSettings(), browser_unavailable="no Playwright")

    assert config.enabled is True
    assert config.disabled_reason is None


class NeedsKey(OnByDefault):
    name = "needs_key"
    capabilities = Capabilities(has_api=True, requires_api_key=True)


@pytest.mark.parametrize("stored", [None, True])
def test_requires_api_key_without_a_key_is_disabled_with_reason(stored: bool | None) -> None:
    config = resolve_config(NeedsKey, SourcesSettings(), stored)

    assert config.enabled is False
    assert config.disabled_reason == (
        'needs an API key: set "api_key" for needs_key in SOURCES_ADAPTERS'
    )


def test_requires_api_key_disable_beats_an_env_pin() -> None:
    settings = SourcesSettings(adapters={"needs_key": AdapterOverrides(enabled=True)})

    config = resolve_config(NeedsKey, settings)

    assert (config.enabled, config.enabled_pinned) == (False, True)
    assert config.disabled_reason is not None


def test_requires_api_key_with_a_key_follows_normal_precedence() -> None:
    settings = SourcesSettings(adapters={"needs_key": AdapterOverrides(api_key=SecretStr("k"))})

    assert resolve_config(NeedsKey, settings, False).enabled is False
    config = resolve_config(NeedsKey, settings)
    assert (config.enabled, config.disabled_reason) == (True, None)


def test_blank_search_locations_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least 1 character"):
        AdapterOverrides(search_locations=("  ",))
