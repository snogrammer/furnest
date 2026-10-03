"""Versioned search criteria; the logic is in ``furnest.db.criteria_store``.

Each row is an immutable snapshot of a :class:`furnest.domain.Criteria` as JSON.
Saving a profile appends a version and activating an old one just moves the
``is_active`` flag, so history is never rewritten. A partial unique index allows at
most one active row; the store keeps it at exactly one once any version exists.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from furnest.db.base import Base, UTCDateTime, utcnow


class CriteriaVersion(Base):
    __tablename__ = "criteria_versions"
    __table_args__ = (
        Index(
            "uq_criteria_versions_one_active",
            "is_active",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(unique=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Who saved it: "dashboard", "discord", "system", "cli".
    created_by: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(default=False)
