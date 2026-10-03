"""Lease locks: contention, expiry takeover, renew and release, and a cross-process race."""

import multiprocessing
from datetime import timedelta
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Event

import pytest
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.base import utcnow
from furnest.db.engine import create_sqlite_engine
from furnest.db.models import JobLock
from furnest.db.session import make_session_factory, session_scope
from furnest.worker import locks

pytestmark = pytest.mark.integration

TTL = timedelta(minutes=5)


def _lock(factory: sessionmaker[Session], name: str = "scrape") -> JobLock | None:
    with session_scope(factory) as session:
        return session.get(JobLock, name)


def test_acquire_free_lease(session_factory: sessionmaker[Session]) -> None:
    now = utcnow()

    assert locks.acquire(session_factory, "scrape", "a", TTL, now=now)

    lock = _lock(session_factory)
    assert lock is not None
    assert (lock.owner, lock.acquired_at, lock.expires_at) == ("a", now, now + TTL)


def test_second_owner_is_refused_while_lease_is_live(
    session_factory: sessionmaker[Session],
) -> None:
    now = utcnow()
    locks.acquire(session_factory, "scrape", "a", TTL, now=now)

    assert not locks.acquire(session_factory, "scrape", "b", TTL, now=now + TTL / 2)

    lock = _lock(session_factory)
    assert lock is not None
    assert lock.owner == "a"


def test_different_names_do_not_contend(session_factory: sessionmaker[Session]) -> None:
    assert locks.acquire(session_factory, "scrape:one", "a", TTL)
    assert locks.acquire(session_factory, "scrape:two", "b", TTL)


def test_expired_lease_is_taken_over(session_factory: sessionmaker[Session]) -> None:
    start = utcnow()
    locks.acquire(session_factory, "scrape", "a", TTL, now=start)
    later = start + TTL

    assert locks.acquire(session_factory, "scrape", "b", TTL, now=later)

    lock = _lock(session_factory)
    assert lock is not None
    assert (lock.owner, lock.acquired_at, lock.expires_at) == ("b", later, later + TTL)


def test_owner_reacquiring_extends_without_resetting_acquired_at(
    session_factory: sessionmaker[Session],
) -> None:
    start = utcnow()
    locks.acquire(session_factory, "scrape", "a", TTL, now=start)

    assert locks.acquire(session_factory, "scrape", "a", TTL, now=start + TTL / 2)

    lock = _lock(session_factory)
    assert lock is not None
    assert (lock.acquired_at, lock.expires_at) == (start, start + TTL / 2 + TTL)


def test_renew_extends_our_lease(session_factory: sessionmaker[Session]) -> None:
    start = utcnow()
    locks.acquire(session_factory, "scrape", "a", TTL, now=start)

    assert locks.renew(session_factory, "scrape", "a", TTL, now=start + TTL / 2)

    lock = _lock(session_factory)
    assert lock is not None
    assert lock.expires_at == start + TTL / 2 + TTL


def test_renew_fails_after_takeover(session_factory: sessionmaker[Session]) -> None:
    start = utcnow()
    locks.acquire(session_factory, "scrape", "a", TTL, now=start)
    locks.acquire(session_factory, "scrape", "b", TTL, now=start + TTL)

    assert not locks.renew(session_factory, "scrape", "a", TTL, now=start + TTL)
    assert not locks.renew(session_factory, "missing", "a", TTL)

    lock = _lock(session_factory)
    assert lock is not None
    assert lock.owner == "b"


def test_release_frees_only_our_own_lease(session_factory: sessionmaker[Session]) -> None:
    locks.acquire(session_factory, "scrape", "a", TTL)

    locks.release(session_factory, "scrape", "b")
    assert _lock(session_factory) is not None

    locks.release(session_factory, "scrape", "a")
    assert _lock(session_factory) is None
    assert locks.acquire(session_factory, "scrape", "b", TTL)


RACERS = 6


def _race(db_url: str, owner: str, start: Event, results: "Queue[tuple[str, bool]]") -> None:
    engine = create_sqlite_engine(db_url)
    start.wait()
    results.put((owner, locks.acquire(make_session_factory(engine), "scrape", owner, TTL)))
    engine.dispose()


def test_processes_racing_for_a_lease_have_exactly_one_winner(
    db_url: str, session_factory: sessionmaker[Session]
) -> None:
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    results: Queue[tuple[str, bool]] = ctx.Queue()
    racers = [
        ctx.Process(target=_race, args=(db_url, f"worker-{i}", start, results))
        for i in range(RACERS)
    ]
    for process in racers:
        process.start()
    start.set()
    outcomes = dict(results.get(timeout=60) for _ in racers)
    for process in racers:
        process.join(timeout=60)

    winners = [owner for owner, won in outcomes.items() if won]
    assert len(outcomes) == RACERS
    assert len(winners) == 1
    lock = _lock(session_factory)
    assert lock is not None
    assert lock.owner == winners[0]
