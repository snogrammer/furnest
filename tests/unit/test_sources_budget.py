"""The Apify spend guard (#216): run cost estimates, the ``maxTotalChargeUsd`` cap, and
the monthly budget check against ``/users/me/limits``, with Apify faked by
``httpx2.MockTransport``."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx2
import pytest
from pydantic import SecretStr

from furnest.config import AdapterOverrides, InMemorySettingsStore, SourcesSettings
from furnest.domain import Criteria, NormalizedListing
from furnest.sources import discover, resolve_config
from furnest.sources.apify import ApifyAdapter
from furnest.sources.base import AdapterConfig
from furnest.sources.budget import LIMITS_URL, USAGE_KEY, USAGE_TTL, ApifyBudget, ApifyUsage

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 11, 20, 0, tzinfo=UTC)
TOKEN = "apify-test-token"  # noqa: S105 - a fake
STARTER_PLAN_USD = 19.0


class Billed(ApifyAdapter):
    name: ClassVar[str] = "billed"
    display_name: ClassVar[str] = "Billed"
    job: ClassVar[str] = "someone/some-actor"
    site: ClassVar[str] = "https://example.com"
    start_cost_usd: ClassVar[float] = 0.05
    result_cost_usd: ClassVar[float] = 0.01

    def build_input(self, criteria: Criteria) -> dict[str, Any]:
        return {}

    def parse_items(
        self, items: list[dict[str, Any]], run_input: Mapping[str, Any]
    ) -> list[NormalizedListing]:
        return []


def config(**overrides: Any) -> AdapterConfig:  # noqa: ANN401 - test helper
    settings = SourcesSettings(
        adapters={"billed": AdapterOverrides(api_key=SecretStr(TOKEN), **overrides)}
    )
    return resolve_config(Billed, settings)


class Limits:
    """Answers ``GET /users/me/limits`` with ``used`` of ``plan`` USD; counts calls."""

    def __init__(self, used: float, plan: float = 5.0, status: int = 200) -> None:
        self.used, self.plan, self.status = used, plan, status
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if str(request.url) != LIMITS_URL or self.status != 200:
            return httpx2.Response(self.status if self.status != 200 else 404)
        return httpx2.Response(
            200,
            json={
                "data": {
                    "monthlyUsageCycle": {
                        "startAt": "2026-10-07T00:00:00.000Z",
                        "endAt": "2026-11-06T23:59:59.999Z",
                    },
                    "limits": {"maxMonthlyUsageUsd": self.plan},
                    "current": {"monthlyUsageUsd": self.used},
                }
            },
        )


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def budget(
    limits: Limits,
    *,
    store: InMemorySettingsStore | None = None,
    clock: Clock | None = None,
    monthly: float | None = 5.0,
) -> ApifyBudget:
    return ApifyBudget(
        SourcesSettings(apify_monthly_budget_usd=monthly, max_retries=0),
        store if store is not None else InMemorySettingsStore(),
        transport=httpx2.MockTransport(limits),
        now=clock or Clock(),
    )


# --- Cost model ------------------------------------------------------------------------


def test_apify_sources_default_to_a_daily_run_of_25_results() -> None:
    resolved = config()

    assert (resolved.interval_minutes, resolved.max_results) == (1440, 25)
    assert Billed.run_cost_usd(resolved) == pytest.approx(0.30)
    # 30 runs in a 30-day month.
    assert Billed.monthly_cost_usd(resolved) == pytest.approx(9.00)


def test_the_run_cap_is_twice_the_estimate_but_at_least_the_actor_minimum() -> None:
    assert Billed(config()).max_charge() == pytest.approx(0.60)
    assert Billed(config(max_results=5)).max_charge() == pytest.approx(0.50)
    assert Billed(config(max_charge_usd=2.5)).max_charge() == pytest.approx(2.5)


def test_with_defaults_a_month_of_every_working_apify_source_fits_the_starter_plan() -> None:
    settings = SourcesSettings()
    # No budget set: runs may spend up to the plan's limit, $19 on Starter.
    assert settings.apify_monthly_budget_usd is None
    apify = [
        adapter
        for name in discover().names()
        if (adapter := discover().get(name)) is not None and issubclass(adapter, ApifyAdapter)
    ]
    assert {a.name for a in apify} >= {"airbnb", "zillow", "zumper", "apartments_com"}

    working = [a for a in apify if a.broken_reason is None]
    projected = sum(a.monthly_cost_usd(resolve_config(a, settings)) for a in working)

    assert projected <= STARTER_PLAN_USD


# --- Usage and the budget check ------------------------------------------------------------


def test_a_run_under_the_budget_may_start() -> None:
    limits = Limits(used=1.0)

    assert budget(limits).blocked_reason(Billed, config()) is None
    [request] = limits.requests
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_a_run_that_would_reach_the_budget_is_declined_with_when_it_resumes() -> None:
    # 4.75 used + a 0.30 run reaches 5.
    reason = budget(Limits(used=4.75)).blocked_reason(Billed, config())

    assert reason == "Apify monthly budget reached ($4.75 of $5.00 used); resumes Nov 7"


def test_the_plan_limit_wins_over_a_larger_budget() -> None:
    reason = budget(Limits(used=4.8, plan=5.0), monthly=20).blocked_reason(Billed, config())

    assert reason is not None
    assert "of $5.00" in reason


def test_without_a_budget_runs_may_spend_up_to_the_plan_limit() -> None:
    guard = budget(Limits(used=18.0, plan=STARTER_PLAN_USD), monthly=None)

    assert guard.blocked_reason(Billed, config()) is None
    guard.reserve(0.75)
    reason = guard.blocked_reason(Billed, config())
    assert reason == "Apify monthly budget reached ($18.75 of $19.00 used); resumes Nov 7"


def test_a_smaller_budget_wins_over_the_plan_limit() -> None:
    reason = budget(Limits(used=2.0, plan=5.0), monthly=2).blocked_reason(Billed, config())

    assert reason is not None
    assert "of $2.00" in reason


def test_usage_is_cached_for_a_while_then_read_again() -> None:
    limits, clock, store = Limits(used=1.0), Clock(), InMemorySettingsStore()
    guard = budget(limits, store=store, clock=clock)

    guard.usage(TOKEN)
    clock.now += USAGE_TTL - timedelta(seconds=1)
    guard.usage(TOKEN)
    assert len(limits.requests) == 1

    clock.now += timedelta(seconds=1)
    limits.used = 2.0
    usage = guard.usage(TOKEN)
    assert len(limits.requests) == 2
    assert usage is not None
    assert usage.used_usd == 2.0
    assert ApifyUsage.from_json(store.get(USAGE_KEY)) == usage


def test_a_starting_run_is_counted_against_the_cached_usage() -> None:
    limits = Limits(used=4.0)
    guard = budget(limits)
    resolved = config()

    assert guard.blocked_reason(Billed, resolved) is None
    guard.reserve(Billed.run_cost_usd(resolved))
    guard.reserve(Billed.run_cost_usd(resolved))
    guard.reserve(Billed.run_cost_usd(resolved))

    # 4.0 + 3 x 0.30 reserved + 0.30 for this run reaches 5, without asking Apify again.
    assert guard.blocked_reason(Billed, resolved) is not None
    assert len(limits.requests) == 1


def test_runs_go_ahead_when_the_usage_cannot_be_read() -> None:
    limits = Limits(used=0.0, status=500)

    assert budget(limits).blocked_reason(Billed, config()) is None
    assert limits.requests


def test_a_source_without_a_token_is_not_checked() -> None:
    limits = Limits(used=10.0)
    resolved = resolve_config(Billed, SourcesSettings())

    assert budget(limits).blocked_reason(Billed, resolved) is None
    assert limits.requests == []
