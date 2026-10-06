"""What the Approve, Deny, Inquire and Snooze buttons on a match message do.

:func:`register_buttons` adds one handler per (interaction type, button) to the
:class:`~furnest.web.discord_dispatch.InteractionDispatcher`. Each handler runs in
the interactions endpoint's transaction, next to the record of the interaction, so
the state change and that record commit together or not at all. A Discord retry
of the same interaction id is acknowledged without running the handler again.

* **Approve** moves a suggested or denied property to ``approved``.
* **Deny** answers the click with a modal asking for an optional reason. The modal's
  submit denies the property and saves the reason as a note
  (:func:`~furnest.db.property_store.deny_in`).
* **Inquire** approves a ``suggested`` property first, then queues an
  :data:`~furnest.email.inquiry.INQUIRY_CREATE` job, unless one is already queued
  for it. That job drafts the email and respects ``INQUIRY_MODE``.
* **Snooze** hides the property for ``PIPELINE_SNOOZE_HOURS``.

Every change is audited with the Discord actor and the clicking user's name and id.
After an action, the handler queues a message sync
(:func:`~furnest.notify.sync.enqueue_sync`) and acknowledges with a deferred update.
The worker then edits the message to show the new state and grey out buttons that
no longer apply.

An action the property can't take (approving a property that is already approved,
after a click on a stale message) gets a reply only the clicker sees. The message is
synced anyway, since the click shows it was out of date. Such errors are answered,
not raised, so the transaction still commits the interaction record.
"""

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from furnest.config import Settings
from furnest.db.base import utcnow
from furnest.db.models import Property
from furnest.db.property_store import (
    PropertyNotFoundError,
    SnoozeError,
    deny_in,
    snooze_in,
    transition_in,
)
from furnest.domain import Actor, InvalidTransitionError, PropertyStatus, allowed_targets
from furnest.email.inquiry import INQUIRY_CREATE
from furnest.notify.custom_ids import DiscordAction, encode_custom_id
from furnest.notify.embeds import APPROVABLE, INQUIRABLE
from furnest.notify.interactions import (
    Interaction,
    InteractionType,
    JsonObject,
    deferred_update,
    ephemeral,
    modal,
    paragraph_input,
)
from furnest.notify.sync import enqueue_sync
from furnest.web.discord_dispatch import InteractionDispatcher
from furnest.worker import queue

# The Deny modal's one text input.
REASON_INPUT = "reason"
NOT_FOUND = "That property no longer exists."


