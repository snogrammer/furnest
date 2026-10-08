"""Listing ingestion: run one adapter, store what it found, match what changed.

:class:`ListingIngestor` is what :class:`furnest.pipeline.schedule.SourceRun` calls for
each source run.
One run:

1. records a ``source_runs`` row as ``running``;
2. calls ``adapter.search`` with the active criteria;
3. upserts the results (:meth:`SqlListingStore.upsert_batch`) and counts a missed
   run for the source's listings it didn't return, marking some removed
   (:meth:`SqlListingStore.mark_missing`);
4. finishes the run row as ``success`` with its counts;
5. hands the ids it marked removed to ``on_removed`` and the relisted ones to
   ``on_relisted``;
6. extracts amenities and runs :func:`~furnest.matching.match_listing` for every
   new, changed or relisted listing, and hands each result to ``record``.

Steps 5 and 6 run after the write transaction has committed, because an LLM
extractor can take seconds per listing and SQLite has a single writer. A hook, or
a listing's extraction or matching, that raises is logged and skipped; the run
still succeeds.

If the search or the store raises, the run row is finished as ``failed`` with the
error and the exception propagates, so the scheduler can update the ``sources``
counters and report it. Nothing is marked missing for a failed run. Without an
active criteria profile there is nothing to search for, so no run happens.

``cancelled`` is set by the worker when the run times out. A search that returns
after that is thrown away (the run fails with :class:`RunCancelledError`) rather than
stored late, and step 6 stops at the next listing.

The hooks default to doing nothing. The worker plugs in
:class:`~furnest.pipeline.properties.PropertyPipeline`, which moves passing listings'
properties to ``suggested`` and properties whose listings are all gone to
``unavailable``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from furnest.config import SourcesSettings
from furnest.db.base import utcnow
from furnest.db.criteria_store import CriteriaRecord, CriteriaStore
from furnest.db.listing_store import RunCounts, SqlListingStore, UpsertResult
from furnest.db.models import SourceRunStatus
from furnest.domain import AmenityExtractor
from furnest.logging import error_text, get_logger
from furnest.matching import match_listing
from furnest.pipeline.rerun import ResultSink, discard
from furnest.sources.base import SourceAdapter

ListingIdsHook = Callable[[Sequence[int]], None]

log = get_logger(__name__)


def ignore(_listing_ids: Sequence[int]) -> None:
    return None


class RunCancelledError(Exception):
    """The run was cancelled (it timed out) before its results were stored."""


@dataclass(frozen=True)
class RunSummary:
    run_id: int
    counts: RunCounts
    # Listings matched in step 5; less than the changed count if some failed.
    matched: int
    removed_ids: tuple[int, ...] = ()


class ListingIngestor:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        extractor: AmenityExtractor,
        settings: SourcesSettings,
        record: ResultSink = discard,
        on_removed: ListingIdsHook = ignore,
        on_relisted: ListingIdsHook = ignore,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._listings = SqlListingStore(session_factory)
        self._criteria = CriteriaStore(session_factory)
        self._extractor = extractor
        self._settings = settings
        self._record = record
        self._on_removed = on_removed
        self._on_relisted = on_relisted
        self._clock = clock

    def run(
        self, adapter: SourceAdapter, *, cancelled: threading.Event | None = None
    ) -> RunSummary | None:
        """Run ``adapter`` once; ``None`` if there is no active criteria profile."""
        source = adapter.name
        active = self._criteria.active()
        if active is None:
            log.info("ingest_skipped_no_criteria", source=source)
            return None

        run_id = self._listings.start_run(source, at=self._clock())
        try:
            found = adapter.search(active.criteria)
            if cancelled is not None and cancelled.is_set():
                msg = f"run of {source!r} was cancelled before its results were stored"
                raise RunCancelledError(msg)
            seen_at = self._clock()
            upserted = self._listings.upsert_batch(source, found, seen_at=seen_at)
            removed = self._listings.mark_missing(
                source,
                upserted.seen_ids,
                at=seen_at,
                after_runs=self._settings.remove_after_missed_runs,
                after_hours=self._settings.remove_after_hours,
            )
        except Exception as exc:
            self._listings.finish_run(
                run_id,
                status=SourceRunStatus.FAILED,
                at=self._clock(),
                error=error_text(exc),
            )
            raise

        counts = RunCounts(
            found=len(upserted.seen_ids),
            new=len(upserted.new_ids),
            updated=len(upserted.updated_ids) + len(upserted.relisted_ids),
            removed=len(removed),
        )
        self._listings.finish_run(
            run_id, status=SourceRunStatus.SUCCESS, at=self._clock(), counts=counts
        )
        self._call_hook("removed", self._on_removed, source, removed)
        self._call_hook("relisted", self._on_relisted, source, upserted.relisted_ids)
        matched = self._match_changed(source, upserted, active, cancelled)
        log.info(
            "ingest_run",
            source=source,
            found=counts.found,
            new=counts.new,
            updated=counts.updated,
            removed=counts.removed,
            price_changes=upserted.price_changes,
            matched=matched,
        )
        return RunSummary(run_id=run_id, counts=counts, matched=matched, removed_ids=tuple(removed))

    @staticmethod
    def _call_hook(
        what: str, hook: ListingIdsHook, source: str, listing_ids: Sequence[int]
    ) -> None:
        if not listing_ids:
            return
        try:
            hook(listing_ids)
        except Exception:
            log.exception("ingest_hook_failed", source=source, hook=what, listing_ids=listing_ids)

    def _match_changed(
        self,
        source: str,
        upserted: UpsertResult,
        active: CriteriaRecord,
        cancelled: threading.Event | None,
    ) -> int:
        matched = 0
        for listing_id in upserted.changed_ids:
            if cancelled is not None and cancelled.is_set():
                log.info("ingest_match_cancelled", source=source, matched=matched)
                break
            stored = self._listings.get(listing_id)
            if stored is None:  # deleted since the upsert; nothing to match
                continue
            try:
                amenities = self._extractor.extract(stored.listing)
                self._listings.set_amenities(listing_id, amenities)
                result = match_listing(
                    stored.listing,
                    active.criteria,
                    criteria_version=active.version,
                    amenities=amenities,
                )
                self._record(stored, result)
            except Exception:
                log.exception("ingest_match_failed", source=source, listing_id=listing_id)
                continue
            matched += 1
        return matched
