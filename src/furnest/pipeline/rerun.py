"""The ``rerun_criteria`` job: score stored listings again against one criteria version.

The dashboard's "Re-run criteria" button queues this job with the version that was
active at the time, so a run explains its results against the exact profile the user
saw, even if another version is activated while the job waits in the queue.

The handler is deliberately thin. It loads the version, runs
:func:`~furnest.matching.match_listing` on every listing that ``listings`` yields,
with the amenities extracted when the listing was ingested, and hands each result to
``record``. The default source yields nothing and the default sink discards. The
worker plugs in the active stored listings (``SqlListingStore.active``) and
:class:`~furnest.pipeline.properties.PropertyPipeline`, which updates scores without
moving any property a person already decided on. The handler checks
``ctx.cancelled`` between listings, and re-running it is harmless because every
result is recomputed from scratch.

A payload with ``listing_ids`` scores only those listings (through ``by_id``), active
or not: splitting a property by hand queues one for the listings involved.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker

from furnest.db.criteria_store import CriteriaStore
from furnest.db.listing_store import StoredListing
from furnest.logging import get_logger
from furnest.matching import MatchResult, match_listing

if TYPE_CHECKING:
    from furnest.worker.execution import JobContext

RERUN_CRITERIA = "rerun_criteria"

ListingSource = Callable[[], Iterable[StoredListing]]
ListingsById = Callable[[Sequence[int]], Iterable[StoredListing]]
ResultSink = Callable[[StoredListing, MatchResult], None]

log = get_logger(__name__)


def no_listings() -> Iterable[StoredListing]:
    return ()


def none_by_id(_ids: Sequence[int]) -> Iterable[StoredListing]:
    return ()


def discard(_listing: StoredListing, _result: MatchResult) -> None:
    return None


class RerunCriteria:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        listings: ListingSource = no_listings,
        by_id: ListingsById = none_by_id,
        record: ResultSink = discard,
    ) -> None:
        self._store = CriteriaStore(session_factory)
        self._listings = listings
        self._by_id = by_id
        self._record = record

    def __call__(self, ctx: JobContext) -> None:
        version = int(ctx.payload["criteria_version"])
        criteria = self._store.get(version).criteria
        ids = ctx.payload.get("listing_ids")
        listings = self._listings() if ids is None else self._by_id([int(i) for i in ids])
        scored = 0
        for stored in listings:
            if ctx.cancelled.is_set():
                log.info("criteria_rerun_cancelled", criteria_version=version, scored=scored)
                return
            result = match_listing(
                stored.for_matching, criteria, criteria_version=version, amenities=stored.amenities
            )
            self._record(stored, result)
            scored += 1
        log.info("criteria_rerun", criteria_version=version, scored=scored)
