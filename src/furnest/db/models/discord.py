"""Discord interactions already handled, so a repeated delivery is a no-op.

``POST /discord/interactions`` records each interaction id here in the same
transaction as whatever the interaction does. A request carrying an id that is
already present is acknowledged and otherwise ignored. Old rows are pruned as new
ones arrive (see :mod:`furnest.db.discord_store`).
"""

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from furnest.db.base import Base, UTCDateTime, utcnow


class DiscordInteraction(Base):
    __tablename__ = "discord_interactions"

    # Discord's snowflake id for the interaction.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[int]
    custom_id: Mapped[str | None] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
