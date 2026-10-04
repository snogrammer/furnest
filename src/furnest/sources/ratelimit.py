"""Token-bucket rate limiter: one per adapter, shared by its worker threads.

Tokens refill continuously at ``rate_per_minute / 60`` per second, up to
``capacity``. :meth:`TokenBucket.acquire` takes one token and sleeps first if the
bucket is empty. The default capacity of 1 allows no bursts, so requests are spaced
evenly. That is the polite choice for sites we scrape.

The lock is held while sleeping. A waiting thread therefore queues the others
behind it instead of letting them race for the next token.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    def __init__(
        self,
        rate_per_minute: float,
        capacity: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_minute <= 0 or capacity < 1:
            msg = "rate_per_minute must be positive and capacity at least 1"
            raise ValueError(msg)
        self.rate = rate_per_minute / 60.0
        self.capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._tokens = capacity
        self._updated = clock()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Take one token, sleeping until one is available. Returns the seconds slept."""
        with self._lock:
            self._refill()
            waited = 0.0
            if self._tokens < 1:
                waited = (1 - self._tokens) / self.rate
                self._sleep(waited)
                self._refill()
                # A fake or coarse clock may not have advanced by exactly ``waited``.
                self._tokens = max(self._tokens, 1.0)
            self._tokens -= 1
            return waited

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now
