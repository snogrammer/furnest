"""Posting new suggestions to Discord, together in one list message.

A passing match moves a property to ``suggested``. The property store then marks it
(``notify_requested_at``) and queues a :data:`~furnest.db.property_store.NOTIFY`
job unless one is already waiting. A snooze running out on a property still waiting
on a decision does the same, and so does grouping for a merged survivor that was
never posted.

The worker runs :class:`NotifyPost`. The job doesn't post at once: it waits until
the first mark is ``DISCORD_NOTIFY_BATCH_MINUTES`` old, by queueing itself again for
that time, so a whole data pull (every source's run) lands in one message. Then it
posts every marked property that should still go out
(:meth:`~furnest.db.property_store.SqlPropertyStore.notify_candidates`) as one
:func:`~furnest.notify.embeds.batch_message`, best score first, to
``DISCORD_CHANNEL_ID`` with the bot token. More than
:data:`~furnest.notify.embeds.MAX_BATCH` properties take more than one message.
Each property then points at its list message
(:meth:`~furnest.db.property_store.SqlPropertyStore.attach_batch`), and from then
on every change to it queues a sync of the list (:mod:`furnest.notify.sync`).

Notifying a property that already has a message (after a snooze) lists it again in
the new message. The old message is synced once more, which marks the property
there as reposted.

* Discord not configured, or nothing left to post: the job logs and does nothing.
  Marks stay, so the next notify job posts them once Discord is set up.
* Rate limits and other Discord errors raise, and the queue retries the job.
* If the worker dies after Discord accepted a post but before the ids are saved,
  the retry posts those properties a second time. That is rare and harmless enough
  that it isn't worth a two-phase handshake.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker

from furnest.config import Settings
from furnest.db.base import utcnow
from furnest.db.property_store import NOTIFY, SqlPropertyStore
from furnest.logging import get_logger
from furnest.notify.discord import DiscordClient, discord_client
from furnest.notify.embeds import MAX_BATCH, batch_message

if TYPE_CHECKING:
    from furnest.worker.execution import JobContext

__all__ = ["NOTIFY", "NotifyPost"]

log = get_logger(__name__)


class NotifyPost:
    """The :data:`~furnest.db.property_store.NOTIFY` handler.

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
        self._window = timedelta(minutes=settings.discord.notify_batch_minutes)
        self._client_factory = client_factory or (lambda: discord_client(settings))
        self._clock = clock

    def __call__(self, ctx: JobContext) -> None:
        # Jobs queued before batching carry a property id; every job posts the batch.
        now = self._clock()
        first = self._store.first_notify_request()
        if first is None:
            self._skip("nothing marked")
            return
        due = first + self._window
        if due > now:
            self._store.defer_notify(until=due)
            log.info("notify_deferred", until=due.isoformat())
            return
        client = self._client_factory()
        if client is None or self._channel_id is None:
            self._skip("not configured")
            return
        candidates = self._store.notify_candidates(now=now)
        if not candidates:
            self._skip("nothing to post")
            return
        with client:
            for start in range(0, len(candidates), MAX_BATCH):
                chunk = candidates[start : start + MAX_BATCH]
                payload = batch_message(chunk, base_url=self._base_url, now=now)
                message = client.create_message(self._channel_id, payload)
                self._store.attach_batch(
                    channel_id=message.channel_id,
                    message_id=message.id,
                    rendered=[detail.property for detail in chunk],
                )
                log.info(
                    "discord_batch_posted",
                    message_id=message.id,
                    count=len(chunk),
                    reposted=sum(d.property.discord_message_id is not None for d in chunk),
                )

    @staticmethod
    def _skip(reason: str) -> None:
        log.info("notify_skipped", reason=reason)
