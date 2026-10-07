"""The images.hash job: cache by URL, failures recorded, scheduling gated on IMAGES_ENABLED."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import ImageSettings, Settings
from furnest.db.image_store import SqlImageStore
from furnest.db.listing_store import SqlListingStore
from furnest.dedup.phash import ImageFetchError
from furnest.domain import NormalizedListing
from furnest.pipeline.images import (
    HASH_IMAGES,
    HashCounts,
    HashListingImages,
    schedule_image_hashing,
)
from furnest.worker.execution import Handler, JobContext

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
A = "https://img.example.com/a.jpg"
B = "https://img.example.com/b.jpg"
BAD = "https://img.example.com/bad.jpg"
H1, H2 = "ee9d91626a159595", "f5989e69c93230c7"


class FakeFetcher:
    def __init__(self, hashes: dict[str, str]) -> None:
        self.hashes = hashes
        self.fetched: list[str] = []

    def fetch_phash(self, url: str) -> str:
        self.fetched.append(url)
        if url not in self.hashes:
            raise ImageFetchError("HTTP 404", url=url)
        return self.hashes[url]


class FakeWorker:
    def __init__(self) -> None:
        self.scheduled: list[tuple[str, timedelta, timedelta | None]] = []

    def schedule(
        self, name: str, handler: Handler, *, interval: timedelta, timeout: timedelta | None = None
    ) -> None:
        self.scheduled.append((name, interval, timeout))


def add(factory: sessionmaker[Session], source_id: str, urls: list[str]) -> None:
    item = NormalizedListing.model_validate(
        {
            "source": "test",
            "source_id": source_id,
            "url": f"https://example.com/{source_id}",
            "title": "Furnished place",
            "images": urls,
        }
    )
    SqlListingStore(factory).upsert_batch("test", [item], seen_at=T0)


def job(
    factory: sessionmaker[Session],
    fetcher: FakeFetcher,
    **settings: Any,  # noqa: ANN401
) -> HashListingImages:
    return HashListingImages(factory, fetcher, settings=ImageSettings(**settings), clock=lambda: T0)


def context() -> JobContext:
    return JobContext(
        job_id=HASH_IMAGES,
        kind=HASH_IMAGES,
        payload={},
        attempt=1,
        deadline=T0 + timedelta(hours=1),
    )


def test_hashes_pending_photos_and_records_failures(
    session_factory: sessionmaker[Session],
) -> None:
    add(session_factory, "one", [A, BAD, B])
    fetcher = FakeFetcher({A: H1, B: H2})

    counts = job(session_factory, fetcher).run(context())

    assert counts == HashCounts(hashed=2, failed=1)
    assert fetcher.fetched == [A, BAD, B]
    images = SqlImageStore(session_factory)
    assert images.pending(limit=10, per_listing=10) == []
    assert images.cached(A) == H1


def test_a_known_url_is_copied_without_a_fetch(session_factory: sessionmaker[Session]) -> None:
    add(session_factory, "one", [A])
    fetcher = FakeFetcher({A: H1})
    job(session_factory, fetcher).run()
    add(session_factory, "two", [A])

    counts = job(session_factory, fetcher).run()

    assert counts == HashCounts(cached=1)
    assert fetcher.fetched == [A]


def test_a_failed_url_is_not_tried_again(session_factory: sessionmaker[Session]) -> None:
    add(session_factory, "one", [BAD])
    fetcher = FakeFetcher({})
    job(session_factory, fetcher).run()

    assert job(session_factory, fetcher).run() == HashCounts()
    assert fetcher.fetched == [BAD]


def test_batch_size_bounds_one_run(session_factory: sessionmaker[Session]) -> None:
    add(session_factory, "one", [A, B])
    fetcher = FakeFetcher({A: H1, B: H2})

    job(session_factory, fetcher, batch_size=1)(context())

    assert fetcher.fetched == [A]


def test_a_cancelled_run_stops_before_the_next_photo(
    session_factory: sessionmaker[Session],
) -> None:
    add(session_factory, "one", [A, B])
    fetcher = FakeFetcher({A: H1, B: H2})
    ctx = context()
    ctx.cancelled.set()

    assert job(session_factory, fetcher).run(ctx) == HashCounts()
    assert fetcher.fetched == []


def test_scheduled_when_enabled(session_factory: sessionmaker[Session]) -> None:
    worker = FakeWorker()
    settings = Settings(images=ImageSettings(interval_minutes=5, batch_size=10))

    assert schedule_image_hashing(worker, session_factory, settings) is True

    ((name, interval, timeout),) = worker.scheduled
    assert name == HASH_IMAGES
    assert interval == timedelta(minutes=5)
    # Each of 10 URLs: a 15s timeout plus a 2s rate-limit slot, and a minute to spare.
    assert timeout == timedelta(seconds=10 * 17 + 60)


def test_not_scheduled_when_disabled(session_factory: sessionmaker[Session]) -> None:
    worker = FakeWorker()

    assert (
        schedule_image_hashing(
            worker, session_factory, Settings(images=ImageSettings(enabled=False))
        )
        is False
    )
    assert worker.scheduled == []
