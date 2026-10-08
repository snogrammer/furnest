"""Scam and risk flags raised on a property; the logic is in ``furnest.enrichment.risk``.

One row per ``(property_id, kind)``. ``severity`` is ``low``, ``medium`` or ``high``
and ``detail`` holds the evidence (a text summary plus the numbers behind it). A
re-run of the check updates rows in place and removes the flags that no longer apply.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from furnest.db.base import Base, UTCDateTime, utcnow


class RiskFlag(Base):
    __tablename__ = "risk_flags"
    __table_args__ = (UniqueConstraint("property_id", "kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(
        ForeignKey("properties.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    summary: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