class ButtonActions:
    def __init__(self, settings: Settings, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._snooze = timedelta(hours=settings.pipeline.snooze_hours)
        self._clock = clock

    def approve(self, session: Session, interaction: Interaction) -> JsonObject:
        property_id = _property_id(interaction)
        row = session.get(Property, property_id)
        if row is None:
            return ephemeral(NOT_FOUND)
        if row.status not in APPROVABLE:
            return _rejected(session, interaction, f"Can't approve this property: it is {_s(row)}.")
        # The state machine lets the Discord actor approve every APPROVABLE status.
        transition_in(
            session,
            property_id,
            PropertyStatus.APPROVED,
            actor=Actor.DISCORD,
            at=self._clock(),
            detail=interaction.actor_detail,
        )
        return _synced(session, interaction)

    def deny_prompt(self, session: Session, interaction: Interaction) -> JsonObject:
        """Open the reason modal, if the property can still be denied."""
        property_id = _property_id(interaction)
        row = session.get(Property, property_id)
        if row is None:
            return ephemeral(NOT_FOUND)
        if PropertyStatus.DENIED not in allowed_targets(row.status, Actor.DISCORD):
            return _rejected(session, interaction, f"Can't deny this property: it is {_s(row)}.")
        return deny_modal(property_id)

    def deny(self, session: Session, interaction: Interaction) -> JsonObject:
        try:
            deny_in(
                session,
                _property_id(interaction),
                actor=Actor.DISCORD,
                at=self._clock(),
                reason=interaction.fields.get(REASON_INPUT),
                detail=interaction.actor_detail,
            )
        except PropertyNotFoundError:
            return ephemeral(NOT_FOUND)
        except InvalidTransitionError as e:
            return _rejected(session, interaction, f"Can't deny this property: {e.reason}.")
        except ValueError as e:  # a reason longer than a note may be
            return ephemeral(f"Can't deny this property: {e}.")
        return _synced(session, interaction)

    def inquire(self, session: Session, interaction: Interaction) -> JsonObject:
        property_id = _property_id(interaction)
        row = session.get(Property, property_id)
        if row is None:
            return ephemeral(NOT_FOUND)
        if row.status not in INQUIRABLE:
            return _rejected(
                session, interaction, f"Can't send an inquiry for this property: it is {_s(row)}."
            )
        if row.status is PropertyStatus.SUGGESTED:
            transition_in(
                session,
                property_id,
                PropertyStatus.APPROVED,
                actor=Actor.DISCORD,
                at=self._clock(),
                detail=interaction.actor_detail,
            )
        if queue.pending(session, INQUIRY_CREATE, property_id=property_id) is None:
            queue.enqueue(
                session,
                INQUIRY_CREATE,
                {
                    "property_id": property_id,
                    "actor": Actor.DISCORD.value,
                    "actor_detail": interaction.actor_detail,
                },
            )
        return _synced(session, interaction)

    def snooze(self, session: Session, interaction: Interaction) -> JsonObject:
        now = self._clock()
        try:
            snooze_in(
                session,
                _property_id(interaction),
                until=now + self._snooze,
                actor=Actor.DISCORD,
                at=now,
                detail=interaction.actor_detail,
            )
        except PropertyNotFoundError:
            return ephemeral(NOT_FOUND)
        except SnoozeError as e:
            return _rejected(session, interaction, f"Can't snooze this property: {e.reason}.")
        return _synced(session, interaction)


def register_buttons(
    dispatcher: InteractionDispatcher,
    settings: Settings,
    *,
    clock: Callable[[], datetime] = utcnow,
) -> None:
    actions = ButtonActions(settings, clock=clock)
    component, submit = InteractionType.MESSAGE_COMPONENT, InteractionType.MODAL_SUBMIT
    dispatcher.register(component, DiscordAction.APPROVE, actions.approve)
    dispatcher.register(component, DiscordAction.DENY, actions.deny_prompt)
    dispatcher.register(submit, DiscordAction.DENY, actions.deny)
    dispatcher.register(component, DiscordAction.INQUIRE, actions.inquire)
    dispatcher.register(component, DiscordAction.SNOOZE, actions.snooze)


def deny_modal(property_id: int) -> JsonObject:
    """The Deny button's modal. Its submit carries the same ``custom_id`` as the button."""
    return modal(
        encode_custom_id(DiscordAction.DENY, property_id),
        "Deny property",
        [
            paragraph_input(
                REASON_INPUT,
                "Reason (optional, saved as a note)",
                placeholder="Too far from work, no parking, …",
            )
        ],
    )


def _property_id(interaction: Interaction) -> int:
    # The dispatcher only hands over interactions whose custom_id named a property.
    if interaction.property_id is None:
        msg = "interaction has no property id"
        raise ValueError(msg)
    return interaction.property_id


def _s(row: Property) -> str:
    return row.status.value.replace("_", " ")


def _synced(session: Session, interaction: Interaction) -> JsonObject:
    enqueue_sync(
        session,
        _property_id(interaction),
        channel_id=interaction.channel_id,
        message_id=interaction.message_id,
    )
    return deferred_update()


def _rejected(session: Session, interaction: Interaction, message: str) -> JsonObject:
    """Tell the clicker why nothing happened, and refresh the stale message."""
    _synced(session, interaction)
    return ephemeral(message)
