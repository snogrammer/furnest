"""SqlImageStore and photo hash state across SqlListingStore rewrites."""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.image_store import MAX_HASH_ERROR_LENGTH, SqlImageStore
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import Listing, ListingImage
from furnest.db.session import session_scope
from furnest.domain import NormalizedListing

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
A = "https://img.example.com/a.jpg"
B = "https://img.example.com/b.jpg"
C = "https://img.example.com/c.jpg"
H1, H2 = "ee9d91626a159595", "f5989e69c93230c7"


def listing(source_id: str, images: list[str], **fields: Any) -> NormalizedListing:  # noqa: ANN401
    values: dict[str, Any] = {
        "source": "test",
        "source_id": source_id,
        "url": f"https://example.com/{source_id}",
        "title": "Furnished place",
        "images": images,
    }
    values.update(fields)
    return NormalizedListing.model_validate(values)


@pytest.fixture
def listings(session_factory: sessionmaker[Session]) -> SqlListingStore:
    return SqlListingStore(session_factory)


@pytest.fixture
def images(session_factory: sessionmaker[Session]) -> SqlImageStore:
    return SqlImageStore(session_factory)


def add(store: SqlListingStore, source_id: str, urls: list[str], **fields: Any) -> int:  # noqa: ANN401
    result = store.upsert_batch("test", [listing(source_id, urls, **fields)], seen_at=T0)
    return (result.new_ids or result.updated_ids or tuple(result.seen_ids))[0]


def rows(factory: sessionmaker[Session], url: str) -> list[tuple[str | None, str | None]]:
    with session_scope(factory) as session:
        return [
            (row.phash, row.hash_error)
            for row in session.scalars(select(ListingImage).where(ListingImage.url == url))
        ]


def test_pending_lists_each_url_once_oldest_first(
    listings: SqlListingStore, images: SqlImageStore
) -> None:
    add(listings, "one", [B, A])
    add(listings, "two", [A, C])

    assert images.pending(limit=10, per_listing=10) == [B, A, C]
    assert images.pending(limit=2, per_listing=10) == [B, A]


def test_pending_only_takes_the_first_photos_of_a_listing(
    listings: SqlListingStore, images: SqlImageStore
) -> None:
    add(listings, "one", [A, B, C])

    assert images.pending(limit=10, per_listing=2) == [A, B]


def test_pending_skips_hashed_failed_and_removed(
    listings: SqlListingStore, images: SqlImageStore, session_factory: sessionmaker[Session]
) -> None:
    add(listings, "one", [A, B])
    gone = add(listings, "two", [C])
    images.record(A, phash=H1, at=T0)
    images.record(B, error="HTTP 404", at=T0)
    with session_scope(session_factory) as session:
        row = session.get(Listing, gone)
        assert row is not None
        row.removed_at = T0

    assert images.pending(limit=10, per_listing=10) == []


def test_record_writes_every_row_with_the_url(
    listings: SqlListingStore, images: SqlImageStore, session_factory: sessionmaker[Session]
) -> None:
    add(listings, "one", [A])
    add(listings, "two", [A, B])

    assert images.record(A, phash=H1, at=T0) == 2

    assert rows(session_factory, A) == [(H1, None), (H1, None)]
    assert rows(session_factory, B) == [(None, None)]
    assert images.cached(A) == H1
    assert images.cached(B) is None


def test_record_needs_exactly_one_outcome(images: SqlImageStore) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        images.record(A, at=T0)
    with pytest.raises(ValueError, match="exactly one"):
        images.record(A, phash=H1, error="x", at=T0)


def test_long_errors_are_truncated(
    listings: SqlListingStore, images: SqlImageStore, session_factory: sessionmaker[Session]
) -> None:
    add(listings, "one", [A])

    images.record(A, error="x" * 1000, at=T0)

    assert rows(session_factory, A) == [(None, "x" * MAX_HASH_ERROR_LENGTH)]


def test_hashes_are_in_photo_order_and_skip_unhashed(
    listings: SqlListingStore, images: SqlImageStore
) -> None:
    listing_id = add(listings, "one", [A, B, C])
    images.record(C, phash=H2, at=T0)
    images.record(A, phash=H1, at=T0)

    assert images.hashes(listing_id) == [H1, H2]
    assert images.hashes(listing_id + 100) == []


def test_rewriting_photos_keeps_state_for_urls_still_there(
    listings: SqlListingStore, images: SqlImageStore, session_factory: sessionmaker[Session]
) -> None:
    listing_id = add(listings, "one", [A, B])
    images.record(A, phash=H1, at=T0)
    images.record(B, error="HTTP 404", at=T0)

    # A content change rewrites the photo rows: B is dropped, C is new, A moves.
    updated = add(listings, "one", [C, A], title="Renovated place")

    assert updated == listing_id
    assert images.hashes(listing_id) == [H1]
    assert rows(session_factory, B) == []
    assert images.pending(limit=10, per_listing=10) == [C]
    with session_scope(session_factory) as session:
        kept = session.scalars(select(ListingImage).where(ListingImage.url == A)).one()
        assert kept.position == 1
        assert kept.hashed_at == T0


def test_known_is_true_only_for_stored_photo_urls(
    listings: SqlListingStore, images: SqlImageStore
) -> None:
    add(listings, "l1", [A])

    assert images.known(A)
    assert not images.known(B)
