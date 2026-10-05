"""SqlListingStore: stored listings, price history and source runs.

Ingestion (``furnest.pipeline.ingest``) hands each successful run's listings to
:meth:`SqlListingStore.upsert_batch`, which deduplicates exactly on
``(source, source_id)``. A listing seen for the first time is inserted with its
photos and first price. One seen before gets a new ``last_seen``, and its data
columns and photos are rewritten only when its :func:`content_hash` changed. A
price row is added whenever the price differs from the last one recorded, so the
history holds changes, not observations. A listing that lost its price keeps the
last known one in the history.

:meth:`SqlListingStore.mark_missing` then counts a missed run for every active
listing of the source that the run didn't return, and marks a listing removed once
it has missed ``after_runs`` runs *and* hasn't been seen for ``after_hours``.
Requiring both means neither one short search nor a long worker outage removes
anything on its own. A removed listing that shows up again is active again
(``relisted``). Only successful runs reach this store, so a failing source never
removes listings.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.models import Listing, ListingImage, PriceHistory, SourceRun, SourceRunStatus
from furnest.db.session import session_scope
from furnest.domain import Amenity, AmenityResult, NormalizedListing
from furnest.domain.price import Price

# Long enough for a useful error summary, short enough for a table cell.
MAX_ERROR_LENGTH = 2000

# Fields that change on every sighting or carry source noise, so they don't count as a change.
_UNHASHED_FIELDS = frozenset({"first_seen", "last_seen", "raw"})


def _dump(listing: NormalizedListing) -> dict[str, Any]:
    data = listing.model_dump(mode="json")
    # frozenset order isn't stable; store amenities sorted so equal listings dump equally.
    data["amenities"] = sorted(listing.amenities)
    return data


def content_hash(listing: NormalizedListing) -> str:
    """SHA-256 of the listing's content, ignoring timestamps and the raw payload."""
    data = {k: v for k, v in _dump(listing).items() if k not in _UNHASHED_FIELDS}
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class StoredListing:
    id: int
    listing: NormalizedListing
    property_id: int | None
    missed_runs: int
    removed_at: datetime | None
    # The last amenity extraction, or None if the listing hasn't been extracted yet.
    amenities: AmenityResult | None


@dataclass(frozen=True)
class PricePoint:
    price: Price
    observed_at: datetime


@dataclass(frozen=True)
class UpsertResult:
    """What one batch did. Each listing id appears in at most one of the id tuples,
    except ``seen_ids``, which holds every listing in the batch."""

    seen_ids: frozenset[int] = frozenset()
    new_ids: tuple[int, ...] = ()
    updated_ids: tuple[int, ...] = ()
    # Previously removed listings that came back, changed or not.
    relisted_ids: tuple[int, ...] = ()
    price_changes: int = 0

    @property
    def changed_ids(self) -> tuple[int, ...]:
        """Listings whose match result may differ from last time: new, changed or relisted."""
        return tuple(sorted({*self.new_ids, *self.updated_ids, *self.relisted_ids}))


@dataclass(frozen=True)
class RunCounts:
    found: int = 0
    new: int = 0
    updated: int = 0
    removed: int = 0


@dataclass(frozen=True)
class SourceRunState:
    id: int
    source: str
    started_at: datetime
    finished_at: datetime | None
    status: SourceRunStatus
    counts: RunCounts = field(default_factory=RunCounts)
    error: str | None = None


def _stored(row: Listing) -> StoredListing:
    listing = NormalizedListing.model_validate(
        {**row.data, "first_seen": row.first_seen, "last_seen": row.last_seen}
    )
    amenities = None
    if row.amenities_present is not None and row.amenities_absent is not None:
        amenities = AmenityResult(
            present=frozenset(Amenity(tag) for tag in row.amenities_present),
            absent=frozenset(Amenity(tag) for tag in row.amenities_absent),
        )
    return StoredListing(
        id=row.id,
        listing=listing,
        property_id=row.property_id,
        missed_runs=row.missed_runs,
        removed_at=row.removed_at,
        amenities=amenities,
    )


def _run_state(row: SourceRun) -> SourceRunState:
    return SourceRunState(
        id=row.id,
        source=row.source,
        started_at=row.started_at,
        finished_at=row.finished_at,
        status=row.status,
        counts=RunCounts(found=row.found, new=row.new, updated=row.updated, removed=row.removed),
        error=row.error,
    )


