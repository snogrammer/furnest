"""SSRF guard: address classification, pinning, and redirect re-checks in both clients."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from furnest import netguard
from furnest.config import ImageSettings, SourcesSettings
from furnest.dedup.phash import ImageFetcher, ImageFetchError
from furnest.netguard import GuardedTransport, UnsafeAddressError, is_public_address, resolve_public
from furnest.sources import SourceError, SourceHttpClient
from furnest.sources.base import AdapterConfig

pytestmark = pytest.mark.unit

ROOM = (Path(__file__).parents[1] / "fixtures" / "images" / "room.jpg").read_bytes()
HOSTS = {
    "good.example.com": ["93.184.216.34"],
    "evil.example.com": ["10.0.0.5"],
    "mixed.example.com": ["93.184.216.34", "127.0.0.1"],
}


def resolver(host: str, port: int) -> list[str]:
    return HOSTS[host]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "172.17.0.2",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",  # noqa: S104
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "::",
        "fe80::1",
        "fd00::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "64:ff9b::a00:1",
        "2002:7f00:1::",
        "not-an-ip",
    ],
)
def test_refuses_non_public_addresses(address: str) -> None:
    assert not is_public_address(address)


@pytest.mark.parametrize(
    "address", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1::1", "::ffff:8.8.8.8"]
)
def test_accepts_public_addresses(address: str) -> None:
    assert is_public_address(address)


def test_one_bad_record_is_enough() -> None:
    with pytest.raises(UnsafeAddressError):
        resolve_public("mixed.example.com", 443, resolver)


def test_unresolvable_host_is_refused() -> None:
    def fail(host: str, port: int) -> list[str]:
        raise OSError("nope")

    with pytest.raises(UnsafeAddressError):
        resolve_public("nowhere.invalid", 443, fail)


def test_transport_pins_the_validated_address(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx2.Request] = []

    def fake(self: Any, request: httpx2.Request) -> httpx2.Response:  # noqa: ANN401
        seen.append(request)
        return httpx2.Response(200)

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", fake)
    with httpx2.Client(transport=GuardedTransport(resolver=resolver)) as client:
        client.get("https://good.example.com/x.jpg")

    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "good.example.com"
    assert seen[0].extensions["sni_hostname"] == "good.example.com"


def test_transport_refuses_internal_host() -> None:
    with (
        httpx2.Client(transport=GuardedTransport(resolver=resolver)) as client,
        pytest.raises(UnsafeAddressError),
    ):
        client.get("http://evil.example.com/")


def test_guard_can_be_disabled() -> None:
    args = netguard.guarded_client_args(
        transport=None, proxy=None, allow_private=True, resolver=resolver
    )
    assert args == {"transport": None}


# --- ImageFetcher -------------------------------------------------------------------


def fetcher(handler: Callable[[httpx2.Request], httpx2.Response], **sources: Any) -> ImageFetcher:  # noqa: ANN401
    return ImageFetcher(
        ImageSettings(),
        SourcesSettings(respect_robots=False, **sources),
        transport=httpx2.MockTransport(handler),
        resolver=resolver,
    )


def image_host(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, content=ROOM, headers={"content-type": "image/jpeg"})


def test_image_on_internal_host_is_refused() -> None:
    with pytest.raises(ImageFetchError) as caught:
        fetcher(image_host).fetch("http://evil.example.com/a.jpg")
    assert caught.value.reason == "blocked address"


def test_image_redirect_to_internal_host_is_refused() -> None:
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.url.host)
        if request.url.host == "good.example.com":
            return httpx2.Response(302, headers={"location": "http://evil.example.com/a.jpg"})
        return image_host(request)

    with pytest.raises(ImageFetchError) as caught:
        fetcher(handler).fetch("https://good.example.com/a.jpg")

    assert caught.value.reason == "blocked address"
    assert sent == ["good.example.com"]


def test_image_redirect_to_public_host_still_works() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/a.jpg":
            return httpx2.Response(302, headers={"location": "https://good.example.com/b.jpg"})
        return image_host(request)

    assert fetcher(handler).fetch("https://good.example.com/a.jpg") == ROOM


def test_allow_private_addresses_turns_the_guard_off() -> None:
    assert fetcher(image_host, allow_private_addresses=True).fetch("http://evil.example.com/a.jpg")


# --- SourceHttpClient ---------------------------------------------------------------


def config(**changes: Any) -> AdapterConfig:  # noqa: ANN401
    values: dict[str, Any] = {
        "name": "example_source",
        "enabled": True,
        "interval_minutes": 60,
        "jitter_seconds": 0,
        "requests_per_minute": 6000.0,
        "user_agent": "furnest-test/1",
        "request_timeout_seconds": 7,
        "respect_robots": False,
        "max_retries": 3,
    }
    values.update(changes)
    return AdapterConfig(**values)


def test_source_redirect_to_internal_host_is_refused_without_retry() -> None:
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.url.host)
        return httpx2.Response(302, headers={"location": "http://evil.example.com/meta"})

    with (
        SourceHttpClient(
            config(), transport=httpx2.MockTransport(handler), resolver=resolver
        ) as http,
        pytest.raises(SourceError, match="non-public"),
    ):
        http.get("https://good.example.com/search")

    assert sent == ["good.example.com"]


def test_source_allows_internal_host_when_configured() -> None:
    handler = lambda request: httpx2.Response(200, text="ok")  # noqa: E731
    with SourceHttpClient(
        config(allow_private_addresses=True),
        transport=httpx2.MockTransport(handler),
        resolver=resolver,
    ) as http:
        assert http.get("http://evil.example.com/").text == "ok"
