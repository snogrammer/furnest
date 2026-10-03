"""Lease locks on ``job_locks``: a named job never runs concurrently with itself.

A lease is a row ``(name, owner, expires_at)``. Whoever holds an unexpired row owns
the name; an expired row is free for anyone to take over, so a worker that dies
mid-run blocks the job only until its lease runs out. Pick a TTL longer than the
job's timeout.

Every function runs in one ``session_scope``, and every transaction starts with
``BEGIN IMMEDIATE`` (see :mod:`furnest.db.engine`), so the read-then-write in
``acquire`` holds the SQLite write lock throughout. Two processes can't both see a
free lease and both take it.
"""

from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.base import utcnow
from furnest.db.models import JobLock
from furnest.db.session import session_scope


def acquire(
    factory: sessionmaker[Session],
    name: str,
    owner: str,
    ttl: timedelta,
    *,
    now: datetime | None = None,
) -> bool:
    """Take or refresh the lease on ``name``. ``False`` if someone else holds it."""
    now = now or utcnow()
    with session_scope(factory) as session:
        lock = session.get(JobLock, name)
        if lock is None:
            session.add(JobLock(name=name, owner=owner, acquired_at=now, expires_at=now + ttl))
            return True
        if lock.owner != owner and lock.expires_at > now:
            return False
        if lock.owner != owner:
            lock.owner = owner
            lock.acquired_at = now
        lock.expires_at = now + ttl
        return True


def renew(
    factory: sessionmaker[Session],
    name: str,
    owner: str,
    ttl: timedelta,
    *,
    now: datetime | None = None,
) -> bool:
    """Extend a lease we still hold. ``False`` if it was lost (expired and taken over)."""
    now = now or utcnow()
    with session_scope(factory) as session:
        lock = session.get(JobLock, name)
        if lock is None or lock.owner != owner:
            return False
        lock.expires_at = now + ttl
        return True


def release(factory: sessionmaker[Session], name: str, owner: str) -> None:
    """Drop our lease on ``name``; a lease someone else took over is left alone."""
    with session_scope(factory) as session:
        session.execute(delete(JobLock).where(JobLock.name == name, JobLock.owner == owner))