def _write_content(row: Listing, listing: NormalizedListing, digest: str) -> None:
    monthly = listing.monthly_price
    row.url = str(listing.url)
    row.title = listing.title
    row.monthly_price_minor = monthly.amount_minor if monthly else None
    row.currency = monthly.currency if monthly else None
    row.beds = listing.beds
    row.baths = listing.baths
    row.lat = listing.lat
    row.lng = listing.lng
    row.data = _dump(listing)
    row.content_hash = digest


def _write_images(session: Session, listing_id: int, listing: NormalizedListing) -> None:
    session.execute(delete(ListingImage).where(ListingImage.listing_id == listing_id))
    urls = list(dict.fromkeys(str(url) for url in listing.images))
    session.add_all(
        ListingImage(listing_id=listing_id, position=position, url=url)
        for position, url in enumerate(urls)
    )


def _record_price(session: Session, listing_id: int, price: Price | None, at: datetime) -> bool:
    """Add a price row if ``price`` differs from the last one; return whether it did."""
    if price is None:
        return False
    last = session.scalars(
        select(PriceHistory)
        .where(PriceHistory.listing_id == listing_id)
        .order_by(PriceHistory.observed_at.desc(), PriceHistory.id.desc())
        .limit(1)
    ).first()
    amount_max = price.amount_max.amount_minor if price.amount_max else None
    if (
        last is not None
        and last.amount_minor == price.amount.amount_minor
        and last.amount_max_minor == amount_max
        and last.currency == price.currency
        and last.period == price.period.value
    ):
        return False
    session.add(
        PriceHistory(
            listing_id=listing_id,
            amount_minor=price.amount.amount_minor,
            amount_max_minor=amount_max,
            currency=price.currency,
            period=price.period.value,
            monthly_minor=price.monthly.amount_minor,
            observed_at=at,
        )
    )
    return True


class SqlListingStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def upsert_batch(
        self, source: str, listings: Iterable[NormalizedListing], *, seen_at: datetime
    ) -> UpsertResult:
        """Insert or refresh every listing from one run of ``source``, in one transaction.

        A listing from another source raises ``ValueError``. If the batch repeats a
        ``source_id``, the last one wins.
        """
        batch: dict[str, NormalizedListing] = {}
        for listing in listings:
            if listing.source != source:
                msg = f"listing from {listing.source!r} in a batch for {source!r}"
                raise ValueError(msg)
            batch[listing.source_id] = listing
        if not batch:
            return UpsertResult()

        seen: set[int] = set()
        new: list[int] = []
        updated: list[int] = []
        relisted: list[int] = []
        price_changes = 0
        with session_scope(self._session_factory) as session:
            existing = {
                row.source_id: row
                for row in session.scalars(
                    select(Listing).where(
                        Listing.source == source, Listing.source_id.in_(list(batch))
                    )
                )
            }
            for source_id, listing in batch.items():
                digest = content_hash(listing)
                row = existing.get(source_id)
                if row is None:
                    row = Listing(
                        source=source,
                        source_id=source_id,
                        first_seen=seen_at,
                        last_seen=seen_at,
                        missed_runs=0,
                    )
                    _write_content(row, listing, digest)
                    session.add(row)
                    session.flush()
                    _write_images(session, row.id, listing)
                    new.append(row.id)
                else:
                    row.last_seen = seen_at
                    row.missed_runs = 0
                    if row.removed_at is not None:
                        row.removed_at = None
                        relisted.append(row.id)
                    if row.content_hash == digest:
                        seen.add(row.id)
                        continue
                    _write_content(row, listing, digest)
                    _write_images(session, row.id, listing)
                    if row.id not in relisted:
                        updated.append(row.id)
                seen.add(row.id)
                price_changes += _record_price(session, row.id, listing.price, seen_at)
        return UpsertResult(
            seen_ids=frozenset(seen),
            new_ids=tuple(new),
            updated_ids=tuple(updated),
            relisted_ids=tuple(relisted),
            price_changes=price_changes,
        )

    def mark_missing(
        self,
        source: str,
        seen_ids: Iterable[int],
        *,
        at: datetime,
        after_runs: int,
        after_hours: float,
    ) -> list[int]:
        """Count a missed run for active listings of ``source`` not in ``seen_ids`` and
        mark removed those gone for ``after_runs`` runs and ``after_hours``.

        Returns the ids marked removed by this call.
        """
        active = (Listing.source == source) & Listing.removed_at.is_(None)
        with session_scope(self._session_factory) as session:
            session.execute(
                update(Listing)
                .where(active, Listing.id.not_in(list(seen_ids)))
                .values(missed_runs=Listing.missed_runs + 1)
            )
            gone = active & (Listing.missed_runs >= after_runs)
            gone &= Listing.last_seen <= at - timedelta(hours=after_hours)
            removed = list(session.scalars(select(Listing.id).where(gone).order_by(Listing.id)))
            if removed:
                session.execute(
                    update(Listing).where(Listing.id.in_(removed)).values(removed_at=at)
                )
        return removed

    def set_amenities(self, listing_id: int, result: AmenityResult) -> None:
        with session_scope(self._session_factory) as session:
            session.execute(
                update(Listing)
                .where(Listing.id == listing_id)
                .values(
                    amenities_present=sorted(result.present),
                    amenities_absent=sorted(result.absent),
                )
            )

    def get(self, listing_id: int) -> StoredListing | None:
        with session_scope(self._session_factory) as session:
            row = session.get(Listing, listing_id)
            return None if row is None else _stored(row)

    def find(self, source: str, source_id: str) -> StoredListing | None:
        with session_scope(self._session_factory) as session:
            row = session.scalars(
                select(Listing).where(Listing.source == source, Listing.source_id == source_id)
            ).first()
            return None if row is None else _stored(row)

    def active(self, source: str | None = None) -> list[StoredListing]:
        """Listings not marked removed, oldest first, optionally for one source."""
        statement = select(Listing).where(Listing.removed_at.is_(None)).order_by(Listing.id)
        if source is not None:
            statement = statement.where(Listing.source == source)
        with session_scope(self._session_factory) as session:
            return [_stored(row) for row in session.scalars(statement)]

    def images(self, listing_id: int) -> list[str]:
        with session_scope(self._session_factory) as session:
            return list(
                session.scalars(
                    select(ListingImage.url)
                    .where(ListingImage.listing_id == listing_id)
                    .order_by(ListingImage.position)
                )
            )

    def price_history(self, listing_id: int) -> list[PricePoint]:
        """Every recorded price for the listing, oldest first."""
        with session_scope(self._session_factory) as session:
            rows = session.scalars(
                select(PriceHistory)
                .where(PriceHistory.listing_id == listing_id)
                .order_by(PriceHistory.observed_at, PriceHistory.id)
            )
            return [
                PricePoint(
                    price=Price.model_validate(
                        {
                            "amount": {"amount_minor": row.amount_minor, "currency": row.currency},
                            "amount_max": None
                            if row.amount_max_minor is None
                            else {"amount_minor": row.amount_max_minor, "currency": row.currency},
                            "period": row.period,
                        }
                    ),
                    observed_at=row.observed_at,
                )
                for row in rows
            ]

    def start_run(self, source: str, *, at: datetime) -> int:
        with session_scope(self._session_factory) as session:
            row = SourceRun(source=source, started_at=at, status=SourceRunStatus.RUNNING)
            session.add(row)
            session.flush()
            return row.id

    def finish_run(
        self,
        run_id: int,
        *,
        status: SourceRunStatus,
        at: datetime,
        counts: RunCounts | None = None,
        error: str | None = None,
    ) -> None:
        counts = counts or RunCounts()
        with session_scope(self._session_factory) as session:
            session.execute(
                update(SourceRun)
                .where(SourceRun.id == run_id)
                .values(
                    status=status,
                    finished_at=at,
                    found=counts.found,
                    new=counts.new,
                    updated=counts.updated,
                    removed=counts.removed,
                    error=None if error is None else error[:MAX_ERROR_LENGTH],
                )
            )

    def failure_counts(self) -> dict[str, int]:
        """How many stored runs failed, per source; sources with none are left out."""
        statement = (
            select(SourceRun.source, func.count())
            .where(SourceRun.status == SourceRunStatus.FAILED)
            .group_by(SourceRun.source)
        )
        with session_scope(self._session_factory) as session:
            return {source: count for source, count in session.execute(statement)}

    def runs(self, source: str | None = None, *, limit: int = 50) -> Sequence[SourceRunState]:
        """The most recent runs, newest first, optionally for one source."""
        statement = select(SourceRun).order_by(SourceRun.id.desc()).limit(limit)
        if source is not None:
            statement = statement.where(SourceRun.source == source)
        with session_scope(self._session_factory) as session:
            return [_run_state(row) for row in session.scalars(statement)]
