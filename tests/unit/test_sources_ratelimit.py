"""TokenBucket, driven by a fake clock whose sleep advances time."""

import threading

import pytest

from furnest.sources import TokenBucket

pytestmark = pytest.mark.unit


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def t() -> FakeTime:
    return FakeTime()


def test_first_acquire_is_immediate_then_spaced(t: FakeTime) -> None:
    bucket = TokenBucket(6, clock=t.clock, sleep=t.sleep)  # one token per 10s

    assert bucket.acquire() == 0
    assert bucket.acquire() == pytest.approx(10)
    assert bucket.acquire() == pytest.approx(10)
    assert t.slept == pytest.approx([10, 10])


def test_elapsed_time_refills(t: FakeTime) -> None:
    bucket = TokenBucket(6, clock=t.clock, sleep=t.sleep)
    bucket.acquire()

    t.now += 4
    assert bucket.acquire() == pytest.approx(6)
    t.now += 30
    assert bucket.acquire() == 0


def test_refill_is_capped_at_capacity(t: FakeTime) -> None:
    bucket = TokenBucket(60, capacity=2, clock=t.clock, sleep=t.sleep)
    t.now += 3600  # an idle hour still banks only two tokens

    assert bucket.acquire() == 0
    assert bucket.acquire() == 0
    assert bucket.acquire() == pytest.approx(1)


@pytest.mark.parametrize(("rate", "capacity"), [(0, 1), (-1, 1), (6, 0.5)])
def test_invalid_parameters_are_rejected(rate: float, capacity: float) -> None:
    with pytest.raises(ValueError, match="rate_per_minute"):
        TokenBucket(rate, capacity)


def test_threads_share_the_budget(t: FakeTime) -> None:
    lock = threading.Lock()

    def sleep(seconds: float) -> None:
        with lock:
            t.sleep(seconds)

    bucket = TokenBucket(60, clock=t.clock, sleep=sleep)
    threads = [threading.Thread(target=bucket.acquire) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # One token up front, then four one-second waits: no two threads shared a token.
    assert sum(t.slept) == pytest.approx(4)
