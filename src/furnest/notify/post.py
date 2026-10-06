"""Posting a suggested property to Discord.

A passing match moves a property to ``suggested`` and queues a
:data:`~furnest.db.property_store.NOTIFY` job; so does a snooze running out on a
property that is still waiting on a decision. The worker runs :class:`NotifyPost`.
It draws the property's match message from the database, posts it to
``DISCORD_CHANNEL_ID`` with the bot token, and saves the channel and message id on
the property (:meth:`~furnest.db.property_store.SqlPropertyStore.attach_message`).
From then on every change to the property queues a message sync
(:mod:`furnest.notify.sync`).

Re-notifying a property that already has a message posts a new one, so the channel
sees it again. The old message is then synced once more, which greys out its
buttons and marks it as reposted.

* Discord not configured, the property gone, no longer ``suggested`` or snoozed
  again: the job logs and does nothing.
* Rate limits and other Discord errors raise, and the queue retries the job.
* If the worker dies after Discord accepted the post but before the ids are saved,
  the retry posts the property a second time. That is rare and harmless enough
  that it isn't worth a two-phase handshake.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker

from furnest.config import Settings
from furnest.db.base import utcnow
from furnest.db.property_store import NOTIFY, SqlPropertyStore
from furnest.domain import PropertyStatus
from furnest.logging import get_logger
from furnest.notify.discord import DiscordClient, discord_client
from furnest.notify.embeds import disabled_actions, match_message

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
        source_name: Callable[[str], str] = str,
        client_factory: Callable[[], DiscordClient | None] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = SqlPropertyStore(session_factory)
        self._base_url = str(settings.app.base_url)
        self._channel_id = settings.discord.channel_id
        self._source_name = source_name
        self._client_factory = client_factory or (lambda: discord_client(settings))
        self._clock = clock

    def __call__(self, ctx: JobContext) -> None:
        property_id = int(ctx.payload["property_id"])
        detail = self._store.detail(property_id)
        if detail is None:
            self._skip(property_id, "no such property")
            return
        state = detail.property
        if state.status is not PropertyStatus.SUGGESTED:
            self._skip(property_id, f"it is {state.status}")
            return
        if state.snoozed_until is not None and state.snoozed_until > self._clock():
            self._skip(property_id, "snoozed")
            return
        client = self._client_factory()
        if client is None or self._channel_id is None:
            self._skip(property_id, "not configured")
            return
        payload = match_message(
            detail,
            base_url=self._base_url,
            source_name=self._source_name,
            disabled=disabled_actions(state),
        )
        with client:
            message = client.create_message(self._channel_id, payload)
        self._store.attach_message(
            property_id, channel_id=message.channel_id, message_id=message.id, rendered=state
        )
        log.info(
            "discord_match_posted",
            property_id=property_id,
            message_id=message.id,
            reposted=state.discord_message_id is not None,
        )

    @staticmethod
    def _skip(property_id: int, reason: str) -> None:
        log.info("notify_skipped", property_id=property_id, reason=reason)
