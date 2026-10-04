"""SourceBrowser: headless Chromium for the sources that only render with JavaScript.

Playwright is an optional dependency (the ``browser`` extra), so nothing here imports
it at module load. Two checks keep a missing browser from breaking anything else:

* :func:`browser_unavailable_reason` says, without importing Playwright, whether a
  browser can be used at all: ``SOURCES_BROWSER_ENABLED`` must be true and the
  ``playwright`` package installed. The registry uses it to auto-disable adapters
  whose capabilities say ``needs_browser``, and records the reason (see
  :mod:`furnest.sources.registry`).
* :class:`SourceBrowser` imports Playwright only when it is entered. A missing
  package or Chromium binary raises :class:`BrowserUnavailableError`, which names
  the fix.

Every browser context is built from the adapter's :class:`AdapterConfig`: user
agent, proxy and timeouts, the same values the HTTP client uses. Images, fonts and
media are aborted before they are fetched, since adapters only read the DOM and
skipping them makes pages load much faster.

:meth:`SourceBrowser.render` is the polite path for loading one page. It shares the
adapter's :class:`SourceHttpClient`, so robots.txt rules and the request budget are
the same for both: each navigation takes one token from the adapter's bucket.
Subresources the page loads itself (scripts, XHR) are not counted. A 403 or a
captcha page raises :class:`SourceBlockedError`, like it does over HTTP.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from importlib.machinery import ModuleSpec
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Self
from urllib.parse import unquote, urlsplit, urlunsplit

from furnest.config import SourcesSettings
from furnest.logging import get_logger
from furnest.sources.base import AdapterConfig
from furnest.sources.errors import SourceBlockedError, SourceError
from furnest.sources.http import DEFAULT_BLOCK_MARKERS, SourceHttpClient, find_block_marker

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Playwright, Route

log = get_logger(__name__)

INSTALL_HINT = (
    "install the 'browser' extra (uv sync --extra browser, or pip install 'furnest[browser]') "
    "and then Chromium (playwright install chromium)"
)
# Resource types adapters never need; aborting them speeds up every page load.
BLOCKED_RESOURCE_TYPES = frozenset({"image", "font", "media"})

WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]


class BrowserUnavailableError(SourceError):
    """Playwright or Chromium is missing, or the browser layer is switched off."""

    def __init__(self, message: str, *, url: str = "") -> None:
        super().__init__(message, url=url)


def browser_unavailable_reason(
    settings: SourcesSettings,
    *,
    find_spec: Callable[[str], ModuleSpec | None] = importlib.util.find_spec,
) -> str | None:
    """Why no adapter can use a browser right now, or ``None`` if one can.

    Chromium itself is not launched here. A missing binary shows up as
    :class:`BrowserUnavailableError` when :class:`SourceBrowser` is entered.
    """
    if not settings.browser_enabled:
        return "SOURCES_BROWSER_ENABLED is false"
    if find_spec("playwright") is None:
        return "Playwright is not installed; " + INSTALL_HINT
    return None


def load_sync_api() -> Any:  # noqa: ANN401 - the playwright.sync_api module
    """``playwright.sync_api``, imported on first use."""
    try:
        return importlib.import_module("playwright.sync_api")
    except ImportError as exc:
        msg = "Playwright is not installed; " + INSTALL_HINT
        raise BrowserUnavailableError(msg) from exc


def proxy_settings(proxy_url: str | None) -> dict[str, str] | None:
    """Playwright's proxy dict for ``proxy_url``; credentials go in their own keys."""
    if proxy_url is None:
        return None
    parts = urlsplit(proxy_url)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port is not None else host
    proxy = {"server": urlunsplit((parts.scheme, netloc, "", "", ""))}
    if parts.username is not None:
        proxy["username"] = unquote(parts.username)
    if parts.password is not None:
        proxy["password"] = unquote(parts.password)
    return proxy


