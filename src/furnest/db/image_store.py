"""SqlImageStore: perceptual hashes of listing photos, shared by URL.

``listing_images`` holds one row per photo per listing. Two listings, often on two
sources, can show the same photo URL, and a photo only needs fetching once, so the
hash state is per URL: :meth:`SqlImageStore.record` writes it to every row with that
URL, and :meth:`SqlImageStore.cached` finds a hash another row already has. A row
that arrives later with a known URL is picked up by the next run without a fetch.

A row whose hash and error are both NULL is pending. A failed URL keeps its error
and isn't tried again. ``SqlListingStore`` keeps the state of URLs a listing still
has when it rewrites the listing's photos.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.models import Listing, ListingImage
from furnest.db.session import session_scope

# Matches the ``hash_error`` column.
MAX_HASH_ERROR_LENGTH = 255


class SqlImageStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def pending(self, *, limit: int, per_listing: int) -> list[str]:
        """Up to ``limit`` distinct URLs to hash, oldest rows first.

        Only the first ``per_listing`` photos of listings that aren't removed count.
        """
        first_id = func.min(ListingImage.id)
        statement = (
            select(ListingImage.url)
            .join(Listing, Listing.id == ListingImage.listing_id)
            .where(
                ListingImage.phash.is_(None),
                ListingImage.hash_error.is_(None),
                ListingImage.position < per_listing,
                Listing.removed_at.is_(None),
            )
            .group_by(ListingImage.url)
            .order_by(first_id)
            .limit(limit)
        )
        with session_scope(self._session_factory) as session:
            return list(session.scalars(statement))

    def cached(self, url: str) -> str | None:
        """A hash some row with this URL already has, if any."""
        with session_scope(self._session_factory) as session:
            return session.scalars(
                select(ListingImage.phash)
                .where(ListingImage.url == url, ListingImage.phash.is_not(None))
                .limit(1)
            ).first()

    def record(
        self, url: str, *, at: datetime, phash: str | None = None, error: str | None = None
    ) -> int:
        """Store a hash or an error for every row with ``url``; return how many rows.

        Exactly one of ``phash`` and ``error`` must be given.
        """
        if (phash is None) == (error is None):
            msg = "record needs exactly one of phash and error"
            raise ValueError(msg)
        if error is not None:
            error = error[:MAX_HASH_ERROR_LENGTH]
        with session_scope(self._session_factory) as session:
            result = session.execute(
                update(ListingImage)
                .where(ListingImage.url == url)
                .values(phash=phash, hash_error=error, hashed_at=at)
            )
            return int(result.rowcount)  # type: ignore[attr-defined]

    def hashes(self, listing_id: int) -> Sequence[str]:
        """The listing's photo hashes in photo order, skipping photos without one."""
        with session_scope(self._session_factory) as session:
            return list(
                session.scalars(
                    select(ListingImage.phash)
                    .where(ListingImage.listing_id == listing_id, ListingImage.phash.is_not(None))
                    .order_by(ListingImage.position)
                )
            )
