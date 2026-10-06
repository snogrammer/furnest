"""The DB-backed job queue on ``jobs``: web enqueues, the worker drains.

Lifecycle of a row::

    queued ──claim──▶ running ──complete──▶ succeeded
                         │
                         ├──fail──▶ failed (run_after = now + backoff) ──claim──▶ …
                         │     └─▶ dead (attempts exhausted)
                         └──requeue──▶ queued (shutdown interrupted it; attempt not counted)

``claim`` counts the attempt and leases the row to one worker until
``locked_until``, which covers the job's timeout. If that worker dies, the lease
runs out and another worker reclaims the row as if it had failed; the attempt still
counts, so a job that crashes the worker every time ends up ``dead``.

``complete`` and ``fail`` only touch a row the caller still holds. A worker whose
lease expired (and whose job was reclaimed elsewhere) can't overwrite the newer
outcome.
"""

import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.base import utcnow
from furnest.db.models import Job, JobStatus
from furnest.db.session import session_scope

# ``last_error`` keeps the tail of long tracebacks/messages, not the whole thing.
_MAX_ERROR_CHARS = 4000


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    run_after: datetime | None = None,
    max_attempts: int | None = None,
) -> Job:
    """Add a job inside the caller's transaction.

    Taking a session (not a factory) lets the web process enqueue in the same
    transaction as the state change that needs the job, so both commit or neither.
    """
    job = Job(kind=kind, payload=payload or {}, status=JobStatus.QUEUED)
    if run_after is not None:
        job.run_after = run_after
    if max_attempts is not None:
        job.max_attempts = max_attempts
    session.add(job)
    session.flush()
    return job


# A job in one of these states will still run (or is running).
_UNFINISHED = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.FAILED)


def pending(
    session: Session,
    kind: str,
    *,
    statuses: Iterable[JobStatus] = _UNFINISHED,
    **payload: str | int,
) -> Job | None:
    """The oldest job of ``kind`` in ``statuses`` (by default, any that hasn't finished
    yet) whose payload has these string or integer values, or ``None``. Lets the web
    process avoid queueing the same work twice.
    """
    statement = select(Job).where(
        Job.kind == kind,
        Job.status.in_(list(statuses)),
        *(
            (
                Job.payload[key].as_string()
                if isinstance(value, str)
                else Job.payload[key].as_integer()
            )
            == value
            for key, value in payload.items()
        ),
    )
    return session.scalars(statement.order_by(Job.id).limit(1)).first()


@dataclass(frozen=True)
class ClaimedJob:
    """A detached snapshot of a claimed row; the worker never holds ORM objects."""

    id: int
    kind: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    locked_until: datetime


def claim(
    factory: sessionmaker[Session],
    worker_id: str,
    lease: timedelta,
    *,
    now: datetime | None = None,
) -> ClaimedJob | None:
    """Lease the next due job to ``worker_id``, or ``None`` if nothing is due."""
    now = now or utcnow()
    due = or_(
        and_(Job.status.in_([JobStatus.QUEUED, JobStatus.FAILED]), Job.run_after <= now),
        # A worker died holding this one.
        and_(Job.status == JobStatus.RUNNING, Job.locked_until < now),
    )
    with session_scope(factory) as session:
        next_due = select(Job).where(due).order_by(Job.run_after, Job.id).limit(1)
        job = session.scalars(next_due).first()
        if job is None:
            return None
        job.status = JobStatus.RUNNING
        job.attempts += 1
        job.locked_by = worker_id
        job.locked_until = now + lease
        return ClaimedJob(
            id=job.id,
            kind=job.kind,
            payload=dict(job.payload),
            attempt=job.attempts,
            max_attempts=job.max_attempts,
            locked_until=job.locked_until,
        )


def complete(factory: sessionmaker[Session], job_id: int, worker_id: str) -> bool:
    """Mark a job we hold as succeeded. ``False`` if we no longer hold it."""
    with session_scope(factory) as session:
        job = _held(session, job_id, worker_id)
        if job is None:
            return False
        job.status = JobStatus.SUCCEEDED
        job.locked_by = None
        job.locked_until = None
        job.last_error = None
        return True


def fail(
    factory: sessionmaker[Session],
    job_id: int,
    worker_id: str,
    error: str,
    *,
    retry_in: timedelta,
    now: datetime | None = None,
) -> JobStatus | None:
    """Record a failed attempt on a job we hold.

    The job goes back to ``failed`` (due again after ``retry_in``) or, with no
    attempts left, to ``dead``. Returns the new status, or ``None`` if we no longer
    hold the job.
    """
    now = now or utcnow()
    with session_scope(factory) as session:
        job = _held(session, job_id, worker_id)
        if job is None:
            return None
        job.status = JobStatus.DEAD if job.attempts >= job.max_attempts else JobStatus.FAILED
        job.run_after = now + retry_in
        job.locked_by = None
        job.locked_until = None
        job.last_error = error[-_MAX_ERROR_CHARS:]
        return job.status


def requeue(
    factory: sessionmaker[Session],
    job_id: int,
    worker_id: str,
    reason: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Hand back a job we hold without counting the attempt (the worker is shutting down).

    It is due again at once, so the next worker picks it up straight away.
    """
    with session_scope(factory) as session:
        job = _held(session, job_id, worker_id)
        if job is None:
            return False
        job.status = JobStatus.QUEUED
        job.attempts -= 1
        job.run_after = now or utcnow()
        job.locked_by = None
        job.locked_until = None
        job.last_error = reason
        return True


def _held(session: Session, job_id: int, worker_id: str) -> Job | None:
    job = session.get(Job, job_id)
    if job is None or job.status != JobStatus.RUNNING or job.locked_by != worker_id:
        return None
    return job


def backoff(
    attempt: int,
    *,
    base: float,
    cap: float,
    rng: Callable[[], float] = random.random,
) -> timedelta:
    """Delay before retrying after failed attempt number ``attempt`` (1-based).

    Exponential, ``base * 2^(attempt-1)`` capped at ``cap``, then jittered down by up
    to half so jobs that failed together (say, an SMTP outage) don't retry in lockstep.
    """
    delay = min(cap, base * 2 ** (attempt - 1))
    return timedelta(seconds=delay * (1 - rng() / 2))
