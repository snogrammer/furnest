"""The worker's heartbeat row, which ``/readyz`` reads (see :mod:`furnest.web.health`).

Each worker process upserts one ``worker_heartbeat`` row keyed by a per-process id,
so a restarted worker never resurrects its predecessor's row. Rows from processes
that are long gone are pruned when a worker starts.
"""

import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from furnest import __version__
from furnest.db.base import utcnow
from furnest.db.models import WorkerHeartbeat
from furnest.db.session import session_scope


def _new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class WorkerIdentity:
    """Who this worker is; ``worker_id`` also names it as a lease owner."""

    worker_id: str = field(default_factory=_new_worker_id)
    hostname: str = field(default_factory=socket.gethostname)
    pid: int = field(default_factory=os.getpid)
    version: str = __version__
    started_at: datetime = field(default_factory=utcnow)


def beat(
    factory: sessionmaker[Session], identity: WorkerIdentity, *, now: datetime | None = None
) -> None:
    with session_scope(factory) as session:
        session.merge(
            WorkerHeartbeat(
                worker_id=identity.worker_id,
                hostname=identity.hostname,
                pid=identity.pid,
                version=identity.version,
                started_at=identity.started_at,
                last_seen_at=now or utcnow(),
            )
        )


def prune(
    factory: sessionmaker[Session], older_than: timedelta, *, now: datetime | None = None
) -> int:
    """Delete heartbeats not seen for ``older_than``; returns how many went."""
    cutoff = (now or utcnow()) - older_than
    with session_scope(factory) as session:
        result = session.execute(
            delete(WorkerHeartbeat).where(WorkerHeartbeat.last_seen_at < cutoff)
        )
        return int(result.rowcount)  # type: ignore[attr-defined]
