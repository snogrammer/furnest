"""Discord bookkeeping: interactions already handled, and batched match posts.

**Interactions.**
``POST /discord/interactions`` records each interaction id here in the same
transaction as whatever the interaction does. A request carrying an id that is
already present is acknowledged and otherwise ignored. Old rows are pruned as new
ones arrive (see :mod:`furnest.db.discord_store`).

**Batches.** New suggestions go out together in one list message
(:mod:`furnest.notify.post`). A :class:`DiscordBatch` remembers which properties a
list message shows, in display order, so a later edit (:mod:`furnest.notify.sync`)
can draw the same list with each property's current status.
"""

from datetime import datetime

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from furnest.db.base import Base, UTCDateTime, utcnow


class DiscordInteraction(Base):
    __tablename__ = "discord_interactions"

    # Discord's snowflake id for the interaction.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[int]
    custom_id: Mapped[str | None] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class DiscordBatch(Base):
    __tablename__ = "discord_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(32))
    message_id: Mapped[str] = mapped_column(String(32), unique=True)
    # The properties the message lists, in the order it lists them.
    property_ids: Mapped[list[int]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
