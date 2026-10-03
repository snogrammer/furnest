"""The DB job queue: ordering, claiming, success, retry with backoff, dead jobs, reclaim."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.base import utcnow
from furnest.db.models import Job, JobStatus
from furnest.db.session import session_scope
from furnest.worker import queue

pytestmark = pytest.mark.integration

LEASE = timedelta(minutes=10)
RETRY = timedelta(seconds=30)


def _enqueue(
    factory: sessionmaker[Session],
    *,
    run_after: datetime | None = None,
    max_attempts: int | None = None,
) -> int:
    with session_scope(factory) as session:
        job = queue.enqueue(
            session,
            "send",
            {"to": "someone@example.com"},
            run_after=run_after,
            max_attempts=max_attempts,
        )
        return job.id


def _job(factory: sessionmaker[Session], job_id: int) -> Job:
    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job


def test_enqueue_defaults(session_factory: sessionmaker[Session]) -> None:
    before = utcnow()
    job = _job(session_factory, _enqueue(session_factory))

    assert job.status is JobStatus.QUEUED
    assert (job.attempts, job.max_attempts) == (0, 5)
    assert job.payload == {"to": "someone@example.com"}
    assert job.run_after >= before


def test_enqueue_rolls_back_with_the_callers_transaction(
    session_factory: sessionmaker[Session],
) -> None:
    with pytest.raises(RuntimeError), session_scope(session_factory) as session:
        queue.enqueue(session, "send")
        raise RuntimeError

    assert queue.claim(session_factory, "w", LEASE) is None


def test_claim_returns_none_when_empty(session_factory: sessionmaker[Session]) -> None:
    assert queue.claim(session_factory, "w", LEASE) is None


def test_claim_takes_oldest_due_job_and_leases_it(session_factory: sessionmaker[Session]) -> None:
    now = utcnow()
    later = _enqueue(session_factory, run_after=now - timedelta(seconds=1))
    first = _enqueue(session_factory, run_after=now - timedelta(seconds=2))
    _enqueue(session_factory, run_after=now + timedelta(hours=1))  # not due

    claimed = queue.claim(session_factory, "w", LEASE, now=now)

    assert claimed is not None
    assert (claimed.id, claimed.kind, claimed.attempt) == (first, "send", 1)
    assert claimed.payload == {"to": "someone@example.com"}
    job = _job(session_factory, first)
    assert job.status is JobStatus.RUNNING
    assert (job.attempts, job.locked_by, job.locked_until) == (1, "w", now + LEASE)

    second = queue.claim(session_factory, "w", LEASE, now=now)
    assert second is not None
    assert second.id == later
    assert queue.claim(session_factory, "w", LEASE, now=now) is None


def test_ties_on_run_after_are_broken_by_id(session_factory: sessionmaker[Session]) -> None:
    now = utcnow()
    ids = [_enqueue(session_factory, run_after=now) for _ in range(3)]

    claimed = [queue.claim(session_factory, "w", LEASE, now=now) for _ in ids]

    assert [c.id for c in claimed if c] == ids


def test_complete_marks_succeeded(session_factory: sessionmaker[Session]) -> None:
    job_id = _enqueue(session_factory)
    queue.claim(session_factory, "w", LEASE)

    assert queue.complete(session_factory, job_id, "w")

    job = _job(session_factory, job_id)
    assert job.status is JobStatus.SUCCEEDED
    assert (job.locked_by, job.locked_until) == (None, None)
    assert queue.claim(session_factory, "w", LEASE) is None


def test_fail_schedules_a_retry_after_the_backoff(session_factory: sessionmaker[Session]) -> None:
    now = utcnow()
    job_id = _enqueue(session_factory, run_after=now)
    queue.claim(session_factory, "w", LEASE, now=now)

    status = queue.fail(session_factory, job_id, "w", "SMTPError: boom", retry_in=RETRY, now=now)

    assert status is JobStatus.FAILED
    job = _job(session_factory, job_id)
    assert (job.status, job.run_after, job.last_error) == (
        JobStatus.FAILED,
        now + RETRY,
        "SMTPError: boom",
    )
    assert (job.locked_by, job.locked_until) == (None, None)
    assert queue.claim(session_factory, "w", LEASE, now=now + RETRY / 2) is None
    retried = queue.claim(session_factory, "w", LEASE, now=now + RETRY)
    assert retried is not None
    assert retried.attempt == 2


def test_job_dies_when_attempts_run_out(session_factory: sessionmaker[Session]) -> None:
    now = utcnow()
    job_id = _enqueue(session_factory, run_after=now, max_attempts=2)
    statuses = []
    for _ in range(2):
        claimed = queue.claim(session_factory, "w", LEASE, now=now)
        assert claimed is not None
        statuses.append(queue.fail(session_factory, job_id, "w", "boom", retry_in=RETRY, now=now))
        now += RETRY

    assert statuses == [JobStatus.FAILED, JobStatus.DEAD]
    assert queue.claim(session_factory, "w", LEASE, now=now + timedelta(days=1)) is None


def test_last_error_keeps_the_tail_of_long_messages(
    session_factory: sessionmaker[Session],
) -> None:
    job_id = _enqueue(session_factory)
    queue.claim(session_factory, "w", LEASE)

    queue.fail(session_factory, job_id, "w", "x" * 5000 + "END", retry_in=RETRY)

    error = _job(session_factory, job_id).last_error
    assert error is not None
    assert len(error) == 4000
    assert error.endswith("END")


def test_running_job_with_expired_lease_is_reclaimed(
    session_factory: sessionmaker[Session],
) -> None:
    now = utcnow()
    job_id = _enqueue(session_factory, run_after=now)
    queue.claim(session_factory, "dead-worker", LEASE, now=now)

    assert queue.claim(session_factory, "w", LEASE, now=now + LEASE / 2) is None
    reclaimed = queue.claim(session_factory, "w", LEASE, now=now + LEASE + timedelta(seconds=1))

    assert reclaimed is not None
    assert (reclaimed.id, reclaimed.attempt) == (job_id, 2)


def test_stale_worker_cannot_settle_a_reclaimed_job(session_factory: sessionmaker[Session]) -> None:
    now = utcnow()
    job_id = _enqueue(session_factory, run_after=now)
    queue.claim(session_factory, "slow", LEASE, now=now)
    queue.claim(session_factory, "fresh", LEASE, now=now + LEASE + timedelta(seconds=1))

    assert not queue.complete(session_factory, job_id, "slow")
    assert queue.fail(session_factory, job_id, "slow", "late", retry_in=RETRY) is None
    assert not queue.requeue(session_factory, job_id, "slow", "late")
    assert not queue.complete(session_factory, 999_999, "fresh")

    job = _job(session_factory, job_id)
    assert (job.status, job.locked_by) == (JobStatus.RUNNING, "fresh")


def test_requeue_hands_back_without_counting_the_attempt(
    session_factory: sessionmaker[Session],
) -> None:
    now = utcnow()
    job_id = _enqueue(session_factory, run_after=now - timedelta(minutes=1))
    queue.claim(session_factory, "w", LEASE, now=now)

    assert queue.requeue(session_factory, job_id, "w", "interrupted", now=now)

    job = _job(session_factory, job_id)
    assert (job.status, job.attempts, job.run_after, job.last_error) == (
        JobStatus.QUEUED,
        0,
        now,
        "interrupted",
    )
    claimed = queue.claim(session_factory, "other", LEASE, now=now)
    assert claimed is not None
    assert claimed.attempt == 1


# --- backoff ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "seconds"), [(1, 30), (2, 60), (3, 120), (4, 240), (8, 3600), (50, 3600)]
)
def test_backoff_doubles_up_to_the_cap(attempt: int, seconds: int) -> None:
    assert queue.backoff(attempt, base=30, cap=3600, rng=lambda: 0.0) == timedelta(seconds=seconds)


def test_backoff_jitter_reduces_by_at_most_half() -> None:
    assert queue.backoff(2, base=30, cap=3600, rng=lambda: 1.0) == timedelta(seconds=30)
    assert queue.backoff(2, base=30, cap=3600, rng=lambda: 0.5) == timedelta(seconds=45)
    for _ in range(100):
        assert timedelta(seconds=30) <= queue.backoff(2, base=30, cap=3600) <= timedelta(seconds=60)
