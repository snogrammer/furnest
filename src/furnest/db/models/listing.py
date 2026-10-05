"""Stored listings, their photos, price history and source runs.

The logic is in ``furnest.db.listing_store``. A ``listings`` row is one listing on
one source, unique on ``(source, source_id)`` (exact dedup). ``data`` holds the
whole :class:`~furnest.domain.NormalizedListing` as JSON, so it reads back exactly
as the adapter produced it. The other data columns are copies kept for querying.
``content_hash`` decides whether a re-seen listing changed. ``property_id`` groups
listings under a property; the ``properties`` table and its foreign key come with #27.

``missed_runs`` counts successful runs of the source that didn't return the
listing, and ``removed_at`` is set once it has been gone long enough. Seeing it
again clears both.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Enum, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from furnest.db.base import Base, UTCDateTime, utcnow


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("source", "source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(255))
    property_id: Mapped[int | None] = mapped_column(index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    monthly_price_minor: Mapped[int | None]
    currency: Mapped[str | None] = mapped_column(String(3))
    beds: Mapped[int | None]
    baths: Mapped[float | None] = mapped_column(Float)
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    # The last amenity extraction, as sorted lists of Amenity values; NULL until extracted.
    amenities_present: Mapped[list[str] | None] = mapped_column(JSON)
    amenities_absent: Mapped[list[str] | None] = mapped_column(JSON)
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime)
    missed_runs: Mapped[int] = mapped_column(default=0)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ListingImage(Base):
    __tablename__ = "listing_images"
    __table_args__ = (UniqueConstraint("listing_id", "url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    position: Mapped[int]
    url: Mapped[str] = mapped_column(Text)


class PriceHistory(Base):
    """One row per observed price; a row is added only when the price changes."""

    __tablename__ = "price_history"
    __table_args__ = (
        Index("ix_price_history_listing_id_observed_at", "listing_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    amount_minor: Mapped[int]
    amount_max_minor: Mapped[int | None]
    currency: Mapped[str] = mapped_column(String(3))
    period: Mapped[str] = mapped_column(String(16))
    monthly_minor: Mapped[int]
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class SourceRunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class SourceRun(Base):
    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[SourceRunStatus] = mapped_column(
        Enum(
            SourceRunStatus,
            name="source_run_status",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda statuses: [s.value for s in statuses],
        ),
        default=SourceRunStatus.RUNNING,
    )
    found: Mapped[int] = mapped_column(default=0)
    new: Mapped[int] = mapped_column(default=0)
    updated: Mapped[int] = mapped_column(default=0)
    removed: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
