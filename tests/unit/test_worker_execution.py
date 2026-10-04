"""Running a job with a timeout, and the handler registry."""

import threading
from datetime import timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from furnest.db.base import utcnow
from furnest.worker.execution import (
    Handler,
    JobContext,
    JobRegistry,
    OutcomeKind,
    default_registry,
    run_with_timeout,
)

pytestmark = pytest.mark.unit


def _waits_for(event: threading.Event) -> Handler:
    def handler(_ctx: JobContext) -> None:
        event.wait(5)

    return handler


def _ctx(timeout: timedelta = timedelta(seconds=5)) -> JobContext:
    return JobContext(job_id=1, kind="send", payload={}, attempt=1, deadline=utcnow() + timeout)


def test_ok() -> None:
    seen: list[JobContext] = []
    ctx = _ctx()

    outcome = run_with_timeout(seen.append, ctx, abandon=threading.Event())

    assert outcome.kind is OutcomeKind.OK
    assert seen == [ctx]
    assert not ctx.cancelled.is_set()


def test_error_is_returned_not_raised() -> None:
    def boom(_ctx: JobContext) -> None:
        raise ValueError("nope")

    outcome = run_with_timeout(boom, _ctx(), abandon=threading.Event())

    assert outcome.kind is OutcomeKind.ERROR
    assert isinstance(outcome.error, ValueError)


def test_timeout_stops_waiting_and_cancels_the_job() -> None:
    finished = threading.Event()

    def stuck(ctx: JobContext) -> None:
        ctx.cancelled.wait(5)
        finished.set()

    ctx = _ctx(timedelta(milliseconds=200))
    outcome = run_with_timeout(stuck, ctx, abandon=threading.Event())

    assert outcome.kind is OutcomeKind.TIMED_OUT
    assert ctx.cancelled.is_set()
    assert finished.wait(2), "a cooperative job stops once cancelled"


def test_abandon_stops_waiting() -> None:
    abandon = threading.Event()
    release = threading.Event()
    ctx = _ctx()
    threading.Timer(0.2, abandon.set).start()

    outcome = run_with_timeout(_waits_for(release), ctx, abandon=abandon)
    release.set()

    assert outcome.kind is OutcomeKind.ABANDONED
    assert ctx.cancelled.is_set()


def test_on_finish_runs_after_the_handler_even_when_it_timed_out() -> None:
    release = threading.Event()
    finished = threading.Event()

    outcome = run_with_timeout(
        _waits_for(release),
        _ctx(timedelta(milliseconds=150)),
        abandon=threading.Event(),
        on_finish=finished.set,
    )

    assert outcome.kind is OutcomeKind.TIMED_OUT
    assert not finished.is_set(), "a stuck job keeps its lease"
    release.set()
    assert finished.wait(2)


def test_registry_lookup_and_longest_timeout() -> None:
    registry = JobRegistry()
    registry.register("quick", lambda _ctx: None)
    registry.register("slow", lambda _ctx: None, timeout=timedelta(hours=1))

    quick, slow = registry.get("quick"), registry.get("slow")
    assert quick is not None
    assert quick.timeout is None
    assert slow is not None
    assert slow.timeout == timedelta(hours=1)
    assert registry.get("unknown") is None
    assert registry.longest_timeout(timedelta(minutes=5)) == timedelta(hours=1)
    assert JobRegistry().longest_timeout(timedelta(minutes=5)) == timedelta(minutes=5)


def test_registering_a_kind_twice_is_an_error() -> None:
    registry = JobRegistry()
    registry.register("send", lambda _ctx: None)

    with pytest.raises(ValueError, match="already registered"):
        registry.register("send", lambda _ctx: None)


def test_default_registry_runs_criteria_reruns() -> None:
    registry = default_registry(sessionmaker())
    assert registry.get("rerun_criteria") is not None
    assert registry.get("anything") is None
