"""SourceHttpClient: robots, rate limit, retry/backoff and block detection.

Sources are faked in-process with ``httpx2.MockTransport``. Time is a fake clock
whose sleep advances it, and jitter comes from a fixed ``rng``, so every delay is exact.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx2
import pytest

from furnest.sources import SourceBlockedError, SourceError, SourceHttpClient
from furnest.sources.base import AdapterConfig
from furnest.sources.http import client_options

pytestmark = pytest.mark.unit

URL = "https://example.com/search?city=x"
ROBOTS_URL = "https://example.com/robots.txt"

Reply = httpx2.Response | Exception


def make_config(**changes: Any) -> AdapterConfig:  # noqa: ANN401
    values: dict[str, Any] = {
        "name": "example_source",
        "enabled": True,
        "enabled_pinned": False,
        "interval_minutes": 60,
        "jitter_seconds": 0,
        "requests_per_minute": 60.0,
        "user_agent": "furnest-test/1",
        "request_timeout_seconds": 7,
        "respect_robots": False,
        "max_retries": 3,
    }
    values.update(changes)
    return AdapterConfig(**values)


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Source:
    """A MockTransport handler: robots.txt from ``robots``, other URLs from ``replies``."""

    def __init__(self, *replies: Reply, robots: Reply | None = None) -> None:
        self.replies = list(replies)
        self.robots = robots
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        reply = self.robots if str(request.url) == ROBOTS_URL else self.replies.pop(0)
        if reply is None:
            msg = "unexpected robots.txt request"
            raise AssertionError(msg)
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


@pytest.fixture
def t() -> FakeTime:
    return FakeTime()


@pytest.fixture
def client(t: FakeTime) -> Callable[..., SourceHttpClient]:
    def build(source: Source, **kwargs: Any) -> SourceHttpClient:  # noqa: ANN401
        markers = kwargs.pop("block_markers", ())
        return SourceHttpClient(
            make_config(**kwargs),
            transport=httpx2.MockTransport(source),
            clock=t.clock,
            sleep=t.sleep,
            rng=lambda: 0.5,
            block_markers=markers,
        )

    return build


def html(body: str, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


# --- Client options ---------------------------------------------------------------


def test_client_options_come_from_config() -> None:
    options = client_options(make_config(proxy_url="http://proxy.example.com:8080/"))

    assert options["headers"] == {"User-Agent": "furnest-test/1"}
    assert options["timeout"] == httpx2.Timeout(7.0)
    assert options["proxy"] == "http://proxy.example.com:8080/"
    assert options["follow_redirects"] is True
    assert client_options(make_config())["proxy"] is None


def test_requests_carry_the_user_agent(client: Callable[..., SourceHttpClient]) -> None:
    source = Source(httpx2.Response(200, json={"ok": True}))
    with client(source) as http:
        response = http.get(URL)

    assert response.json() == {"ok": True}
    assert source.requests[0].headers["user-agent"] == "furnest-test/1"


def test_post_sends_the_body(client: Callable[..., SourceHttpClient]) -> None:
    source = Source(httpx2.Response(200))
    with client(source) as http:
        http.post(URL, json={"q": "x"})

    assert source.requests[0].method == "POST"
    assert source.requests[0].content == b'{"q":"x"}'


# --- Retries ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(httpx2.Response(429), id="429"),
        pytest.param(httpx2.Response(503), id="503"),
        pytest.param(httpx2.ReadTimeout("slow"), id="timeout"),
        pytest.param(httpx2.ConnectError("refused"), id="connect-error"),
    ],
)
def test_retryable_failures_back_off_then_succeed(
    client: Callable[..., SourceHttpClient], t: FakeTime, failure: Reply
) -> None:
    source = Source(failure, failure, httpx2.Response(200))
    with client(source) as http:
        assert http.get(URL).status_code == 200

    assert len(source.requests) == 3
    # rng=0.5: backoff 0.5s (half of 1s), then the 60 rpm limiter waits out the rest of
    # its second, then backoff 1.0s (half of 2s), which already refills the bucket.
    assert t.slept == pytest.approx([0.5, 0.5, 1.0])


def test_backoff_is_capped(client: Callable[..., SourceHttpClient], t: FakeTime) -> None:
    failures = [httpx2.Response(500) for _ in range(9)]
    source = Source(*failures, httpx2.Response(200))
    with client(source, max_retries=9, requests_per_minute=600) as http:
        http.get(URL)

    assert max(t.slept) == 30.0  # 0.5 * min(60, 2**8)


def test_retries_exhausted_raise_source_error(client: Callable[..., SourceHttpClient]) -> None:
    source = Source(*(httpx2.Response(502) for _ in range(3)))
    with client(source, max_retries=2) as http, pytest.raises(SourceError) as info:
        http.get(URL)

    assert info.value.status == 502
    assert info.value.url == URL
    assert "after 3 attempts" in str(info.value)
    assert not isinstance(info.value, SourceBlockedError)


def test_transport_errors_exhausted_chain_the_cause(
    client: Callable[..., SourceHttpClient],
) -> None:
    source = Source(httpx2.ReadTimeout("slow"))
    with client(source, max_retries=0) as http, pytest.raises(SourceError) as info:
        http.get(URL)

    assert info.value.status is None
    assert isinstance(info.value.__cause__, httpx2.ReadTimeout)


def test_retry_after_seconds_is_honored(
    client: Callable[..., SourceHttpClient], t: FakeTime
) -> None:
    source = Source(httpx2.Response(429, headers={"Retry-After": "42"}), httpx2.Response(200))
    with client(source) as http:
        http.get(URL)

    assert 42.0 in t.slept
    assert 0.5 not in t.slept


def test_retry_after_http_date_is_honored(
    client: Callable[..., SourceHttpClient], t: FakeTime
) -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    source = Source(httpx2.Response(503, headers={"Retry-After": when}), httpx2.Response(200))
    with client(source) as http:
        http.get(URL)

    assert any(100 < s <= 120 for s in t.slept)


@pytest.mark.parametrize("value", ["soon", "Wed, 99 Foo 2026 00:00:00 GMT"])
def test_unparseable_retry_after_falls_back_to_backoff(
    client: Callable[..., SourceHttpClient], t: FakeTime, value: str
) -> None:
    source = Source(httpx2.Response(429, headers={"Retry-After": value}), httpx2.Response(200))
    with client(source) as http:
        http.get(URL)

    assert 0.5 in t.slept


def test_naive_and_past_http_dates_mean_retry_now(
    client: Callable[..., SourceHttpClient], t: FakeTime
) -> None:
    past = "Mon, 01 Jan 2024 00:00:00 -0000"  # -0000 parses as a naive datetime
    source = Source(httpx2.Response(503, headers={"Retry-After": past}), httpx2.Response(200))
    with client(source) as http:
        http.get(URL)

    assert 0.0 in t.slept


def test_long_retry_after_gives_up(client: Callable[..., SourceHttpClient], t: FakeTime) -> None:
    source = Source(httpx2.Response(429, headers={"Retry-After": "3600"}))
    with client(source) as http, pytest.raises(SourceError, match="Retry-After 3600s") as info:
        http.get(URL)

    assert info.value.status == 429
    assert len(source.requests) == 1
    assert 3600 not in t.slept


def test_client_errors_are_not_retried(client: Callable[..., SourceHttpClient]) -> None:
    source = Source(httpx2.Response(404))
    with client(source) as http, pytest.raises(SourceError, match="HTTP 404") as info:
        http.get(URL)

    assert info.value.status == 404
    assert len(source.requests) == 1


def test_every_attempt_is_rate_limited(
    client: Callable[..., SourceHttpClient], t: FakeTime
) -> None:
    source = Source(httpx2.Response(200), httpx2.Response(500), httpx2.Response(200))
    with client(source, requests_per_minute=6) as http:  # one request per 10s
        http.get(URL)
        http.get(URL)

    # Second request waits 10s; its retry waits for the 0.5s backoff and then the
    # remaining 9.5s for the limiter.
    assert t.slept == pytest.approx([10.0, 0.5, 9.5])


# --- Block detection --------------------------------------------------------------


def test_403_is_a_block(client: Callable[..., SourceHttpClient]) -> None:
    source = Source(httpx2.Response(403))
    with client(source) as http, pytest.raises(SourceBlockedError) as info:
        http.get(URL)

    assert info.value.reason == "HTTP 403"
    assert info.value.status == 403
    assert len(source.requests) == 1


@pytest.mark.parametrize(
    "marker", ["g-recaptcha", "H-CAPTCHA", "cf-challenge", "<title>Attention Required! |"]
)
def test_captcha_pages_are_blocks(client: Callable[..., SourceHttpClient], marker: str) -> None:
    source = Source(html(f"<html><div class='x'>{marker}</div></html>"))
    with client(source) as http, pytest.raises(SourceBlockedError, match="captcha marker"):
        http.get(URL)


def test_adapter_markers_are_added(client: Callable[..., SourceHttpClient]) -> None:
    source = Source(html("<p>Please verify you are human</p>"))
    with (
        client(source, block_markers=["Verify you are HUMAN"]) as http,
        pytest.raises(SourceBlockedError) as info,
    ):
        http.get(URL)

    assert info.value.reason == "captcha marker 'verify you are human'"


def test_markers_outside_html_are_ignored(client: Callable[..., SourceHttpClient]) -> None:
    source = Source(
        httpx2.Response(200, json={"description": "has a g-recaptcha in the text"}),
        html("<p>" + "x" * 70_000 + "g-recaptcha</p>"),  # beyond the scanned prefix
    )
    with client(source) as http:
        http.get(URL)
        http.get(URL)


def test_markers_on_error_pages_are_plain_errors(
    client: Callable[..., SourceHttpClient],
) -> None:
    source = Source(html("g-recaptcha", status=404))
    with client(source) as http, pytest.raises(SourceError) as info:
        http.get(URL)

    assert not isinstance(info.value, SourceBlockedError)


# --- robots.txt -------------------------------------------------------------------

ROBOTS = "User-agent: *\nDisallow: /private/\n"


def test_robots_disallow_blocks_before_sending(client: Callable[..., SourceHttpClient]) -> None:
    source = Source(robots=httpx2.Response(200, text=ROBOTS))
    with client(source, respect_robots=True) as http, pytest.raises(SourceBlockedError) as info:
        http.get("https://example.com/private/1")

    assert info.value.reason == "robots.txt"
    assert source.urls == [ROBOTS_URL]


def test_robots_allow_is_cached(client: Callable[..., SourceHttpClient]) -> None:
    source = Source(
        httpx2.Response(200), httpx2.Response(200), robots=httpx2.Response(200, text=ROBOTS)
    )
    with client(source, respect_robots=True) as http:
        http.get(URL)
        http.get(URL)

    assert source.urls == [ROBOTS_URL, URL, URL]


def test_missing_robots_allows(client: Callable[..., SourceHttpClient]) -> None:
    source = Source(httpx2.Response(200), robots=httpx2.Response(404))
    with client(source, respect_robots=True) as http:
        http.get("https://example.com/private/1")


def test_unreachable_robots_raises_source_error(
    client: Callable[..., SourceHttpClient],
) -> None:
    source = Source(robots=httpx2.Response(503))
    with (
        client(source, respect_robots=True, max_retries=1) as http,
        pytest.raises(SourceError, match=r"robots\.txt unavailable"),
    ):
        http.get(URL)

    assert source.urls == [ROBOTS_URL, ROBOTS_URL]


def test_robots_transport_failure_raises_source_error(
    client: Callable[..., SourceHttpClient],
) -> None:
    source = Source(robots=httpx2.ConnectError("refused"))
    with (
        client(source, respect_robots=True, max_retries=0) as http,
        pytest.raises(SourceError) as info,
    ):
        http.get(URL)

    assert info.value.url == ROBOTS_URL


def test_robots_ignored_when_disabled(client: Callable[..., SourceHttpClient]) -> None:
    source = Source(httpx2.Response(200))
    with client(source, respect_robots=False) as http:
        http.get("https://example.com/private/1")

    assert source.urls == ["https://example.com/private/1"]
