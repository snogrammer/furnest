"""Per-adapter state and health; the logic is in ``furnest.db.source_store``.

One row per registered adapter, keyed by its name. ``enabled`` is the dashboard
toggle: NULL means "never toggled", so the env config or the adapter's default
decides (see ``furnest.sources.config``). ``overrides`` holds the settings the
dashboard saved for the adapter (search locations, interval, max results), as JSON
that ``furnest.config.SourceOverrides`` validates on read; NULL means none.
``disabled_reason`` says why the worker can't run the adapter at all, e.g. it needs a
browser that isn't installed.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from furnest.db.base import Base, UTCDateTime, utcnow


class Source(Base):
    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool | None]
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    listings_found: Mapped[int] = mapped_column(default=0)
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    overrides: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
