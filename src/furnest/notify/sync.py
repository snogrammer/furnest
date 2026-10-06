"""Keeping a match message in Discord in step with its property.

Whenever a posted property's status or snooze changes, from the dashboard, the API,
a Discord button or the system, the property store queues a
:data:`MESSAGE_SYNC_JOB` (:func:`enqueue_sync`) in the same transaction. Nothing
edits the message in-line: the interactions endpoint has to answer within 3 seconds,
and no request should wait on Discord.

The worker runs :class:`MessageSync`. It renders the property's message again from
the database, with the buttons that no longer apply greyed out
(:func:`~furnest.notify.embeds.disabled_actions`), and edits it with the bot token.
The edit always renders the property's current state, so a late or repeated job is
harmless. The bot token is used rather than the interaction token, which expires
after 15 minutes, so a job the queue retries later still works.

A message the property no longer points at was replaced by a repost (see
:mod:`furnest.notify.post`). It is drawn with every button greyed out and marked as
reposted, so only the newest message is live.

* Discord not configured: the job logs and does nothing.
* Message deleted (404): logged and dropped, since there is nothing to edit.
* Rate limits and other Discord errors raise, and the queue retries the job.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker

from furnest.config import Settings
from furnest.db.property_store import MESSAGE_SYNC_JOB, SqlPropertyStore, enqueue_sync
from furnest.logging import get_logger
from furnest.notify.custom_ids import DiscordAction
from furnest.notify.discord import DiscordClient, DiscordNotFoundError, discord_client
from furnest.notify.embeds import disabled_actions, match_message

if TYPE_CHECKING:
    from furnest.worker.execution import JobContext

log = get_logger(__name__)

__all__ = ["MESSAGE_SYNC_JOB", "MessageSync", "enqueue_sync"]


class MessageSync:
    """The :data:`MESSAGE_SYNC_JOB` handler.

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
    ) -> None:
        self._store = SqlPropertyStore(session_factory)
        self._base_url = str(settings.app.base_url)
        self._source_name = source_name
        self._client_factory = client_factory or (lambda: discord_client(settings))

    def __call__(self, ctx: JobContext) -> None:
        property_id = int(ctx.payload["property_id"])
        channel_id = str(ctx.payload["channel_id"])
        message_id = str(ctx.payload["message_id"])
        detail = self._store.detail(property_id)
        if detail is None:
            log.info("discord_sync_skipped", property_id=property_id, reason="no such property")
            return
        client = self._client_factory()
        if client is None:
            log.info("discord_sync_skipped", property_id=property_id, reason="not configured")
            return
        superseded = detail.property.discord_message_id not in (None, message_id)
        payload = match_message(
            detail,
            base_url=self._base_url,
            source_name=self._source_name,
            disabled=frozenset(DiscordAction) if superseded else disabled_actions(detail.property),
            superseded=superseded,
        )
        with client:
            try:
                client.edit_message(channel_id, message_id, payload)
            except DiscordNotFoundError:
                log.info("discord_sync_skipped", property_id=property_id, reason="message gone")
                return
        log.info(
            "discord_message_synced", property_id=property_id, status=detail.property.status.value
        )
