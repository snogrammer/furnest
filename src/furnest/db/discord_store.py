"""Idempotency for Discord interactions (the ``discord_interactions`` table).

:func:`record_interaction` runs inside the request's transaction, together with the
work the interaction triggers. If that work fails, the record rolls back with it,
so Discord's retry is handled, not swallowed.

The endpoint rejects any interaction whose signed timestamp is more than
``DISCORD_INTERACTION_MAX_AGE_SECONDS`` (minutes) old, so a replay can only use an id
seen recently. Keeping a day of ids is therefore plenty. Older rows are deleted as
new ones are recorded, which keeps the table small without a scheduled job.
"""

from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from furnest.db.models import DiscordInteraction
from furnest.notify.interactions import Interaction

RETENTION = timedelta(days=1)


def record_interaction(session: Session, interaction: Interaction, *, now: datetime) -> bool:
    """Record ``interaction`` as handled. ``False`` if it already was (a replay)."""
    session.execute(
        delete(DiscordInteraction).where(DiscordInteraction.received_at < now - RETENTION)
    )
    row = DiscordInteraction(
        id=interaction.id,
        type=int(interaction.type),
        custom_id=interaction.custom_id,
        received_at=now,
    )
    try:
        # A savepoint, so a duplicate id leaves the caller's transaction usable.
        with session.begin_nested():
            session.add(row)
    except IntegrityError:
        return False
    return True
