"""SourceHttpClient: the polite, resilient HTTP client every adapter uses.

Every request goes through the same steps:

1. **robots.txt.** If ``config.respect_robots`` is set and the URL is disallowed for
   our user agent, :class:`SourceBlockedError` is raised before anything is sent.
   See :mod:`furnest.sources.robots`.
2. **Rate limit.** Each attempt, retries and robots.txt fetches included, takes a
   token from the adapter's :class:`TokenBucket` (``config.requests_per_minute``).
3. **Retry.** A 429, a 5xx, a timeout or a transport error is retried up to
   ``config.max_retries`` times. The wait is exponential backoff with full jitter
   (a random delay up to ``1s * 2**attempt``, capped at 60s). A ``Retry-After``
   header in seconds or as an HTTP-date replaces that wait. If it asks for more
   than five minutes we give up instead of tying up a worker thread.
4. **Classify.** A 403, or an HTML 2xx page that contains a captcha or challenge
   marker, raises :class:`SourceBlockedError`. Any other 4xx, or a retryable
   failure once retries run out, raises :class:`SourceError` with the cause
   chained. Everything else is returned.

Every hop, redirects included, is refused if its host resolves to a non-public
address (:mod:`furnest.netguard`), unless ``config.allow_private_addresses`` is set.

The client is built from the adapter's resolved :class:`AdapterConfig`: timeout,
user agent and proxy (:func:`client_options`). Tests pass an
``httpx2.MockTransport``, plus a fake clock, sleep and random source.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self

import httpx2

from furnest.logging import get_logger
from furnest.netguard import Resolver, UnsafeAddressError, guarded_client_args
from furnest.sources.base import AdapterConfig
from furnest.sources.errors import SourceBlockedError, SourceError
from furnest.sources.ratelimit import TokenBucket
from furnest.sources.robots import RobotsCache

log = get_logger(__name__)

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0
# A Retry-After longer than this is treated as "come back on the next run".
MAX_RETRY_AFTER_SECONDS = 300.0
# Only the start of a page is checked for markers; challenge pages are small.
_MARKER_SCAN_CHARS = 64 * 1024
# Strings that appear on captcha and bot-challenge pages, compared case-insensitively.
DEFAULT_BLOCK_MARKERS = (
    "g-recaptcha",
    "h-captcha",
    "cf-challenge",
    "px-captcha",
    "captcha-delivery",
    "attention required!",
)
_RETRYABLE_STATUSES = frozenset({429}) | frozenset(range(500, 600))


def client_options(config: AdapterConfig) -> dict[str, Any]:
    """The ``httpx2.Client`` keyword arguments for one adapter."""
    return {
        "timeout": httpx2.Timeout(float(config.request_timeout_seconds)),
        "headers": {"User-Agent": config.user_agent},
        "proxy": config.proxy_url,
        "follow_redirects": True,
    }


class SourceHttpClient:
    def __init__(
        self,
        config: AdapterConfig,
        *,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        block_markers: Iterable[str] = (),
        resolver: Resolver | None = None,
    ) -> None:
        self.config = config
        self._sleep = sleep
        self._rng = rng
        self._markers = tuple(m.lower() for m in (*DEFAULT_BLOCK_MARKERS, *block_markers))
        self._http = httpx2.Client(
            **client_options(config),
            **guarded_client_args(
                transport=transport,
                proxy=config.proxy_url,
                allow_private=config.allow_private_addresses,
                resolver=resolver,
            ),
        )
        self.limiter = TokenBucket(config.requests_per_minute, clock=clock, sleep=sleep)
        self.robots = RobotsCache(self._fetch_robots, clock=clock)

    # --- Public API ---------------------------------------------------------------

    def get(self, url: str, **kwargs: Any) -> httpx2.Response:  # noqa: ANN401
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx2.Response:  # noqa: ANN401
        return self.request("POST", url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx2.Response:  # noqa: ANN401
        """Send with robots check, rate limit and retries; raise on block or failure."""
        self.check_robots(url)
        response = self._send(method, url, **kwargs)
        self._check_blocked(response, url)
        if response.is_error:
            msg = f"HTTP {response.status_code} from {url}"
            raise SourceError(msg, url=url, status=response.status_code)
        return response

    def check_robots(self, url: str) -> None:
        """Raise :class:`SourceBlockedError` if robots.txt applies and disallows ``url``.

        The browser layer calls this too, so a rendered page obeys the same rules.
        """
        if self.config.respect_robots and not self.robots.allowed(url, self.config.user_agent):
            log.warning("source.robots_disallowed", adapter=self.config.name, url=url)
            msg = f"robots.txt disallows {url}"
            raise SourceBlockedError(msg, url=url, reason="robots.txt")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- Internals ----------------------------------------------------------------

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx2.Response:  # noqa: ANN401
        """Rate-limited send with retries. Returns the first non-retryable response."""
        attempt = 0
        while True:
            self.limiter.acquire()
            try:
                response = self._http.request(method, url, **kwargs)
            except UnsafeAddressError as exc:  # never retried
                log.warning("source.unsafe_address", adapter=self.config.name, url=url)
                msg = f"refused {url}: {exc}"
                raise SourceError(msg, url=url) from exc
            except httpx2.TransportError as exc:  # timeouts are TransportErrors too
                if attempt >= self.config.max_retries:
                    msg = f"{type(exc).__name__} for {url} after {attempt + 1} attempts"
                    raise SourceError(msg, url=url) from exc
                delay = self._backoff(attempt)
                failure = type(exc).__name__
            else:
                if response.status_code not in _RETRYABLE_STATUSES:
                    return response
                retry_after = _retry_after_seconds(response)
                if retry_after is not None and retry_after > MAX_RETRY_AFTER_SECONDS:
                    msg = f"HTTP {response.status_code} from {url}: Retry-After {retry_after:.0f}s"
                    raise SourceError(msg, url=url, status=response.status_code)
                if attempt >= self.config.max_retries:
                    msg = f"HTTP {response.status_code} from {url} after {attempt + 1} attempts"
                    raise SourceError(msg, url=url, status=response.status_code)
                delay = retry_after if retry_after is not None else self._backoff(attempt)
                failure = f"HTTP {response.status_code}"
            attempt += 1
            log.info(
                "source.retry",
                adapter=self.config.name,
                url=url,
                failure=failure,
                attempt=attempt,
                delay_seconds=round(delay, 2),
            )
            self._sleep(delay)

    def _backoff(self, attempt: int) -> float:
        """Full jitter: uniform in [0, min(cap, base * 2**attempt))."""
        return self._rng() * min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2.0**attempt)

    def _check_blocked(self, response: httpx2.Response, url: str) -> None:
        reason: str | None = None
        if response.status_code == 403:
            reason = "HTTP 403"
        elif response.is_success and "html" in response.headers.get("content-type", ""):
            marker = find_block_marker(response.text, self._markers)
            if marker is not None:
                reason = f"captcha marker {marker!r}"
        if reason is not None:
            log.warning("source.blocked", adapter=self.config.name, url=url, reason=reason)
            msg = f"blocked by {url}: {reason}"
            raise SourceBlockedError(msg, url=url, reason=reason, status=response.status_code)

    def _fetch_robots(self, robots_url: str) -> tuple[int, str]:
        try:
            response = self._send("GET", robots_url)
        except SourceError as exc:
            if exc.status is None:
                raise
            return exc.status, ""
        return response.status_code, response.text


def find_block_marker(text: str, markers: Iterable[str]) -> str | None:
    """The first lower-case ``markers`` entry found in the start of ``text``, if any."""
    head = text[:_MARKER_SCAN_CHARS].lower()
    return next((m for m in markers if m in head), None)


def _retry_after_seconds(response: httpx2.Response) -> float | None:
    """``Retry-After`` as seconds from now, or ``None`` when absent or unparseable."""
    value = response.headers.get("retry-after")
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())
