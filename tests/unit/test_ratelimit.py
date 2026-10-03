"""Sliding-window limiter, driven by a fake clock."""

import pytest

from furnest.web.ratelimit import SlidingWindowLimiter

pytestmark = pytest.mark.unit


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def test_limits_after_limit_hits_within_window(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(3, 60, clock=clock)

    for _ in range(2):
        limiter.hit("a")
        assert limiter.retry_after("a") is None
    limiter.hit("a")

    assert limiter.retry_after("a") == 60
    assert limiter.retry_after("b") is None


def test_window_slides(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(2, 60, clock=clock)
    limiter.hit("a")
    clock.now += 30
    limiter.hit("a")

    clock.now += 10
    assert limiter.retry_after("a") == 20  # the first hit expires 20s from now

    clock.now += 20
    assert limiter.retry_after("a") is None  # only the second hit is left
    clock.now += 30
    assert limiter.retry_after("a") is None
    assert len(limiter) == 0  # fully expired keys are dropped


def test_reset_forgets_key(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(1, 60, clock=clock)
    limiter.hit("a")
    assert limiter.retry_after("a") is not None

    limiter.reset("a")
    limiter.reset("never-seen")

    assert limiter.retry_after("a") is None


def test_tracks_at_most_max_keys_evicting_least_recent(clock: FakeClock) -> None:
    limiter = SlidingWindowLimiter(1, 60, clock=clock, max_keys=2)
    limiter.hit("a")
    limiter.hit("b")
    limiter.hit("a")  # refreshes "a"
    limiter.hit("c")  # evicts "b"

    assert len(limiter) == 2
    assert limiter.retry_after("a") is not None
    assert limiter.retry_after("b") is None
    assert limiter.retry_after("c") is not None
