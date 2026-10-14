"""Keeping the Discord messages a property is on in step with it.

Whenever a posted property's status or snooze changes, from the dashboard, the API,
a Discord button or the system, the property store queues a
:data:`MESSAGE_SYNC_JOB` (:func:`enqueue_sync`) in the same transaction. Nothing
edits the message in-line: the interactions endpoint has to answer within 3 seconds,
and no request should wait on Discord.

The worker runs :class:`MessageSync`. A list message (one batched post of new
suggestions, see :mod:`furnest.notify.post`) is drawn again from the properties it
lists, each with its current status in front, and edited with the bot token. A
property that a later list showed again is marked as reposted. Several changes to
properties on one list share one queued job, since jobs are told apart by message.

A card posted on its own (before batching) is handled as it always was.
:class:`MessageSync` renders the property's card again from the database, with the
buttons that no longer apply greyed out
(:func:`~furnest.notify.embeds.disabled_actions`), and edits it with the bot token.
The edit always renders the property's current state, so a late or repeated job is
harmless. The bot token is used rather than the interaction token, which expires
after 15 minutes, so a job the queue retries later still works.

A message the property no longer points at was replaced by a repost (see
:mod:`furnest.notify.post`). It is drawn with every button greyed out and marked as
reposted, so only the newest message is live. A property grouping merged into
another is drawn the same way, marked as merged, with its link leading to the
survivor.

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
from furnest.notify.embeds import batch_message, disabled_actions, match_message

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
        listed = self._store.batch_property_ids(message_id)
        if listed is not None:
            self._sync_list(channel_id, message_id, listed)
            return
        detail = self._store.detail(property_id)
        if detail is None:
            log.info("discord_sync_skipped", property_id=property_id, reason="no such property")
            return
        client = self._client_factory()
        if client is None:
            log.info("discord_sync_skipped", property_id=property_id, reason="not configured")
            return
        superseded = detail.property.discord_message_id not in (None, message_id)
        inactive = superseded or detail.property.merged_into_id is not None
        payload = match_message(
            detail,
            base_url=self._base_url,
            source_name=self._source_name,
            disabled=frozenset(DiscordAction) if inactive else disabled_actions(detail.property),
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

    def _sync_list(self, channel_id: str, message_id: str, property_ids: list[int]) -> None:
        details = [d for d in map(self._store.detail, property_ids) if d is not None]
        client = self._client_factory()
        if client is None:
            log.info("discord_sync_skipped", message_id=message_id, reason="not configured")
            return
        superseded = {d.property.id for d in details if d.property.discord_message_id != message_id}
        payload = batch_message(details, base_url=self._base_url, superseded=superseded)
        with client:
            try:
                client.edit_message(channel_id, message_id, payload)
            except DiscordNotFoundError:
                log.info("discord_sync_skipped", message_id=message_id, reason="message gone")
                return
        log.info("discord_list_synced", message_id=message_id, count=len(details))
