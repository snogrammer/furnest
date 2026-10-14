"""Which code answers which Discord interaction.

The interactions endpoint verifies and records an interaction, then hands it to an
:class:`InteractionDispatcher` together with the open session. A handler is looked
up by interaction type and button action, because one button can need two: Deny
answers its click with a modal (an immediate response) and acts on the modal's
submit later.

A handler runs inside the request's transaction and has well under 3 seconds to
return the response body. It may change state in the database and enqueue slow work
for the worker, but it must never wait on the network.

The buttons' handlers are in :mod:`furnest.web.discord_actions`. An interaction on
one of our buttons with no handler registered for it changes nothing. It queues a
refresh of the message (:func:`~furnest.notify.sync.enqueue_sync`), in case the
message is stale, and is acknowledged with a deferred update. Anything else
(another bot's ``custom_id``, slash commands) gets a short reply only the user sees.
"""

from collections.abc import Callable

from sqlalchemy.orm import Session

from furnest.notify.custom_ids import DiscordAction
from furnest.notify.interactions import (
    Interaction,
    InteractionType,
    JsonObject,
    deferred_update,
    ephemeral,
)
from furnest.notify.sync import enqueue_sync

InteractionHandler = Callable[[Session, Interaction], JsonObject]

UNSUPPORTED = "This action isn't supported."
_ACTIONABLE = frozenset({InteractionType.MESSAGE_COMPONENT, InteractionType.MODAL_SUBMIT})


class InteractionDispatcher:
    def __init__(self) -> None:
        self._handlers: dict[tuple[InteractionType, DiscordAction], InteractionHandler] = {}

    def register(
        self, kind: InteractionType, action: DiscordAction, handler: InteractionHandler
    ) -> None:
        key = (kind, action)
        if key in self._handlers:
            msg = f"a handler for {kind.name} {action.value!r} is already registered"
            raise ValueError(msg)
        self._handlers[key] = handler

    def dispatch(self, session: Session, interaction: Interaction) -> JsonObject:
        """The response body for ``interaction``, after doing whatever it asks."""
        if (
            interaction.type not in _ACTIONABLE
            or interaction.action is None
            or interaction.property_id is None
        ):
            return ephemeral(UNSUPPORTED)
        handler = self._handlers.get((interaction.type, interaction.action))
        if handler is not None:
            return handler(session, interaction)
        if interaction.message_ephemeral:
            # Only the clicker sees it, so the bot can't edit it; nothing to refresh.
            return deferred_update()
        enqueue_sync(
            session,
            interaction.property_id,
            channel_id=interaction.channel_id,
            message_id=interaction.message_id,
        )
        return deferred_update()
