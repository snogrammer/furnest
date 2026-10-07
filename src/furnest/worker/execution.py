"""Running one job with a timeout, and the handler registry for queued jobs.

Python can't kill a thread, so a timeout can't stop a job by force. Each run gets
its own daemon thread, and the caller stops *waiting* when the timeout passes. The
job is then told to stop through ``JobContext.cancelled`` and is expected to notice
(check it between steps, pass ``deadline`` to network calls). Because the thread is a
daemon, a job that never notices still can't keep the process alive on shutdown.

What follows for handlers:

* They must be **idempotent**. A timed-out or interrupted run is retried, possibly
  while a stuck earlier run is still finishing.
* They should keep their own I/O timeouts below the job timeout.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session, sessionmaker

from furnest.db.base import utcnow

if TYPE_CHECKING:
    from furnest.config import Settings
    from furnest.pipeline.properties import PropertyPipeline


class JobTimeoutError(Exception):
    """A job ran past its timeout; reported to Sentry like any other failure."""


@dataclass(frozen=True)
class JobContext:
    job_id: int | str
    kind: str
    payload: dict[str, Any]
    attempt: int
    deadline: datetime
    # Attempts the queue allows; ``attempt == max_attempts`` is the last try.
    max_attempts: int = 1
    cancelled: threading.Event = field(default_factory=threading.Event)


Handler = Callable[[JobContext], None]


class OutcomeKind(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMED_OUT = "timed_out"
    ABANDONED = "abandoned"  # the worker gave up waiting during shutdown


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    error: BaseException | None = None


# How often a waiting caller checks whether it should stop waiting (shutdown).
_POLL_SECONDS = 0.1


def run_with_timeout(
    handler: Handler,
    ctx: JobContext,
    *,
    abandon: threading.Event,
    on_finish: Callable[[], None] | None = None,
) -> Outcome:
    """Run ``handler(ctx)`` in a daemon thread and wait for it until ``ctx.deadline``.

    ``on_finish`` runs in the job's thread once the handler really returns, even
    after a timeout. Lease release goes there, so a stuck run keeps its lease.
    """
    done = threading.Event()
    errors: list[BaseException] = []

    def target() -> None:
        try:
            handler(ctx)
        except BaseException as exc:
            errors.append(exc)
        finally:
            try:
                if on_finish is not None:
                    on_finish()
            finally:
                done.set()

    thread = threading.Thread(target=target, name=f"job-{ctx.kind}-{ctx.job_id}", daemon=True)
    thread.start()
    while not done.wait(_POLL_SECONDS):
        if abandon.is_set():
            ctx.cancelled.set()
            return Outcome(OutcomeKind.ABANDONED)
        if utcnow() >= ctx.deadline:
            ctx.cancelled.set()
            return Outcome(OutcomeKind.TIMED_OUT)
    if errors:
        return Outcome(OutcomeKind.ERROR, errors[0])
    return Outcome(OutcomeKind.OK)


@dataclass(frozen=True)
class Registration:
    handler: Handler
    timeout: timedelta | None


class JobRegistry:
    """Maps a queued job's ``kind`` to the function that runs it."""

    def __init__(self) -> None:
        self._handlers: dict[str, Registration] = {}

    def register(self, kind: str, handler: Handler, *, timeout: timedelta | None = None) -> None:
        if kind in self._handlers:
            msg = f"a handler for job kind {kind!r} is already registered"
            raise ValueError(msg)
        self._handlers[kind] = Registration(handler, timeout)

    def get(self, kind: str) -> Registration | None:
        return self._handlers.get(kind)

    def longest_timeout(self, default: timedelta) -> timedelta:
        """The longest any job can run, which bounds how long a claim must lease it."""
        return max([default, *(r.timeout for r in self._handlers.values() if r.timeout)])


def default_registry(
    session_factory: sessionmaker[Session],
    pipeline: "PropertyPipeline | None" = None,
    *,
    settings: "Settings | None" = None,
    source_name: Callable[[str], str] = str,
) -> JobRegistry:
    """Handlers the worker ships with. Later issues register theirs here.

    ``settings`` configures the Discord poster, message sync, alerts, digest, inquiry
    drafting and email sender; without them it reads the environment. ``source_name``
    turns an adapter key into its display name.
    """
    from furnest.config import Settings
    from furnest.db.listing_store import SqlListingStore
    from furnest.email.inquiry import INQUIRY_CREATE, InquiryCreate
    from furnest.email.send import EMAIL_SEND, EmailSend
    from furnest.notify.alerts import SOURCE_ALERT_JOB, SourceAlertPost
    from furnest.notify.digest import DIGEST_JOB, DigestPost
    from furnest.notify.post import NotifyPost
    from furnest.notify.sync import MESSAGE_SYNC_JOB, MessageSync
    from furnest.pipeline import properties
    from furnest.pipeline.rerun import RERUN_CRITERIA, RerunCriteria

    settings = settings or Settings()
    pipeline = pipeline or properties.PropertyPipeline(session_factory, grouping=settings.grouping)
    registry = JobRegistry()
    listings = SqlListingStore(session_factory)
    rerun = RerunCriteria(
        session_factory, listings=listings.active, by_id=listings.many, record=pipeline.record
    )
    registry.register(RERUN_CRITERIA, rerun)
    registry.register(
        properties.NOTIFY,
        NotifyPost(session_factory, settings=settings, source_name=source_name),
    )
    registry.register(
        MESSAGE_SYNC_JOB,
        MessageSync(session_factory, settings=settings, source_name=source_name),
    )
    registry.register(SOURCE_ALERT_JOB, SourceAlertPost(settings=settings, source_name=source_name))
    registry.register(DIGEST_JOB, DigestPost(session_factory, settings=settings))
    registry.register(INQUIRY_CREATE, InquiryCreate(session_factory, settings=settings))
    registry.register(EMAIL_SEND, EmailSend(session_factory, settings=settings))
    return registry
