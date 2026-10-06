"""Keeping a match message in Discord in step with its property.

When someone acts on a property from Discord, the interactions endpoint changes the
state in the database and queues a :data:`MESSAGE_SYNC_JOB` (:func:`enqueue_sync`)
in the same transaction. The endpoint can't edit the message itself: it has to
answer within 3 seconds and must never wait on the network.

The worker runs :class:`MessageSync`. It renders the property's message again from
the database, with the buttons that no longer apply greyed out
(:func:`~furnest.notify.embeds.disabled_actions`), and edits it with the bot token.
The edit always renders the property's current state, so a late or repeated job is
harmless. The bot token is used rather than the interaction token, which expires
after 15 minutes, so a job the queue retries later still works.

* Discord not configured: the job logs and does nothing.
* Message deleted (404): logged and dropped, since there is nothing to edit.
* Rate limits and other Discord errors raise, and the queue retries the job.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker

from furnest.config import Settings
from furnest.db.property_store import SqlPropertyStore
from furnest.logging import get_logger
from furnest.notify.discord import DiscordClient, DiscordNotFoundError, discord_client
from furnest.notify.embeds import disabled_actions, match_message
from furnest.worker import queue

if TYPE_CHECKING:
    from furnest.worker.execution import JobContext

log = get_logger(__name__)

MESSAGE_SYNC_JOB = "discord.message_sync"


def enqueue_sync(
    session: Session, property_id: int, *, channel_id: str | None, message_id: str | None
) -> None:
    """Queue an edit of the message to match the property, unless one is already queued.

    Does nothing without a channel and message id, for example after a click that
    carried no message.
    """
    if channel_id is None or message_id is None:
        return
    if queue.pending(session, MESSAGE_SYNC_JOB, message_id=message_id) is not None:
        return
    queue.enqueue(
        session,
        MESSAGE_SYNC_JOB,
        {"property_id": property_id, "channel_id": channel_id, "message_id": message_id},
    )


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
        payload = match_message(
            detail,
            base_url=self._base_url,
            source_name=self._source_name,
            disabled=disabled_actions(detail.property),
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
