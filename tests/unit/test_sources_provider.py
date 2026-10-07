"""ProviderAdapter: config checks, the provider's timeout, and the input reaching the
mapping. The provider is a fake that records what it was asked to run; run_actor has
its own tests in test_sources_apify.py."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr

from furnest.domain import Criteria, NormalizedListing
from furnest.sources import SourceError, SourceHttpClient
from furnest.sources.apify import APIFY, ApifyAdapter
from furnest.sources.base import AdapterConfig
from furnest.sources.provider import DataProvider, ProviderAdapter

pytestmark = pytest.mark.unit

TOKEN = "provider-test-token"  # noqa: S105 - a fake


class FakeProvider:
    def __init__(self, name: str = "Maxun") -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        http: SourceHttpClient,
        job: str,
        run_input: Mapping[str, Any],
        *,
        token: str,
        timeout_seconds: int,
        clock: Callable[[], float],
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "job": job,
                "input": dict(run_input),
                "token": token,
                "timeout": timeout_seconds,
                "respect_robots": http.config.respect_robots,
                "clock": clock,
            }
        )
        return [{"id": "a"}, {"id": "b"}]


class Example(ProviderAdapter):
    name: ClassVar[str] = "example_source"
    display_name: ClassVar[str] = "Example"
    provider: ClassVar[DataProvider] = FakeProvider()
    job: ClassVar[str] = "robot-123"
    site: ClassVar[str] = "https://example.com"

    def build_input(self, criteria: Criteria) -> dict[str, Any]:
        return {"places": list(self.config.search_locations), "beds": criteria.min_beds}

    def parse_items(
        self, items: list[dict[str, Any]], run_input: Mapping[str, Any]
    ) -> list[NormalizedListing]:
        self.seen = (items, dict(run_input))
        return []


def make_config(**overrides: Any) -> AdapterConfig:  # noqa: ANN401 - test helper
    config = AdapterConfig(
        name="example_source",
        enabled=True,
        enabled_pinned=False,
        interval_minutes=60,
        jitter_seconds=0,
        requests_per_minute=600.0,
        user_agent="furnest-test/1",
        request_timeout_seconds=7,
        run_timeout_seconds=900,
        search_locations=("Exampleville, XX",),
        api_key=SecretStr(TOKEN),
    )
    return replace(config, **overrides)


@pytest.fixture
def provider() -> FakeProvider:
    fake = FakeProvider()
    Example.provider = fake
    return fake


def test_runs_the_job_and_maps_its_items_with_the_input_it_sent(provider: FakeProvider) -> None:
    def clock() -> float:
        return 0.0

    adapter = Example(make_config(), clock=clock)

    assert adapter.search(Criteria(min_beds=2)) == []

    sent = {"places": ["Exampleville, XX"], "beds": 2}
    [call] = provider.calls
    assert call == {
        "job": "robot-123",
        "input": sent,
        "token": TOKEN,
        "timeout": 840,
        "respect_robots": False,
        "clock": clock,
    }
    assert adapter.seen == ([{"id": "a"}, {"id": "b"}], sent)


def test_the_provider_timeout_has_a_floor(provider: FakeProvider) -> None:
    Example(make_config(run_timeout_seconds=90)).search(Criteria())

    assert provider.calls[0]["timeout"] == 60


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("Maxun", 'set "api_key" \\(a Maxun API token\\) for example_source'),
        ("Apify", 'set "api_key" \\(an Apify API token\\) for example_source'),
    ],
)
def test_a_missing_token_names_the_provider(name: str, message: str) -> None:
    fake = FakeProvider(name)
    Example.provider = fake

    with pytest.raises(SourceError, match=message):
        Example(make_config(api_key=None)).search(Criteria())
    assert fake.calls == []


def test_missing_search_locations_is_an_error(provider: FakeProvider) -> None:
    with pytest.raises(SourceError, match='set "search_locations"'):
        Example(make_config(search_locations=())).search(Criteria())
    assert provider.calls == []


def test_apify_adapters_run_on_apify() -> None:
    assert ApifyAdapter.provider is APIFY
    assert APIFY.name == "Apify"


def test_today_defaults_to_the_utc_date(provider: FakeProvider) -> None:
    assert Example(make_config())._today() == datetime.now(UTC).date()
