"""The ``rerun_criteria`` job: score stored listings again against one criteria version.

The dashboard's "Re-run criteria" button queues this job with the version that was
active at the time, so a run explains its results against the exact profile the user
saw, even if another version is activated while the job waits in the queue.

The handler is deliberately thin. It loads the version, runs
:func:`~furnest.matching.match_listing` on every listing that ``listings`` yields and
hands each result to ``record``. The default source yields nothing and the default
sink discards. #28 plugs in stored listings (``SqlListingStore.active``) and the
property store, which also keeps human-decided states from regressing. The handler
checks ``ctx.cancelled`` between listings, and re-running it is harmless because
every result is recomputed from scratch.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker

from furnest.db.criteria_store import CriteriaStore
from furnest.domain import NormalizedListing
from furnest.logging import get_logger
from furnest.matching import MatchResult, match_listing

if TYPE_CHECKING:
    from furnest.worker.execution import JobContext

RERUN_CRITERIA = "rerun_criteria"

ListingSource = Callable[[], Iterable[NormalizedListing]]
ResultSink = Callable[[NormalizedListing, MatchResult], None]

log = get_logger(__name__)


def no_listings() -> Iterable[NormalizedListing]:
    return ()


def discard(_listing: NormalizedListing, _result: MatchResult) -> None:
    return None


class RerunCriteria:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        listings: ListingSource = no_listings,
        record: ResultSink = discard,
    ) -> None:
        self._store = CriteriaStore(session_factory)
        self._listings = listings
        self._record = record

    def __call__(self, ctx: JobContext) -> None:
        version = int(ctx.payload["criteria_version"])
        criteria = self._store.get(version).criteria
        scored = 0
        for listing in self._listings():
            if ctx.cancelled.is_set():
                log.info("criteria_rerun_cancelled", criteria_version=version, scored=scored)
                return
            self._record(listing, match_listing(listing, criteria, criteria_version=version))
            scored += 1
        log.info("criteria_rerun", criteria_version=version, scored=scored)
