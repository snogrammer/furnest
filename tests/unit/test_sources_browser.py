"""The browser layer without Playwright: availability checks, options, and
SourceBrowser driven by fake Playwright objects. Real-browser tests live in
tests/integration/test_sources_browser_playwright.py and skip without the extra."""

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.machinery import ModuleSpec
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest

from furnest.config import SourcesSettings
from furnest.sources import (
    AdapterConfig,
    BrowserUnavailableError,
    SourceBlockedError,
    SourceBrowser,
    SourceError,
    SourceHttpClient,
    browser_unavailable_reason,
)
from furnest.sources.browser import (
    BLOCKED_RESOURCE_TYPES,
    block_heavy_resources,
    context_options,
    load_sync_api,
    proxy_settings,
)


def _config(**overrides: Any) -> AdapterConfig:  # noqa: ANN401
    values: dict[str, Any] = {
        "name": "fake",
        "enabled": True,
        "interval_minutes": 60,
        "jitter_seconds": 0,
        "requests_per_minute": 60.0,
        "user_agent": "furnest-test/1",
        "request_timeout_seconds": 7,
    }
    values.update(overrides)
    return AdapterConfig(**values)


# --- Availability ---------------------------------------------------------------


def _spec(name: str) -> ModuleSpec | None:
    return ModuleSpec(name, None)


def _no_spec(name: str) -> ModuleSpec | None:
    return None


def test_unavailable_when_setting_is_off() -> None:
    reason = browser_unavailable_reason(SourcesSettings(browser_enabled=False), find_spec=_spec)

    assert reason == "SOURCES_BROWSER_ENABLED is false"


def test_unavailable_when_playwright_is_missing() -> None:
    reason = browser_unavailable_reason(SourcesSettings(browser_enabled=True), find_spec=_no_spec)

    assert reason is not None
    assert reason.startswith("Playwright is not installed")
    assert "browser" in reason
    assert "playwright install chromium" in reason


def test_available_when_enabled_and_installed() -> None:
    settings = SourcesSettings(browser_enabled=True)

    assert browser_unavailable_reason(settings, find_spec=_spec) is None


def test_load_sync_api_without_playwright_raises_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    with pytest.raises(BrowserUnavailableError, match=r"Playwright is not installed.*extra"):
        load_sync_api()


def test_entering_without_playwright_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    browser = SourceBrowser(_config())

    with pytest.raises(BrowserUnavailableError), browser:
        pass


# --- Options --------------------------------------------------------------------


def test_proxy_settings() -> None:
    assert proxy_settings(None) is None
    assert proxy_settings("http://proxy.example.com:8080/") == {
        "server": "http://proxy.example.com:8080"
    }
    assert proxy_settings("socks5://proxy.example.com") == {"server": "socks5://proxy.example.com"}
    assert proxy_settings("http://us%40er:p%3Ass@proxy.example.com:3128/") == {
        "server": "http://proxy.example.com:3128",
        "username": "us@er",
        "password": "p:ss",
    }


def test_context_options() -> None:
    options = context_options(_config(proxy_url="http://proxy.example.com:8080/"))

    assert options["user_agent"] == "furnest-test/1"
    assert options["proxy"] == {"server": "http://proxy.example.com:8080"}
    assert options["service_workers"] == "block"
    assert "proxy" not in context_options(_config())


@dataclass
class FakeRoute:
    resource_type: str
    calls: list[str] = field(default_factory=list)

    @property
    def request(self) -> SimpleNamespace:
        return SimpleNamespace(resource_type=self.resource_type)

    def abort(self) -> None:
        self.calls.append("abort")

    def fallback(self) -> None:
        self.calls.append("fallback")


@pytest.mark.parametrize(
    ("resource_type", "expected"),
    [
        *((t, "abort") for t in sorted(BLOCKED_RESOURCE_TYPES)),
        ("document", "fallback"),
        ("script", "fallback"),
        ("xhr", "fallback"),
    ],
)
def test_block_heavy_resources(resource_type: str, expected: str) -> None:
    route = FakeRoute(resource_type)

    block_heavy_resources(cast(Any, route))

    assert route.calls == [expected]


# --- SourceBrowser with fake Playwright -------------------------------------------


