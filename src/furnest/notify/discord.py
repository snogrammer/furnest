"""DiscordClient: the few Discord REST calls furnest makes, with Discord's rate limits obeyed.

furnest talks to Discord over plain HTTPS with the bot token, not through a gateway
connection, so a sync ``httpx2`` client is all the worker needs. It posts a match
message and later edits it. Message payloads come from :mod:`furnest.notify.embeds`.

Every request goes through the same steps:

1. **Wait out known limits.** Discord reports each route's budget in
   ``X-RateLimit-Remaining`` and ``X-RateLimit-Reset-After``. Once a route has none
   left, the next request on it sleeps until the reset instead of collecting a 429.
   Routes are keyed by method and channel (the "major parameter" Discord buckets on),
   so a busy channel never slows another. A global 429 blocks every route.
2. **429.** Wait the ``retry_after`` Discord asks for (JSON body, then the
   ``Retry-After`` header), then try again. A wait above
   :data:`MAX_RATE_LIMIT_WAIT_SECONDS`, or more than :data:`MAX_RATE_LIMIT_WAITS` in
   a row, raises :class:`DiscordRateLimitedError` rather than tying up a worker
   thread; the job queue retries later.
3. **Retry.** A 5xx, a timeout or a transport error is retried up to
   ``max_retries`` times with exponential backoff and full jitter, as in
   :mod:`furnest.sources.http`.
4. **Classify.** A 404 raises :class:`DiscordNotFoundError` (the message or channel
   was deleted), any other error :class:`DiscordError` with Discord's own error code.

The token is only ever sent in the ``Authorization`` header. Error messages and log
lines name the route, which holds channel and message ids but nothing secret.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx2

from furnest import __version__
from furnest.config import Settings
from furnest.logging import get_logger

log = get_logger(__name__)

DISCORD_API = "https://discord.com/api/v10"
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0
# A 429 asking for a longer wait than this gives up; the caller retries later.
MAX_RATE_LIMIT_WAIT_SECONDS = 60.0
# 429s in a row on one request before giving up, so a confused bucket can't loop.
MAX_RATE_LIMIT_WAITS = 5

JsonObject = dict[str, Any]


class DiscordError(Exception):
    """A Discord API call failed. ``code`` is Discord's JSON error code, when it sent one."""

    def __init__(self, message: str, *, status: int | None = None, code: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class DiscordNotFoundError(DiscordError):
    """HTTP 404: the channel or message is gone (or the bot can't see it)."""


class DiscordRateLimitedError(DiscordError):
    """Discord asked us to wait longer than we are willing to block for."""

    def __init__(self, message: str, *, retry_after: float) -> None:
        super().__init__(message, status=429)
        self.retry_after = retry_after


@dataclass(frozen=True)
class DiscordMessage:
    id: str
    channel_id: str


def user_agent(base_url: str) -> str:
    """The ``DiscordBot (url, version)`` User-Agent Discord requires of bots."""
    return f"DiscordBot ({base_url.rstrip('/')}, {__version__})"


class DiscordClient:
    def __init__(
        self,
        token: str,
        *,
        user_agent: str,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        api_base: str = DISCORD_API,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.max_retries = max_retries
        self._api = api_base.rstrip("/")
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        # Monotonic times before which a route (or, for the global limit, any route)
        # must not be called.
        self._route_reset: dict[str, float] = {}
        self._global_reset = 0.0
        self._http = httpx2.Client(
            transport=transport,
            timeout=httpx2.Timeout(timeout_seconds),
            headers={"Authorization": f"Bot {token}", "User-Agent": user_agent},
        )

    # --- Public API ---------------------------------------------------------------

    def create_message(self, channel_id: str, payload: Mapping[str, Any]) -> DiscordMessage:
        """Post a message (a payload from :mod:`furnest.notify.embeds`) to ``channel_id``."""
        path = f"/channels/{channel_id}/messages"
        data = self._request("POST", path, route=path, json=payload)
        return _message(data, channel_id)

    def edit_message(
        self, channel_id: str, message_id: str, payload: Mapping[str, Any]
    ) -> DiscordMessage:
        """Replace the embeds and components of one of the bot's messages."""
        data = self._request(
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            route=f"/channels/{channel_id}/messages/:id",
            json=payload,
        )
        return _message(data, channel_id)

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

    def _request(
        self,
        method: str,
        path: str,
        *,
        route: str,
        json: Mapping[str, Any],
    ) -> JsonObject:
        """Send with rate-limit waits and retries; return the JSON body or raise."""
        key = f"{method} {route}"
        name = f"{method} {path}"
        attempt = 0
        rate_limit_waits = 0
        while True:
            self._wait_for(key)
            try:
                response = self._http.request(method, f"{self._api}{path}", json=dict(json))
            except httpx2.TransportError as exc:  # timeouts are TransportErrors too
                if attempt >= self.max_retries:
                    msg = f"Discord {name}: {type(exc).__name__} after {attempt + 1} attempts"
                    raise DiscordError(msg) from exc
                failure = type(exc).__name__
            else:
                self._remember_bucket(key, response)
                status = response.status_code
                if status == 429:
                    rate_limit_waits += 1
                    self._rate_limited(key, name, response, rate_limit_waits)
                    continue
                if status < 500:
                    return _result(name, response)
                if attempt >= self.max_retries:
                    msg = f"Discord {name}: HTTP {status} after {attempt + 1} attempts"
                    raise DiscordError(msg, status=status)
                failure = f"HTTP {status}"
            delay = self._backoff(attempt)
            attempt += 1
            log.info(
                "discord.retry",
                request=name,
                failure=failure,
                attempt=attempt,
                delay_seconds=round(delay, 2),
            )
            self._sleep(delay)

    def _wait_for(self, key: str) -> None:
        reset = max(self._global_reset, self._route_reset.get(key, 0.0))
        delay = reset - self._clock()
        if delay > 0:
            self._sleep(delay)

    def _remember_bucket(self, key: str, response: httpx2.Response) -> None:
        """Note when an exhausted route resets, from Discord's rate-limit headers."""
        remaining = response.headers.get("x-ratelimit-remaining")
        reset_after = _float(response.headers.get("x-ratelimit-reset-after"))
        if remaining == "0" and reset_after is not None:
            self._route_reset[key] = self._clock() + reset_after
        else:
            self._route_reset.pop(key, None)

    def _rate_limited(self, key: str, name: str, response: httpx2.Response, waits: int) -> None:
        """Record a 429's wait so the next attempt sleeps it out, or give up."""
        body = _json(response)
        retry_after = _float(body.get("retry_after")) if body else None
        if retry_after is None:
            retry_after = _float(response.headers.get("retry-after"))
        if retry_after is None:
            retry_after = BACKOFF_BASE_SECONDS
        is_global = bool(body.get("global")) if body else False
        is_global = is_global or response.headers.get("x-ratelimit-global", "").lower() == "true"
        if retry_after > MAX_RATE_LIMIT_WAIT_SECONDS or waits > MAX_RATE_LIMIT_WAITS:
            msg = f"Discord {name}: rate limited, retry after {retry_after:.1f}s"
            raise DiscordRateLimitedError(msg, retry_after=retry_after)
        until = self._clock() + retry_after
        if is_global:
            self._global_reset = max(self._global_reset, until)
        else:
            self._route_reset[key] = until
        log.info(
            "discord.rate_limited",
            request=name,
            retry_after_seconds=retry_after,
            is_global=is_global,
        )

    def _backoff(self, attempt: int) -> float:
        """Full jitter: uniform in [0, min(cap, base * 2**attempt))."""
        return self._rng() * min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2.0**attempt)


def discord_client(settings: Settings, **kwargs: Any) -> DiscordClient | None:  # noqa: ANN401
    """A client from settings, or ``None`` when no bot token or channel is configured.

    ``kwargs`` go to :class:`DiscordClient` (tests pass a transport and a fake sleep).
    """
    discord = settings.discord
    if discord.bot_token is None or not discord.channel_id:
        return None
    return DiscordClient(
        discord.bot_token.get_secret_value(),
        user_agent=user_agent(str(settings.app.base_url)),
        timeout_seconds=discord.request_timeout_seconds,
        max_retries=discord.max_retries,
        **kwargs,
    )


def _result(name: str, response: httpx2.Response) -> JsonObject:
    """The JSON body of a non-retryable response, or the matching error."""
    body = _json(response)
    if response.is_success:
        if body is None:
            msg = f"Discord {name}: HTTP {response.status_code} without a JSON object"
            raise DiscordError(msg, status=response.status_code)
        return body
    code = body.get("code") if body else None
    code = code if isinstance(code, int) else None
    detail = body.get("message") if body else None
    msg = f"Discord {name}: HTTP {response.status_code}"
    if code is not None or detail:
        msg += f" ({code}: {detail})"
    error = DiscordNotFoundError if response.status_code == 404 else DiscordError
    raise error(msg, status=response.status_code, code=code)


def _message(data: JsonObject, channel_id: str) -> DiscordMessage:
    message_id = data.get("id")
    if not isinstance(message_id, str):
        msg = "Discord returned a message without an id"
        raise DiscordError(msg)
    channel = data.get("channel_id")
    return DiscordMessage(
        id=message_id, channel_id=channel if isinstance(channel, str) else channel_id
    )


def _json(response: httpx2.Response) -> JsonObject | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None
