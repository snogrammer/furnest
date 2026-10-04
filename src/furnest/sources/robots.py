"""robots.txt checks, cached per origin.

Status codes are handled as RFC 9309 says:

* 2xx: the file is parsed and its rules apply;
* 4xx: there is no usable file, so everything is allowed;
* 5xx or a transport failure: the file is unreachable, so the crawler has to assume
  it may crawl nothing. We raise :class:`SourceError` and cache nothing, so the
  next run asks again instead of being shut out for the whole TTL.

``fetch`` is injected. The HTTP client passes a function that goes through its rate
limiter but skips the robots check itself.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from furnest.sources.errors import SourceError

# Fetches a robots.txt URL and returns (status, body). Transport failures raise.
RobotsFetch = Callable[[str], tuple[int, str]]

DEFAULT_TTL_SECONDS = 24 * 60 * 60


class RobotsCache:
    def __init__(
        self,
        fetch: RobotsFetch,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, tuple[float, RobotFileParser]] = {}

    def allowed(self, url: str, user_agent: str) -> bool:
        """Whether ``user_agent`` may fetch ``url``. Raises :class:`SourceError` if
        the origin's robots.txt cannot be reached."""
        return self._parser(url).can_fetch(user_agent, url)

    def _parser(self, url: str) -> RobotFileParser:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        now = self._clock()
        cached = self._entries.get(origin)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]

        robots_url = f"{origin}/robots.txt"
        status, body = self._fetch(robots_url)
        parser = RobotFileParser(robots_url)
        if 200 <= status < 300:
            parser.parse(body.splitlines())
        elif 400 <= status < 500:
            parser.parse([])  # no rules, so everything is allowed
        else:
            msg = f"robots.txt unavailable (HTTP {status})"
            raise SourceError(msg, url=robots_url, status=status)
        self._entries[origin] = (now, parser)
        return parser