class FakePlaywrightError(Exception):
    pass


@dataclass
class FakeResponse:
    status: int


class FakePage:
    def __init__(self, world: "FakeWorld") -> None:
        self.world = world

    def goto(self, url: str, *, wait_until: str) -> FakeResponse | None:
        self.world.calls.append(("goto", url, wait_until))
        if self.world.goto_error is not None:
            raise self.world.goto_error
        return None if self.world.status is None else FakeResponse(self.world.status)

    def content(self) -> str:
        return self.world.html


class FakeContext:
    def __init__(self, world: "FakeWorld", options: dict[str, Any]) -> None:
        self.world = world
        self.options = options

    def set_default_timeout(self, ms: float) -> None:
        self.world.calls.append(("timeout", ms))

    def set_default_navigation_timeout(self, ms: float) -> None:
        self.world.calls.append(("navigation_timeout", ms))

    def route(self, pattern: str, handler: Callable[..., None]) -> None:
        self.world.calls.append(("route", pattern, handler))

    def new_page(self) -> FakePage:
        return FakePage(self.world)

    def close(self) -> None:
        self.world.calls.append(("context_closed",))


class FakeBrowser:
    def __init__(self, world: "FakeWorld") -> None:
        self.world = world

    def new_context(self, **options: Any) -> FakeContext:  # noqa: ANN401
        self.world.calls.append(("new_context", options))
        return FakeContext(self.world, options)

    def close(self) -> None:
        self.world.calls.append(("browser_closed",))


@dataclass
class FakeWorld:
    html: str = "<html><body>rendered</body></html>"
    status: int | None = 200
    goto_error: Exception | None = None
    launch_error: Exception | None = None
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def api(self) -> SimpleNamespace:
        world = self

        class Chromium:
            def launch(self, **kwargs: Any) -> FakeBrowser:  # noqa: ANN401
                world.calls.append(("launch", kwargs))
                if world.launch_error is not None:
                    raise world.launch_error
                return FakeBrowser(world)

        class Playwright:
            chromium = Chromium()

            def stop(self) -> None:
                world.calls.append(("playwright_stopped",))

        manager = SimpleNamespace(start=Playwright)
        return SimpleNamespace(Error=FakePlaywrightError, sync_playwright=lambda: manager)

    def names(self) -> list[Any]:
        return [c[0] for c in self.calls]


