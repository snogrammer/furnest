"""The Apify monthly budget: stop starting billed runs before the credit runs out.

Every Apify-backed source bills per run, and the Free plan's $5 a cycle went in a day
when they all ran hourly (#216). Before each Apify run, :class:`ApifyBudget` reads
how much the account has used this cycle (``GET /v2/users/me/limits``, which also
gives the plan's own limit and when the cycle ends) and declines the run when that
plus the run's estimated cost would reach the plan's limit ($19 on Starter), or
``SOURCES_APIFY_MONTHLY_BUDGET_USD`` if that's set and lower. Direct-HTTP sources
never come here.

The reading is cached for :data:`USAGE_TTL` in the ``settings`` table under
:data:`USAGE_KEY`, so the worker and the web process share one reading and a page
load doesn't call Apify each time. When a run starts, :meth:`ApifyBudget.reserve`
adds its estimate to the cached reading, so "Run all now" can't start five runs
against the same stale figure.

If the usage can't be read (Apify is down, the token is wrong), the run goes ahead:
its ``maxTotalChargeUsd`` cap still bounds what it can cost, and a bad token fails
the run anyway.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2

from furnest.config import SettingsStore, SourcesSettings
from furnest.logging import get_logger
from furnest.sources.apify import APIFY_API, ApifyAdapter
from furnest.sources.base import AdapterConfig
from furnest.sources.errors import SourceError
from furnest.sources.http import SourceHttpClient

log = get_logger(__name__)

USAGE_KEY = "sources.apify.usage"
USAGE_TTL = timedelta(minutes=10)
LIMITS_URL = f"{APIFY_API}/users/me/limits"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ApifyUsage:
    """The account's spend this usage cycle, as Apify reported it at ``fetched_at``
    (plus what :meth:`ApifyBudget.reserve` has added since)."""

    used_usd: float
    # The plan's own monthly cap, e.g. $5 on the Free plan.
    plan_limit_usd: float
    cycle_end: datetime
    fetched_at: datetime

    @property
    def resumes_on(self) -> datetime:
        """When the next cycle starts: Apify's ``endAt`` is the cycle's last instant
        (``23:59:59.999``), so the next whole second."""
        return self.cycle_end.replace(microsecond=0) + timedelta(seconds=1)

    def to_json(self) -> dict[str, Any]:
        return {
            "used_usd": self.used_usd,
            "plan_limit_usd": self.plan_limit_usd,
            "cycle_end": self.cycle_end.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
        }

    @classmethod
    def from_json(cls, value: object) -> ApifyUsage | None:
        """The usage stored by :meth:`to_json`, or ``None`` if ``value`` isn't one."""
        if not isinstance(value, dict):
            return None
        try:
            return cls(
                used_usd=float(value["used_usd"]),
                plan_limit_usd=float(value["plan_limit_usd"]),
                cycle_end=datetime.fromisoformat(value["cycle_end"]),
                fetched_at=datetime.fromisoformat(value["fetched_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def spend_limit(budget_usd: float | None, plan_limit_usd: float) -> float:
    """The plan's limit, lowered to ``budget_usd`` when that's set."""
    if budget_usd is None:
        return plan_limit_usd
    return min(budget_usd, plan_limit_usd)


class ApifyBudget:
    """Whether an Apify source may start a run, given the account's spend this cycle."""

    def __init__(
        self,
        settings: SourcesSettings,
        store: SettingsStore,
        *,
        transport: httpx2.BaseTransport | None = None,
        now: Callable[[], datetime] = _now,
    ) -> None:
        self._settings = settings
        self._store = store
        self._transport = transport
        self._now = now

    @property
    def budget_usd(self) -> float | None:
        """``SOURCES_APIFY_MONTHLY_BUDGET_USD``, or ``None`` to spend up to the plan's
        limit."""
        return self._settings.apify_monthly_budget_usd

    def limit_usd(self, usage: ApifyUsage) -> float:
        """What runs may spend this cycle: the plan's limit, or the budget if it's set
        and lower."""
        return spend_limit(self.budget_usd, usage.plan_limit_usd)

    def cached(self) -> ApifyUsage | None:
        """The last reading, however old, without calling Apify."""
        return ApifyUsage.from_json(self._store.get(USAGE_KEY))

    def usage(self, token: str) -> ApifyUsage | None:
        """This cycle's usage: the cached reading if it's fresh, else a new one from
        Apify. ``None`` when Apify can't be read and nothing fresh is cached."""
        cached = self.cached()
        now = self._now()
        if cached is not None and now - cached.fetched_at < USAGE_TTL:
            return cached
        try:
            usage = self._fetch(token, now)
        except SourceError as exc:
            log.warning("apify.usage_unavailable", error=str(exc))
            return None
        self._store.set(USAGE_KEY, usage.to_json())
        return usage

    def blocked_reason(self, adapter: type[ApifyAdapter], config: AdapterConfig) -> str | None:
        """Why ``adapter`` mustn't start a run now, or ``None`` if it may (or the usage
        can't be read)."""
        if config.api_key is None:
            return None
        usage = self.usage(config.api_key.get_secret_value())
        if usage is None:
            return None
        limit = self.limit_usd(usage)
        if usage.used_usd + adapter.run_cost_usd(config) < limit:
            return None
        return (
            f"Apify monthly budget reached (${usage.used_usd:.2f} of ${limit:.2f} used); "
            f"resumes {usage.resumes_on:%b %-d}"
        )

    def reserve(self, cost_usd: float) -> None:
        """Count a run that is starting against the cached reading, until the next one."""
        cached = self.cached()
        if cached is None:
            return
        reserved = ApifyUsage(
            used_usd=cached.used_usd + cost_usd,
            plan_limit_usd=cached.plan_limit_usd,
            cycle_end=cached.cycle_end,
            fetched_at=cached.fetched_at,
        )
        self._store.set(USAGE_KEY, reserved.to_json())

    def _fetch(self, token: str, now: datetime) -> ApifyUsage:
        settings = self._settings
        config = AdapterConfig(
            name="apify_usage",
            enabled=True,
            interval_minutes=settings.default_interval_minutes,
            jitter_seconds=0,
            requests_per_minute=settings.requests_per_minute,
            user_agent=settings.user_agent,
            request_timeout_seconds=settings.request_timeout_seconds,
            # robots.txt is for crawlers; this is an authenticated API call.
            respect_robots=False,
            max_retries=1,
        )
        with SourceHttpClient(config, transport=self._transport) as http:
            response = http.get(LIMITS_URL, headers={"Authorization": f"Bearer {token}"})
        try:
            data = response.json()["data"]
            return ApifyUsage(
                used_usd=float(data["current"]["monthlyUsageUsd"]),
                plan_limit_usd=float(data["limits"]["maxMonthlyUsageUsd"]),
                cycle_end=datetime.fromisoformat(data["monthlyUsageCycle"]["endAt"]),
                fetched_at=now,
            )
        except (KeyError, TypeError, ValueError) as exc:
            msg = "Apify usage response is missing the monthly usage or limit"
            raise SourceError(msg, url=LIMITS_URL) from exc
