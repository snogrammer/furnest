"""Running an Apify actor and reading its results, for adapters backed by a data provider.

Some sources can't be fetched directly (Furnished Finder, for one, puts every search
and property page behind an interactive Cloudflare check). For those, a third-party
"actor" on Apify (https://apify.com) does the fetching and returns JSON. An adapter
calls :func:`run_actor` with the actor's input and maps the items it gets back.

One call:

1. starts a run (``POST /v2/acts/<actor>/runs``) and returns at once, with
   Apify's own ``timeout`` set so the run can't outlive ours;
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

:class:`ApifyAdapter` is the adapter half: it checks the config (an API token and
``search_locations``), runs :attr:`~ApifyAdapter.actor` on the input a subclass
builds, and hands the items back to the subclass to map. Each provider-backed
source is then just its input builder and its item mapping.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any, ClassVar
from urllib.parse import quote

import httpx2

from furnest.domain import Criteria, NormalizedListing
from furnest.logging import get_logger
from furnest.sources.base import AdapterConfig, Capabilities, SourceAdapter
from furnest.sources.errors import SourceError
from furnest.sources.http import SourceHttpClient

log = get_logger(__name__)

APIFY_API = "https://api.apify.com/v2"
# Longest Apify lets one request wait for a run to finish.
MAX_WAIT_SECONDS = 60
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"})
# The actor gets this much less time than the whole run, so Apify stops it (and we
# read why) before the worker's own run timeout fires.
ACTOR_TIMEOUT_MARGIN_SECONDS = 60
MIN_ACTOR_TIMEOUT_SECONDS = 60

JsonObject = dict[str, Any]


def run_actor(
    http: SourceHttpClient,
    actor: str,
    run_input: Mapping[str, Any],
    *,
    token: str,
    timeout_seconds: int,
    clock: Callable[[], float] = time.monotonic,
) -> list[JsonObject]:
    """Run ``actor`` (``"user/name"``) on ``run_input``; returns its dataset items."""
    headers = {"Authorization": f"Bearer {token}"}
    deadline = clock() + timeout_seconds
    actor_path = quote(actor.replace("/", "~"), safe="~")

    run = _data(
        http.post(
            f"{APIFY_API}/acts/{actor_path}/runs",
            params={"timeout": timeout_seconds, "waitForFinish": 0},
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


# --- The adapter base ---------------------------------------------------------------


def _today() -> date:
    return datetime.now(UTC).date()


class ApifyAdapter(SourceAdapter):
    """A source read through an Apify actor that searches by place name.

    Subclasses set :attr:`actor` (``"user/name"``) and :attr:`site` (the source's
    origin, used in error messages) and implement :meth:`build_input` and
    :meth:`parse_items`. The token comes from the adapter's ``api_key``, the places
    from its ``search_locations``.
    """

    actor: ClassVar[str]
    site: ClassVar[str]
    capabilities: ClassVar[Capabilities] = Capabilities(has_api=True, requires_api_key=True)

    def __init__(
        self,
        config: AdapterConfig,
        *,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = _today,
    ) -> None:
        super().__init__(config)
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._today = today

    @abstractmethod
    def build_input(self, criteria: Criteria) -> dict[str, Any]:
        """The actor's input for ``criteria`` and this adapter's config."""

    @abstractmethod
    def parse_items(
        self, items: list[JsonObject], run_input: Mapping[str, Any]
    ) -> list[NormalizedListing]:
        """The run's dataset items as listings. Raise :class:`SourceError` to fail the run.

        ``run_input`` is what :meth:`build_input` sent, for a mapping that depends on the
        search (the stay dates an Airbnb price covers, say).
        """

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        config = self.config
        if config.api_key is None:
            msg = f'set "api_key" (an Apify API token) for {self.name} in SOURCES_ADAPTERS'
            raise SourceError(msg, url=self.site)
        if not config.search_locations:
            msg = (
                f'set "search_locations" (e.g. ["Springfield, IL"]) for {self.name} '
                "in SOURCES_ADAPTERS"
            )
            raise SourceError(msg, url=self.site)

        run_input = self.build_input(criteria)
        timeout = max(
            MIN_ACTOR_TIMEOUT_SECONDS, config.run_timeout_seconds - ACTOR_TIMEOUT_MARGIN_SECONDS
        )
        http_config = replace(config, respect_robots=False)
        with SourceHttpClient(
            http_config, transport=self._transport, clock=self._clock, sleep=self._sleep
        ) as http:
            items = run_actor(
                http,
                self.actor,
                run_input,
                token=config.api_key.get_secret_value(),
                timeout_seconds=timeout,
                clock=self._clock,
            )
        return self.parse_items(items, run_input)