def _robots_404(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(404)


def _http(config: AdapterConfig, handler: Callable[[httpx2.Request], httpx2.Response]) -> Any:  # noqa: ANN401
    return SourceHttpClient(config, transport=httpx2.MockTransport(handler), sleep=lambda _: None)


@pytest.fixture
def world() -> FakeWorld:
    return FakeWorld()


def _browser(world: FakeWorld, config: AdapterConfig | None = None, **kwargs: Any) -> SourceBrowser:  # noqa: ANN401
    config = config or _config()
    http = kwargs.pop("http", None) or _http(config, _robots_404)
    return SourceBrowser(config, http=http, sync_api=world.api(), **kwargs)


def test_render_launches_headless_and_configures_the_context(world: FakeWorld) -> None:
    # No robots fetch: with a proxy set, httpx2 would bypass the mock transport.
    config = _config(proxy_url="http://proxy.example.com:8080/", respect_robots=False)

    with _browser(world, config) as browser:
        html = browser.render("https://listings.example.com/search")

    assert html == world.html
    assert world.calls[0] == ("launch", {"headless": True})
    (new_context,) = [c for c in world.calls if c[0] == "new_context"]
    assert new_context[1] == context_options(config)
    assert ("timeout", 7000) in world.calls
    assert ("navigation_timeout", 7000) in world.calls
    assert ("route", "**/*", block_heavy_resources) in world.calls
    assert ("goto", "https://listings.example.com/search", "domcontentloaded") in world.calls
    assert world.names()[-3:] == ["context_closed", "browser_closed", "playwright_stopped"]


def test_render_takes_a_token_per_navigation(world: FakeWorld) -> None:
    taken: list[str] = []
    with _browser(world) as browser:
        original = browser.http.limiter.acquire

        def acquire() -> float:
            taken.append("token")
            return original()

        browser.http.limiter.acquire = acquire  # type: ignore[method-assign]
        browser.render("https://listings.example.com/a")
        browser.render("https://listings.example.com/b", wait_until="networkidle")

    # One for robots.txt (cached afterwards), one per navigation.
    assert taken == ["token", "token", "token"]
    assert ("goto", "https://listings.example.com/b", "networkidle") in world.calls


def test_render_honours_robots_txt(world: FakeWorld) -> None:
    def robots(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="User-agent: *\nDisallow: /private\n")

    config = _config()
    with (
        _browser(world, config, http=_http(config, robots)) as browser,
        pytest.raises(SourceBlockedError, match=r"robots\.txt") as caught,
    ):
        browser.render("https://listings.example.com/private/1")

    assert caught.value.reason == "robots.txt"
    assert "goto" not in world.names()


def test_render_ignores_robots_when_disabled(world: FakeWorld) -> None:
    def robots(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("robots.txt should not be fetched")

    config = _config(respect_robots=False)
    with _browser(world, config, http=_http(config, robots)) as browser:
        assert browser.render("https://listings.example.com/private/1") == world.html


def test_render_403_is_blocked(world: FakeWorld) -> None:
    world.status = 403

    with _browser(world) as browser, pytest.raises(SourceBlockedError) as caught:
        browser.render("https://listings.example.com/x")

    assert caught.value.reason == "HTTP 403"
    assert caught.value.status == 403


def test_render_captcha_page_is_blocked(world: FakeWorld) -> None:
    world.html = '<html><div class="g-recaptcha"></div></html>'

    with _browser(world) as browser, pytest.raises(SourceBlockedError, match="g-recaptcha"):
        browser.render("https://listings.example.com/x")


def test_render_custom_block_marker(world: FakeWorld) -> None:
    world.html = "<html>Please Verify You Are Human</html>"

    with (
        _browser(world, block_markers=["verify you are human"]) as browser,
        pytest.raises(SourceBlockedError),
    ):
        browser.render("https://listings.example.com/x")


def test_render_error_status_raises_source_error(world: FakeWorld) -> None:
    world.status = 500

    with _browser(world) as browser, pytest.raises(SourceError, match="HTTP 500") as caught:
        browser.render("https://listings.example.com/x")

    assert not isinstance(caught.value, SourceBlockedError)
    assert caught.value.status == 500


def test_render_without_response_returns_content(world: FakeWorld) -> None:
    world.status = None

    with _browser(world) as browser:
        assert browser.render("https://listings.example.com/x") == world.html


def test_render_playwright_error_becomes_source_error(world: FakeWorld) -> None:
    world.goto_error = FakePlaywrightError("net::ERR_TIMED_OUT")

    with _browser(world) as browser, pytest.raises(SourceError, match="ERR_TIMED_OUT") as caught:
        browser.render("https://listings.example.com/x")

    assert caught.value.url == "https://listings.example.com/x"
    assert isinstance(caught.value.__cause__, FakePlaywrightError)
    assert "context_closed" in world.names()


def test_launch_failure_is_unavailable_and_stops_playwright(world: FakeWorld) -> None:
    world.launch_error = FakePlaywrightError("Executable doesn't exist")
    browser = _browser(world)

    with pytest.raises(BrowserUnavailableError, match="playwright install chromium"), browser:
        pass

    assert world.names() == ["launch", "playwright_stopped"]


def test_new_context_requires_entering(world: FakeWorld) -> None:
    browser = _browser(world)

    with pytest.raises(RuntimeError, match="entered"), browser.new_context():
        pass


def test_close_without_entering_is_harmless(world: FakeWorld) -> None:
    browser = _browser(world)

    browser.close()
    browser.close()

    assert world.calls == []


def test_owned_http_client_is_closed(world: FakeWorld) -> None:
    browser = SourceBrowser(_config(), sync_api=world.api())
    with browser:
        pass

    assert browser.http._http.is_closed


def test_shared_http_client_stays_open(world: FakeWorld) -> None:
    config = _config()
    http = _http(config, _robots_404)
    with SourceBrowser(config, http=http, sync_api=world.api()):
        pass

    assert not http._http.is_closed
    http.close()
