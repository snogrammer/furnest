"""ImageFetcher: limits, content-type checks, robots.txt and per-host rate limiting.

Image hosts are faked with ``httpx2.MockTransport``; time is a fake clock.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest

from furnest.config import ImageSettings, SourcesSettings
from furnest.dedup.phash import ImageFetcher, ImageFetchError, compute_phash

IMAGES = Path(__file__).parents[1] / "fixtures" / "images"
ROOM = (IMAGES / "room.jpg").read_bytes()
URL = "https://img.example.com/a/room.jpg"

Handler = Callable[[httpx2.Request], httpx2.Response]


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Host:
    """Serves robots.txt (404 unless given) and one reply for every other URL."""

    def __init__(self, reply: httpx2.Response | Exception, robots: str | None = None) -> None:
        self.reply = reply
        self.robots = robots
        self.urls: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.urls.append(str(request.url))
        if request.url.path == "/robots.txt":
            if self.robots is None:
                return httpx2.Response(404)
            return httpx2.Response(200, text=self.robots)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def jpeg(body: bytes = ROOM, **headers: str) -> httpx2.Response:
    return httpx2.Response(200, content=body, headers={"content-type": "image/jpeg", **headers})


@pytest.fixture
def t() -> FakeTime:
    return FakeTime()


@pytest.fixture
def fetcher(t: FakeTime) -> Callable[..., ImageFetcher]:
    def build(host: Handler, *, respect_robots: bool = True, **images: Any) -> ImageFetcher:  # noqa: ANN401
        return ImageFetcher(
            ImageSettings(**images),
            SourcesSettings(respect_robots=respect_robots),
            transport=httpx2.MockTransport(host),
            clock=t.clock,
            sleep=t.sleep,
        )

    return build


def test_fetches_the_bytes_and_hashes_them(fetcher: Callable[..., ImageFetcher]) -> None:
    host = Host(jpeg())

    with fetcher(host) as images:
        assert images.fetch(URL) == ROOM
        assert images.fetch_phash(URL) == compute_phash(ROOM)


def test_sends_the_sources_user_agent(fetcher: Callable[..., ImageFetcher]) -> None:
    seen: list[str] = []

    def host(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["user-agent"])
        return jpeg() if request.url.path != "/robots.txt" else httpx2.Response(404)

    fetcher(host).fetch(URL)

    assert set(seen) == {SourcesSettings().user_agent}


@pytest.mark.parametrize(
    ("content_type", "reason"),
    [("text/html; charset=utf-8", "not an image (text/html)"), ("", "not an image (no content")],
)
def test_rejects_a_response_that_is_not_an_image(
    fetcher: Callable[..., ImageFetcher], content_type: str, reason: str
) -> None:
    host = Host(httpx2.Response(200, content=b"<html>", headers={"content-type": content_type}))

    with pytest.raises(ImageFetchError) as caught:
        fetcher(host).fetch(URL)

    assert caught.value.reason.startswith(reason)
    assert caught.value.url == URL


def test_rejects_a_declared_size_over_the_limit(fetcher: Callable[..., ImageFetcher]) -> None:
    # The body is small; only the header says it's too big.
    host = Host(
        httpx2.Response(
            200,
            content=iter([b"x"]),
            headers={"content-type": "image/jpeg", "content-length": "6000"},
        )
    )

    with pytest.raises(ImageFetchError, match="too large"):
        fetcher(host, max_bytes=5000).fetch(URL)


def test_stops_a_stream_without_length_once_past_the_limit(
    fetcher: Callable[..., ImageFetcher],
) -> None:
    sent: list[int] = []

    def chunks() -> Iterator[bytes]:
        for _ in range(100):
            sent.append(1)
            yield b"x" * 1000

    host = Host(httpx2.Response(200, content=chunks(), headers={"content-type": "image/jpeg"}))

    with pytest.raises(ImageFetchError, match="too large"):
        fetcher(host, max_bytes=5000).fetch(URL)

    assert len(sent) < 100


def test_an_http_error_is_reported_by_status(fetcher: Callable[..., ImageFetcher]) -> None:
    with pytest.raises(ImageFetchError, match="HTTP 404"):
        fetcher(Host(httpx2.Response(404))).fetch(URL)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx2.ReadTimeout("slow"), "timeout"),
        (httpx2.ConnectError("refused"), "transport error: ConnectError"),
    ],
)
def test_transport_failures_are_reported(
    fetcher: Callable[..., ImageFetcher], error: Exception, reason: str
) -> None:
    with pytest.raises(ImageFetchError) as caught:
        fetcher(Host(error)).fetch(URL)

    assert caught.value.reason == reason


def test_robots_disallow_stops_the_fetch(fetcher: Callable[..., ImageFetcher]) -> None:
    host = Host(jpeg(), robots="User-agent: *\nDisallow: /a/\n")

    with pytest.raises(ImageFetchError, match=r"^robots\.txt$"):
        fetcher(host).fetch(URL)

    assert host.urls == ["https://img.example.com/robots.txt"]


def test_robots_is_skipped_when_not_respected(fetcher: Callable[..., ImageFetcher]) -> None:
    host = Host(jpeg(), robots="User-agent: *\nDisallow: /\n")

    assert fetcher(host, respect_robots=False).fetch(URL) == ROOM
    assert host.urls == [URL]


def test_unreachable_robots_is_a_failure(fetcher: Callable[..., ImageFetcher]) -> None:
    def host(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/robots.txt":
            raise httpx2.ConnectError("down")
        return jpeg()

    with pytest.raises(ImageFetchError, match=r"robots\.txt unreachable"):
        fetcher(host).fetch(URL)


def test_robots_is_fetched_once_per_host(fetcher: Callable[..., ImageFetcher]) -> None:
    host = Host(jpeg())
    images = fetcher(host)

    images.fetch(URL)
    images.fetch(URL.replace("room", "kitchen"))

    assert host.urls.count("https://img.example.com/robots.txt") == 1


def test_rate_limit_is_per_host(fetcher: Callable[..., ImageFetcher], t: FakeTime) -> None:
    images = fetcher(Host(jpeg()), respect_robots=False, requests_per_minute=60)

    images.fetch("https://a.example.com/1.jpg")
    images.fetch("https://b.example.com/1.jpg")
    assert t.slept == []

    images.fetch("https://a.example.com/2.jpg")
    assert t.slept == [pytest.approx(1.0)]
