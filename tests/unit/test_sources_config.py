"""resolve_config: per-field fallback to SOURCES_* and the enabled precedence."""

import pytest
from pydantic import SecretStr

from furnest.config import AdapterOverrides, SourcesSettings
from furnest.domain import Criteria, NormalizedListing
from furnest.sources import SourceAdapter, resolve_config


class OffByDefault(SourceAdapter):
    name = "off_by_default"
    display_name = "Off by default"

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        return []


class OnByDefault(OffByDefault):
    name = "on_by_default"
    default_enabled = True


def test_global_defaults_flow_through() -> None:
    settings = SourcesSettings(proxy_url="http://proxy.example.com:8080")  # type: ignore[arg-type]

    config = resolve_config(OffByDefault, settings)

    assert config.name == "off_by_default"
    assert config.interval_minutes == settings.default_interval_minutes
    assert config.jitter_seconds == settings.jitter_seconds
    assert config.requests_per_minute == settings.requests_per_minute
    assert config.user_agent == settings.user_agent
    assert config.request_timeout_seconds == settings.request_timeout_seconds
    assert config.proxy_url == "http://proxy.example.com:8080/"
    assert config.api_key is None


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
