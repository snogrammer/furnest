"""Things enrichment found out about a property; the logic is in ``furnest.enrichment``.

One row per finding. ``kind`` says what sort (``alternative``: the same unit listed
on another source). An alternative is identified by the listing it points at, so
re-running the finder updates rows in place and removes the ones it no longer finds.
``data`` holds the evidence: the similarity score and its signals, and the price.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from furnest.db.base import Base, UTCDateTime, utcnow

ALTERNATIVE = "alternative"


class EnrichmentResult(Base):
    __tablename__ = "enrichment_results"
    __table_args__ = (UniqueConstraint("property_id", "kind", "source", "source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(
        ForeignKey("properties.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    monthly_price_minor: Mapped[int | None]
    currency: Mapped[str | None] = mapped_column(String(3))
    # Weighted similarity to the property, 0 to 1.
    score: Mapped[float] = mapped_column(Float)
    # Whether the price is below the property's own (same currency); NULL if unknown.
    cheaper: Mapped[bool | None]
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
