"""In-memory sliding-window rate limiter, used for failed login and Bearer attempts.

Each key (a client IP) keeps the timestamps of its recent hits; a key is limited
once ``limit`` hits fall inside the trailing ``window``. State is per process, which
fits furnest's single web process; a restart forgets it.

Memory stays bounded: keys whose hits have all expired are dropped, and at most
``max_keys`` keys are tracked, evicting the least recently hit.
"""

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable


class SlidingWindowLimiter:
    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> float | None:
        """Seconds until ``key`` may try again, or ``None`` when it is not limited."""
        with self._lock:
            hits = self._prune(key, self._clock())
            if hits is None or len(hits) < self.limit:
                return None
            return max(hits[0] + self.window - self._clock(), 0.0)

    def hit(self, key: str) -> None:
        """Record one hit (a failed attempt) for ``key``."""
        with self._lock:
            now = self._clock()
            hits = self._prune(key, now)
            if hits is None:
                hits = self._hits[key] = deque()
            hits.append(now)
            self._hits.move_to_end(key)
            while len(self._hits) > self._max_keys:
                self._hits.popitem(last=False)

    def reset(self, key: str) -> None:
        """Forget ``key``'s hits, e.g. after a successful login."""
        with self._lock:
            self._hits.pop(key, None)

    def __len__(self) -> int:
        return len(self._hits)

    def _prune(self, key: str, now: float) -> deque[float] | None:
        hits = self._hits.get(key)
        if hits is None:
            return None
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if not hits:
            del self._hits[key]
            return None
        return hits
