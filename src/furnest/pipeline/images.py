"""The ``images.hash`` job: perceptual hashes for listing photos that lack one.

Every ``IMAGES_INTERVAL_MINUTES`` the worker takes up to ``IMAGES_BATCH_SIZE``
pending photo URLs (:meth:`SqlImageStore.pending`). A URL another listing's photo
already has a hash for gets that hash without a fetch. The others are downloaded
and hashed by :class:`~furnest.dedup.phash.ImageFetcher`. A URL that fails gets its
reason recorded and is not tried again; it never fails the run.

Hashing runs on its own schedule rather than inside a source run, so a slow image
host can't push a scrape past its run timeout. Photos are only ever held in memory.

Hashes usually arrive after a listing's first match, when grouping had no photos to
compare. After each run the ``on_hashed`` hook gets the ids of the listings that
gained a hash, and the worker passes :meth:`PropertyPipeline.regroup
<furnest.pipeline.properties.PropertyPipeline.regroup>`, so those listings are
grouped again with their photos.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from furnest.config import ImageSettings, Settings
from furnest.db.base import utcnow
from furnest.db.image_store import SqlImageStore
from furnest.dedup.phash import ImageFetcher, ImageFetchError
from furnest.logging import get_logger
from furnest.worker.execution import Handler, JobContext

log = get_logger(__name__)

# The scheduled job's name (and its lease, ``scheduled:images.hash``).
HASH_IMAGES = "images.hash"


class PhashFetcher(Protocol):
    """The part of :class:`ImageFetcher` the job needs."""

    def fetch_phash(self, url: str) -> str: ...


@dataclass(frozen=True)
class HashCounts:
    hashed: int = 0
    cached: int = 0
    failed: int = 0


def _ignore(_listing_ids: Sequence[int]) -> None:
    pass


class HashListingImages:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        fetcher: PhashFetcher,
        *,
        settings: ImageSettings,
        on_hashed: Callable[[Sequence[int]], None] = _ignore,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = SqlImageStore(session_factory)
        self._fetcher = fetcher
        self._settings = settings
        self._on_hashed = on_hashed
        self._clock = clock

    def __call__(self, ctx: JobContext) -> None:
        self.run(ctx)

    def run(self, ctx: JobContext | None = None) -> HashCounts:
        urls = self._store.pending(
            limit=self._settings.batch_size, per_listing=self._settings.per_listing
        )
        hashed = cached = failed = 0
        new_hashes: list[str] = []
        for url in urls:
            if ctx is not None and ctx.cancelled.is_set():
                break
            known = self._store.cached(url)
            if known is not None:
                self._store.record(url, phash=known, at=self._clock())
                new_hashes.append(url)
                cached += 1
                continue
            try:
                phash = self._fetcher.fetch_phash(url)
            except ImageFetchError as exc:
                self._store.record(url, error=exc.reason, at=self._clock())
                log.info("image_hash_failed", url=url, reason=exc.reason)
                failed += 1
                continue
            self._store.record(url, phash=phash, at=self._clock())
            new_hashes.append(url)
            hashed += 1
        counts = HashCounts(hashed=hashed, cached=cached, failed=failed)
        if urls:
            log.info("images_hashed", hashed=hashed, cached=cached, failed=failed)
        if new_hashes:
            self._on_hashed(self._store.listings_with(new_hashes))
        return counts


class Scheduler(Protocol):
    """The part of :class:`furnest.worker.runtime.Worker` this module needs."""

    def schedule(
        self,
        name: str,
        handler: Handler,
        *,
        interval: timedelta,
        timeout: timedelta | None = None,
    ) -> None: ...


def schedule_image_hashing(
    worker: Scheduler,
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    on_hashed: Callable[[Sequence[int]], None] = _ignore,
) -> bool:
    """Schedule the job if ``IMAGES_ENABLED`` is set; returns whether it did."""
    images = settings.images
    if not images.enabled:
        return False
    fetcher = ImageFetcher(images, settings.sources)
    # Room for every URL in a batch to wait out a rate-limit slot and its timeout.
    per_url = images.timeout_seconds + 60.0 / images.requests_per_minute
    worker.schedule(
        HASH_IMAGES,
        HashListingImages(session_factory, fetcher, settings=images, on_hashed=on_hashed),
        interval=timedelta(minutes=images.interval_minutes),
        timeout=timedelta(seconds=images.batch_size * per_url + 60),
    )
    log.info("image_hashing_scheduled", interval_minutes=images.interval_minutes)
    return True
