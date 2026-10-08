"""run_actor: start an Apify run, long-poll it, read its dataset. Apify is faked with
``httpx2.MockTransport`` and the run objects in tests/fixtures/furnished_finder."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from furnest.sources import SourceBlockedError, SourceError, SourceHttpClient
from furnest.sources.apify import APIFY_API, run_actor
from furnest.sources.base import AdapterConfig

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "furnished_finder"
TOKEN = "apify-test-token"  # noqa: S105 - a fake
ACTOR = "someone/some-actor"
RUN_URL = f"{APIFY_API}/actor-runs/RUN0000000000001"
DATASET_URL = f"{APIFY_API}/datasets/DATASET000000001/items"


def fixture(name: str) -> Any:  # noqa: ANN401 - arbitrary JSON
    return json.loads((FIXTURES / name).read_text())


def run_status(status: str) -> httpx2.Response:
    return httpx2.Response(200, json=fixture(f"apify_run_{status}.json"))


class Apify:
    """Answers the start, poll, abort and dataset endpoints; records every request."""

    def __init__(
        self,
        *polls: httpx2.Response,
        items: Any = None,  # noqa: ANN401
        on_poll: Callable[[], None] | None = None,
    ) -> None:
        self.polls = list(polls)
        self.items = [{"id": "a"}, {"id": "b"}, "not an object"] if items is None else items
        self.on_poll = on_poll
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = str(request.url).split("?")[0]
        if request.method == "POST" and url == f"{APIFY_API}/acts/someone~some-actor/runs":
            return httpx2.Response(201, json=fixture("apify_run_ready.json"))
        if request.method == "GET" and url == RUN_URL:
            if self.on_poll is not None:
                self.on_poll()
            return self.polls.pop(0)
        if request.method == "POST" and url == f"{RUN_URL}/abort":
            return run_status("failed")
        if request.method == "GET" and url == DATASET_URL:
            return httpx2.Response(200, json=self.items)
        return httpx2.Response(404, json={"error": {"type": "record-not-found"}})


def make_config() -> AdapterConfig:
    return AdapterConfig(
        name="example_source",
        enabled=True,
        enabled_pinned=False,
        interval_minutes=60,
        jitter_seconds=0,
        requests_per_minute=600.0,
        user_agent="furnest-test/1",
        request_timeout_seconds=7,
        respect_robots=False,
        max_retries=0,
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def call(
    apify: Callable[[httpx2.Request], httpx2.Response],
    *,
    clock: Callable[[], float] | None = None,
    timeout: int = 600,
) -> list[Any]:
    transport = httpx2.MockTransport(apify)
    with SourceHttpClient(make_config(), transport=transport, sleep=lambda _: None) as http:
        return run_actor(
            http,
            ACTOR,
            {"locations": ["Exampleville, XX"]},
            token=TOKEN,
            timeout_seconds=timeout,
            clock=clock or Clock(),
        )


def test_runs_the_actor_and_returns_its_dataset_items() -> None:
    apify = Apify(run_status("running"), run_status("succeeded"))

    items = call(apify)

    assert items == [{"id": "a"}, {"id": "b"}]
    start, poll, _, dataset = apify.requests
    assert json.loads(start.content) == {"locations": ["Exampleville, XX"]}
    assert dict(start.url.params) == {"timeout": "600", "waitForFinish": "0"}
    assert dict(poll.url.params) == {"waitForFinish": "60"}
    assert dict(dataset.url.params) == {"clean": "true", "format": "json"}
    # The server holds a poll up to 60s, so its read timeout covers that plus the usual 7s.
    assert poll.extensions["timeout"]["read"] == 67
    assert start.extensions["timeout"]["read"] == 7


def test_the_token_goes_in_the_header_never_the_url() -> None:
    apify = Apify(run_status("succeeded"))

    call(apify)

    for request in apify.requests:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)


def test_a_poll_never_waits_past_the_deadline() -> None:
    clock = Clock()

    def near_deadline() -> None:
        clock.now = 590.0

    apify = Apify(run_status("running"), run_status("succeeded"), on_poll=near_deadline)

    call(apify, clock=clock, timeout=600)

    first_poll, second_poll = apify.requests[1:3]
    assert dict(first_poll.url.params) == {"waitForFinish": "60"}
    assert dict(second_poll.url.params) == {"waitForFinish": "10"}


def test_a_failed_run_raises_with_its_status_message() -> None:
    with pytest.raises(SourceError, match="ended FAILED: The actor crashed"):
        call(Apify(run_status("running"), run_status("failed")))


def test_a_run_still_going_at_the_deadline_is_aborted() -> None:
    clock = Clock()

    def tick() -> None:
        clock.now += 400.0

    apify = Apify(run_status("running"), run_status("running"), on_poll=tick)

    with pytest.raises(SourceError, match="still RUNNING after 600s"):
        call(apify, clock=clock, timeout=600)

    assert apify.requests[-1].method == "POST"
    assert str(apify.requests[-1].url) == f"{RUN_URL}/abort"


def test_a_failed_abort_still_reports_the_timeout() -> None:
    clock = Clock()

    def tick() -> None:
        clock.now += 1000.0

    class AbortFails(Apify):
        def __call__(self, request: httpx2.Request) -> httpx2.Response:
            if str(request.url).endswith("/abort"):
                self.requests.append(request)
                return httpx2.Response(500)
            return super().__call__(request)

    with pytest.raises(SourceError, match="still RUNNING"):
        call(AbortFails(run_status("running"), on_poll=tick), clock=clock)


def test_a_bad_token_is_an_error() -> None:
    def refuse(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"error": {"type": "token-not-valid"}})

    with pytest.raises(SourceError, match="HTTP 401"):
        call(refuse)


def test_a_forbidden_actor_is_reported_as_blocked() -> None:
    def forbid(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403, json={"error": {"type": "insufficient-permissions"}})

    with pytest.raises(SourceBlockedError):
        call(forbid)


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (httpx2.Response(201, text="<html>oops</html>"), "invalid JSON"),
        (httpx2.Response(201, json={"data": {"status": "READY"}}), "no run object"),
        (httpx2.Response(201, json=["not", "an", "object"]), "no run object"),
    ],
)
def test_malformed_run_responses_are_errors(reply: httpx2.Response, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        call(lambda _request: reply)


def test_a_dataset_that_is_not_a_list_is_an_error() -> None:
    with pytest.raises(SourceError, match="not a JSON list"):
        call(Apify(run_status("succeeded"), items={"items": []}))
