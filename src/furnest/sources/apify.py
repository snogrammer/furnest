"""Running an Apify actor and reading its results, for adapters backed by a data provider.

Some sources can't be fetched directly (Furnished Finder, for one, puts every search
and property page behind an interactive Cloudflare check). For those, a third-party
"actor" on Apify (https://apify.com) does the fetching and returns JSON. An adapter
calls :func:`run_actor` with the actor's input and maps the items it gets back.

One call:

1. starts a run (``POST /v2/acts/<actor>/runs``) and returns at once, with
   Apify's own ``timeout`` set so the run can't outlive ours, and
   ``maxTotalChargeUsd`` so a pay-per-event actor can't bill more than the cap;
2. long-polls the run (``GET /v2/actor-runs/<id>?waitForFinish=60``), so a run of
   several minutes costs a handful of requests, each well inside the rate limit;
3. reads the default dataset once the run has ``SUCCEEDED``.

A run that ends ``FAILED``, ``TIMED-OUT`` or ``ABORTED`` raises :class:`SourceError`.
So does a run still going at our deadline, after a best-effort abort so it stops
billing. The token goes in the ``Authorization`` header, never the URL, so it can't
end up in a logged URL or an error message.

Requests go through the adapter's :class:`~furnest.sources.http.SourceHttpClient`
(rate limit, retries, block detection), built with ``respect_robots`` off:
robots.txt is for crawlers, and this is an authenticated API call.

:class:`ApifyProvider` is Apify as a :class:`~furnest.sources.provider.DataProvider`,
where a job is an actor. :class:`ApifyAdapter` is a
:class:`~furnest.sources.provider.ProviderAdapter` on Apify: a subclass sets
:attr:`~ApifyAdapter.job` to the actor and writes its input builder and item mapping.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, ClassVar
from urllib.parse import quote

import httpx2

from furnest.logging import get_logger
from furnest.sources.base import AdapterConfig, Credential
from furnest.sources.errors import SourceError
from furnest.sources.http import SourceHttpClient
from furnest.sources.provider import DataProvider, JsonObject, ProviderAdapter

log = get_logger(__name__)

APIFY_API = "https://api.apify.com/v2"
# Longest Apify lets one request wait for a run to finish.
MAX_WAIT_SECONDS = 60
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"})


def run_actor(
    http: SourceHttpClient,
    actor: str,
    run_input: Mapping[str, Any],
    *,
    token: str,
    timeout_seconds: int,
    clock: Callable[[], float] = time.monotonic,
    max_charge_usd: float | None = None,
) -> list[JsonObject]:
    """Run ``actor`` (``"user/name"``) on ``run_input``; returns its dataset items.

    ``max_charge_usd`` caps what a pay-per-event actor may bill for the run.
    """
    headers = {"Authorization": f"Bearer {token}"}
    deadline = clock() + timeout_seconds
    actor_path = quote(actor.replace("/", "~"), safe="~")
    params: dict[str, Any] = {"timeout": timeout_seconds, "waitForFinish": 0}
    if max_charge_usd is not None:
        params["maxTotalChargeUsd"] = f"{max_charge_usd:.2f}"

    run = _data(
        http.post(
            f"{APIFY_API}/acts/{actor_path}/runs",
            params=params,
            json=dict(run_input),
            headers=headers,
        ),
        "start run",
    )
    run_id = str(run["id"])
    log.info("apify.run_started", actor=actor, run_id=run_id)

    while run.get("status") not in _TERMINAL:
        remaining = deadline - clock()
        if remaining <= 0:
            _abort(http, run_id, headers)
            msg = (
                f"Apify run {run_id} of {actor} still {run.get('status')} after {timeout_seconds}s"
            )
            raise SourceError(msg, url=f"{APIFY_API}/actor-runs/{run_id}")
        wait = int(min(MAX_WAIT_SECONDS, max(1, remaining)))
        run = _data(
            http.get(
                f"{APIFY_API}/actor-runs/{run_id}",
                params={"waitForFinish": wait},
                headers=headers,
                # The server holds the request up to ``wait`` seconds before answering.
                timeout=httpx2.Timeout(wait + float(http.config.request_timeout_seconds)),
            ),
            "poll run",
        )

    status = run["status"]
    if status != "SUCCEEDED":
        message = run.get("statusMessage") or "no status message"
        msg = f"Apify run {run_id} of {actor} ended {status}: {message}"
        raise SourceError(msg, url=f"{APIFY_API}/actor-runs/{run_id}")

    dataset_url = f"{APIFY_API}/datasets/{run['defaultDatasetId']}/items"
    response = http.get(dataset_url, params={"clean": "true", "format": "json"}, headers=headers)
    items = _json(response, "read dataset")
    if not isinstance(items, list):
        msg = f"Apify dataset for run {run_id} is not a JSON list"
        raise SourceError(msg, url=dataset_url)
    log.info("apify.run_finished", actor=actor, run_id=run_id, items=len(items))
    return [item for item in items if isinstance(item, dict)]


def _abort(http: SourceHttpClient, run_id: str, headers: Mapping[str, str]) -> None:
    try:
        http.post(f"{APIFY_API}/actor-runs/{run_id}/abort", headers=dict(headers))
    except SourceError:
        log.warning("apify.abort_failed", run_id=run_id)


def _json(response: httpx2.Response, what: str) -> Any:  # noqa: ANN401 - arbitrary JSON
    try:
        return response.json()
    except ValueError as exc:
        msg = f"Apify returned invalid JSON ({what})"
        raise SourceError(msg, url=str(response.request.url)) from exc


def _data(response: httpx2.Response, what: str) -> JsonObject:
    body = _json(response, what)
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or "id" not in data:
        msg = f"Apify response has no run object ({what})"
        raise SourceError(msg, url=str(response.request.url))
    return data


# --- Apify as a data provider ---------------------------------------------------------


class ApifyProvider:
    """Apify as a :class:`~furnest.sources.provider.DataProvider`: a job is an actor."""

    name = "Apify"

    def run(
        self,
        http: SourceHttpClient,
        job: str,
        run_input: Mapping[str, Any],
        *,
        token: str,
        timeout_seconds: int,
        clock: Callable[[], float],
        max_charge_usd: float | None = None,
    ) -> list[JsonObject]:
        return run_actor(
            http,
            job,
            run_input,
            token=token,
            timeout_seconds=timeout_seconds,
            clock=clock,
            max_charge_usd=max_charge_usd,
        )


APIFY = ApifyProvider()
# One token for every Apify-backed source; entered once on the dashboard.
APIFY_CREDENTIAL = Credential(
    "apify", "Apify API token", "https://console.apify.com/account/integrations"
)


# The lowest ``maxTotalChargeUsd`` actors accept: their ``minimalMaxTotalChargeUsd``
# was $0.50 on the ones tried in #111.
MIN_MAX_CHARGE_USD = 0.50
# Average minutes in a month, for projecting a source's monthly spend.
MINUTES_PER_MONTH = 30 * 24 * 60


class ApifyAdapter(ProviderAdapter):
    """A source read through an Apify actor. Subclasses set :attr:`job` to the actor
    (``"user/name"``) and :attr:`site`, and implement ``build_input`` and
    ``parse_items`` (see :class:`~furnest.sources.provider.ProviderAdapter`). They all
    share :data:`APIFY_CREDENTIAL`.

    Every run is billed, so Apify sources default to a run every two days of 25
    results (#216), and each sets :attr:`start_cost_usd` and :attr:`result_cost_usd`
    from what its actor bills. Those give the estimate the monthly budget guard
    (:mod:`furnest.sources.budget`) and the Sources page's projection use, and the
    run's ``maxTotalChargeUsd`` cap.
    """

    provider: ClassVar[DataProvider] = APIFY
    credential: ClassVar[Credential | None] = APIFY_CREDENTIAL
    default_interval_minutes: ClassVar[int | None] = 2880
    default_max_results: ClassVar[int | None] = 25
    # What the actor bills to start a run and for each result, in USD.
    start_cost_usd: ClassVar[float] = 0.0
    result_cost_usd: ClassVar[float] = 0.0

    @classmethod
    def run_cost_usd(cls, config: AdapterConfig) -> float:
        """The estimated cost of one run with ``config``'s ``max_results``."""
        return cls.start_cost_usd + cls.result_cost_usd * config.max_results

    @classmethod
    def monthly_cost_usd(cls, config: AdapterConfig) -> float:
        """The estimated cost of a month of scheduled runs with ``config``."""
        return cls.run_cost_usd(config) * MINUTES_PER_MONTH / config.interval_minutes

    def max_charge(self) -> float:
        """``max_charge_usd`` from the config, else twice the estimated run cost, but
        never under :data:`MIN_MAX_CHARGE_USD`."""
        if self.config.max_charge_usd is not None:
            return self.config.max_charge_usd
        return max(MIN_MAX_CHARGE_USD, 2 * self.run_cost_usd(self.config))
