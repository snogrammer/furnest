"""Cached LLM amenity verdicts; the logic is in ``furnest.db.amenity_cache``.

One row per listing-content hash (see ``furnest.matching.amenities.llm.content_hash``).
The hash already covers the model and prompt version, so a changed listing, model
or prompt simply misses and writes a new row. Rows are never updated.
"""

from datetime import datetime

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from furnest.db.base import Base, UTCDateTime, utcnow


class AmenityExtraction(Base):
    __tablename__ = "amenity_extractions"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(200))
    # Sorted lists of Amenity values.
    present: Mapped[list[str]] = mapped_column(JSON)
    absent: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
