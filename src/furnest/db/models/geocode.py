"""Cached geocoder answers; the logic is in ``furnest.db.geocode_cache``.

One row per query key (see ``furnest.enrichment.geocoding.cache_key``), which
already covers the provider. ``found`` is false for a definite "not found", which
is kept so the same address isn't asked again until it expires. Errors are never
stored.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from furnest.db.base import Base, UTCDateTime, utcnow


class GeocodeCacheEntry(Base):
    __tablename__ = "geocode_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    # The query fields the key was made from, for debugging.
    query: Mapped[dict[str, Any]] = mapped_column(JSON)
    found: Mapped[bool]
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    # The geocoder's Address, as JSON.
    address: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    display_name: Mapped[str | None] = mapped_column(Text)
    precision: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
