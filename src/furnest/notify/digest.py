"""The daily digest: one channel message summing up where the properties stand.

With ``DISCORD_DIGEST_TIME`` set, :func:`schedule_digest` gives the worker a daily
job at that time in ``TIMEZONE``. That job (:class:`QueueDigest`) only queues a
:data:`DIGEST_JOB` for the local date, and only if no job for that date exists in
any state. That makes the digest at most once a day even with several workers or a
restart around the scheduled time, while the queue's retries cover Discord being
down. A day the worker isn't running at that time gets no digest.

:class:`DigestPost` runs the queued job. It reads the property count in each
status (snoozed ones included) and the best-scored properties that became
``suggested`` in the last :data:`DIGEST_WINDOW` and are still waiting, then posts
:func:`digest_message`.

* Discord not configured: the job logs and does nothing.
* Rate limits and other Discord errors raise, and the queue retries the job.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.orm import Session, sessionmaker

from furnest.config import Settings
from furnest.db.base import utcnow
from furnest.db.models import JobStatus
from furnest.db.property_store import PropertyRow, PropertySort, SqlPropertyStore
from furnest.db.session import session_scope
from furnest.domain import PropertyStatus
from furnest.logging import get_logger
from furnest.notify.discord import DiscordClient, discord_client
from furnest.notify.embeds import (
    EMBED_COLOR,
    JsonObject,
    dashboard_url,
    escape_markdown,
    truncate,
)
from furnest.worker import queue

if TYPE_CHECKING:
    from furnest.worker.execution import Handler, JobContext

__all__ = [
    "DIGEST_JOB",
    "DIGEST_SCHEDULE",
    "DigestPost",
    "QueueDigest",
    "digest_message",
    "schedule_digest",
]

log = get_logger(__name__)

# Queue job kind; its payload is ``{"date": "<local ISO date>"}``.
DIGEST_JOB = "discord.digest"
# Name of the daily scheduled job that queues it.
DIGEST_SCHEDULE = "discord_digest"
# How far back a suggestion counts as new.
DIGEST_WINDOW = timedelta(hours=24)
# Title of a suggestion line; the line also holds score, price and a link.
_MAX_TITLE = 120


class DailyScheduler(Protocol):
    """The part of :class:`furnest.worker.runtime.Worker` this module needs."""

    def schedule_daily(self, name: str, handler: Handler, *, at: time, tz: tzinfo) -> None: ...


def schedule_digest(
    worker: DailyScheduler,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> bool:
    """Schedule the daily digest if ``DISCORD_DIGEST_TIME`` is set; returns whether it did."""
    at = settings.discord.digest_time
    if at is None:
        return False
    tz = settings.app.tzinfo
    worker.schedule_daily(DIGEST_SCHEDULE, QueueDigest(session_factory, tz=tz), at=at, tz=tz)
    log.info("digest_scheduled", at=at.isoformat(), timezone=settings.app.timezone)
    return True


class QueueDigest:
    """The daily scheduled job: queue today's :data:`DIGEST_JOB` unless it exists."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        tz: tzinfo,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._factory = session_factory
        self._tz = tz
        self._clock = clock

    def __call__(self, _ctx: JobContext) -> None:
        day = self._clock().astimezone(self._tz).date().isoformat()
        with session_scope(self._factory) as session:
            if queue.pending(session, DIGEST_JOB, statuses=list(JobStatus), date=day) is not None:
                log.info("digest_skipped", date=day, reason="already queued")
                return
            queue.enqueue(session, DIGEST_JOB, {"date": day})
        log.info("digest_queued", date=day)


class DigestPost:
    """The :data:`DIGEST_JOB` handler.

    ``client_factory`` makes a :class:`DiscordClient` for each job, or returns
    ``None`` when Discord isn't configured. Tests pass one with a mock transport.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        client_factory: Callable[[], DiscordClient | None] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = SqlPropertyStore(session_factory)
        self._base_url = str(settings.app.base_url)
        self._channel_id = settings.discord.channel_id
        self._top = settings.discord.digest_top
        self._client_factory = client_factory or (lambda: discord_client(settings))
        self._clock = clock

    def __call__(self, ctx: JobContext) -> None:
        day = date.fromisoformat(str(ctx.payload["date"]))
        client = self._client_factory()
        if client is None or self._channel_id is None:
            log.info("digest_skipped", date=day.isoformat(), reason="not configured")
            return
        now = self._clock()
        counts = self._store.status_counts(now=now, include_snoozed=True)
        new = self._store.search(
            now=now,
            limit=self._top,
            statuses=[PropertyStatus.SUGGESTED],
            sort=PropertySort.SCORE,
            changed_since=now - DIGEST_WINDOW,
        )
        payload = digest_message(
            day, counts, new.rows, new_total=new.total, base_url=self._base_url
        )
        with client:
            message = client.create_message(self._channel_id, payload)
        log.info("digest_posted", date=day.isoformat(), message_id=message.id, new=new.total)


def digest_message(
    day: date,
    counts: Mapping[PropertyStatus, int],
    new: Sequence[PropertyRow],
    *,
    new_total: int,
    base_url: str,
) -> JsonObject:
    """The create-message payload for ``day``'s digest.

    ``counts`` has the properties in each status; statuses with none are left out.
    ``new`` are the top new suggestions, best first, out of ``new_total``.
    """
    fields: list[JsonObject] = [
        {"name": _status_text(status), "value": str(counts[status]), "inline": True}
        for status in PropertyStatus
        if counts.get(status)
    ]
    if new:
        lines = [_suggestion_line(row, base_url) for row in new]
        if new_total > len(new):
            lines.append(f"…and {new_total - len(new)} more")
        name = f"New suggestions ({new_total})"
        value = "\n".join(lines)
    else:
        name, value = "New suggestions", "No new suggestions in the last day."
    fields.append({"name": name, "value": truncate(value, "field_value"), "inline": False})
    embed: JsonObject = {
        "title": f"Daily digest · {day.isoformat()}",
        "url": base_url.rstrip("/") + "/",
        "color": EMBED_COLOR,
        "fields": fields,
        "footer": {"text": f"furnest · {sum(counts.values())} properties"},
    }
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def _suggestion_line(row: PropertyRow, base_url: str) -> str:
    """``• [Title](dashboard) · 87/100 · $1,800/mo`` for one suggestion."""
    state, listing = row.property, row.listing
    title = listing.title if listing is not None else None
    label = escape_markdown(truncate(title or state.address or f"Property #{state.id}", _MAX_TITLE))
    parts = [f"• [{label}]({dashboard_url(base_url, state.id)})"]
    if state.score is not None:
        parts.append(f"{state.score}/100")
    if listing is not None and listing.monthly_price is not None:
        parts.append(f"{listing.monthly_price.format()}/mo")
    return " · ".join(parts)


def _status_text(status: PropertyStatus) -> str:
    return status.value.replace("_", " ").capitalize()
