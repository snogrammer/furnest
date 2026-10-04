"""RobotsCache: RFC 9309 status handling and per-origin caching."""

import pytest

from furnest.sources import RobotsCache, SourceError

pytestmark = pytest.mark.unit

ROBOTS = """\
User-agent: *
Disallow: /private/

User-agent: furnest
Disallow: /no-furnest/
"""


class FakeFetch:
    def __init__(self, *replies: tuple[int, str]) -> None:
        self.replies = list(replies)
        self.urls: list[str] = []

    def __call__(self, url: str) -> tuple[int, str]:
        self.urls.append(url)
        return self.replies.pop(0)


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def test_rules_are_applied_per_user_agent() -> None:
    fetch = FakeFetch((200, ROBOTS))
    robots = RobotsCache(fetch)

    assert robots.allowed("https://example.com/listings/1", "furnest/0")
    # A group naming our agent replaces the * group rather than adding to it.
    assert robots.allowed("https://example.com/private/x", "furnest/0")
    assert not robots.allowed("https://example.com/no-furnest/x", "furnest/0")
    assert not robots.allowed("https://example.com/private/x", "otherbot")
    assert robots.allowed("https://example.com/no-furnest/x", "otherbot")
    assert fetch.urls == ["https://example.com/robots.txt"]


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_missing_file_allows_everything(status: int) -> None:
    robots = RobotsCache(FakeFetch((status, "")))

    assert robots.allowed("https://example.com/private/x", "furnest/0")


@pytest.mark.parametrize("status", [500, 503, 302])
def test_unreachable_file_raises_and_is_not_cached(status: int) -> None:
    fetch = FakeFetch((status, ""), (200, ROBOTS))
    robots = RobotsCache(fetch)

    with pytest.raises(SourceError, match=r"robots\.txt unavailable") as info:
        robots.allowed("https://example.com/a", "furnest/0")
    assert info.value.status == status
    assert info.value.url == "https://example.com/robots.txt"

    assert robots.allowed("https://example.com/a", "furnest/0")
    assert len(fetch.urls) == 2


def test_cache_is_per_origin_and_expires() -> None:
    clock = Clock()
    fetch = FakeFetch((200, ROBOTS), (200, ""), (404, ""))
    robots = RobotsCache(fetch, ttl_seconds=100, clock=clock)

    assert not robots.allowed("https://example.com/private/1", "otherbot")
    assert robots.allowed("https://other.example.com/private/1", "otherbot")
    clock.now = 99
    assert not robots.allowed("https://example.com/private/2", "otherbot")
    assert len(fetch.urls) == 2

    clock.now = 100
    assert robots.allowed("https://example.com/private/3", "otherbot")
    assert fetch.urls[-1] == "https://example.com/robots.txt"