def context_options(config: AdapterConfig) -> dict[str, Any]:
    """The ``Browser.new_context`` keyword arguments for one adapter."""
    options: dict[str, Any] = {
        "user_agent": config.user_agent,
        "java_script_enabled": True,
        "service_workers": "block",
        "locale": "en-US",
    }
    proxy = proxy_settings(config.proxy_url)
    if proxy is not None:
        options["proxy"] = proxy
    return options


def block_heavy_resources(route: Route) -> None:
    """Route handler: abort images, fonts and media, let everything else through."""
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.fallback()


class SourceBrowser:
    """A headless Chromium for one adapter. Use it as a context manager.

    ``http`` is the adapter's :class:`SourceHttpClient`, whose robots.txt cache and
    rate limiter :meth:`render` shares. Without one, the browser makes its own from
    ``config`` and closes it on exit. ``sync_api`` replaces the
    ``playwright.sync_api`` module in tests.
    """

    def __init__(
        self,
        config: AdapterConfig,
        *,
        http: SourceHttpClient | None = None,
        sync_api: Any = None,  # noqa: ANN401
        block_markers: Iterable[str] = (),
    ) -> None:
        self.config = config
        self._owns_http = http is None
        self.http = http if http is not None else SourceHttpClient(config)
        self._sync_api = sync_api
        self._markers = tuple(m.lower() for m in (*DEFAULT_BLOCK_MARKERS, *block_markers))
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._error: type[Exception] = Exception

    # --- Lifecycle ----------------------------------------------------------------

    def __enter__(self) -> Self:
        api = self._sync_api if self._sync_api is not None else load_sync_api()
        self._error = api.Error
        self._playwright = api.sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=True)
        except self._error as exc:
            self._playwright.stop()
            self._playwright = None
            msg = f"Chromium could not be launched ({exc}); {INSTALL_HINT}"
            raise BrowserUnavailableError(msg) from exc
        log.info("source.browser_started", adapter=self.config.name)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        if self._owns_http:
            self.http.close()

    # --- Public API ---------------------------------------------------------------

    @contextmanager
    def new_context(self) -> Iterator[BrowserContext]:
        """A fresh browser context with the adapter's UA, proxy, timeouts and blocking."""
        if self._browser is None:
            msg = "SourceBrowser must be entered (with ...) before use"
            raise RuntimeError(msg)
        context = self._browser.new_context(**context_options(self.config))
        timeout_ms = self.config.request_timeout_seconds * 1000
        context.set_default_timeout(timeout_ms)
        context.set_default_navigation_timeout(timeout_ms)
        context.route("**/*", block_heavy_resources)
        try:
            yield context
        finally:
            context.close()

    def render(self, url: str, *, wait_until: WaitUntil = "domcontentloaded") -> str:
        """The HTML of ``url`` after its scripts ran, with robots, rate limit and block checks."""
        self.http.check_robots(url)
        with self.new_context() as context:
            page = context.new_page()
            self.http.limiter.acquire()
            try:
                response = page.goto(url, wait_until=wait_until)
                html: str = page.content()
            except self._error as exc:
                msg = f"{type(exc).__name__} rendering {url}: {exc}"
                raise SourceError(msg, url=url) from exc
        status: int | None = response.status if response is not None else None
        self._check_blocked(url, status, html)
        if status is not None and status >= 400:
            msg = f"HTTP {status} rendering {url}"
            raise SourceError(msg, url=url, status=status)
        return html

    # --- Internals ----------------------------------------------------------------

    def _check_blocked(self, url: str, status: int | None, html: str) -> None:
        reason: str | None = None
        if status == 403:
            reason = "HTTP 403"
        else:
            marker = find_block_marker(html, self._markers)
            if marker is not None:
                reason = f"captcha marker {marker!r}"
        if reason is not None:
            log.warning("source.blocked", adapter=self.config.name, url=url, reason=reason)
            msg = f"blocked by {url}: {reason}"
            raise SourceBlockedError(msg, url=url, reason=reason, status=status)
