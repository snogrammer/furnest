"""The ``enrich.alternatives`` job: the same unit listed on another source, maybe cheaper.

Approving a property queues this job (``SqlPropertyStore.transition``, in the same
transaction). The handler:

1. reads the property and its primary listing. It gives up quietly on a property that
   was merged, deleted, or moved out of the pipeline since (denied, archived, ...);
2. places the property: its coordinates, else the primary listing's, else the
   geocoder's answer for the listing's address when that is at least street-precise.
   Without a place there is nothing to search around, and the run ends;
3. searches every other enabled source (:meth:`SourceRegistry.configured` with the
   current dashboard toggles) for listings within ``ENRICHMENT_SEARCH_RADIUS_KM``. The
   property's own sources are skipped, since their listings are grouped already, and
   a source that fails is logged and skipped;
4. scores each candidate against the property's primary listing with
   :func:`~furnest.dedup.grouping.score_pair`, the same rules and ``GROUPING_*``
   thresholds that group listings: address, distance, title and description
   similarity, and photos. A candidate's first ``ENRICHMENT_PHOTOS_PER_CANDIDATE``
   photos are downloaded and perceptually hashed to compare with the property's
   stored hashes, but only for candidates that survive the vetoes;
5. stores the matches in ``enrichment_results`` (kind ``alternative``) with their
   price, whether it is below the property's, and the evidence. A re-run updates rows
   in place and removes the matches of the sources it searched that are gone, but
   leaves the rows of a source that failed this time.

Re-running is harmless, so a retry after a timeout or an error needs no cleanup. A
run in which every source failed raises, so the queue retries it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import EnrichmentSettings, GroupingSettings, SourcesSettings
from furnest.db.models import EnrichmentResult, Listing, ListingImage, Property
from furnest.db.models.enrichment import ALTERNATIVE
from furnest.db.property_store import ENRICH_ALTERNATIVES, primary_listing
from furnest.db.session import session_scope
from furnest.db.source_store import SqlSourceStore
from furnest.dedup.address import UsAddressParser
from furnest.dedup.grouping import AddressKey, GroupingThresholds, ListingFacts, score_pair
from furnest.dedup.phash import ImageFetchError
from furnest.domain import (
    Address,
    AddressParser,
    Area,
    Criteria,
    DistanceUnit,
    NormalizedListing,
    PropertyStatus,
    haversine,
)
from furnest.enrichment.geocoding import GeocodePrecision, Geocoder
from furnest.logging import get_logger

if TYPE_CHECKING:
    from furnest.sources.base import SourceAdapter
    from furnest.sources.registry import SourceRegistry
    from furnest.worker.execution import JobContext, JobRegistry

log = get_logger(__name__)

# Statuses in which an approved property is still worth enriching; later states are
# reached from ``approved``, possibly before the job ran.
ENRICHABLE = frozenset(
    {
        PropertyStatus.APPROVED,
        PropertyStatus.INQUIRY_DRAFTED,
        PropertyStatus.INQUIRY_SENT,
        PropertyStatus.IN_PROGRESS,
    }
)
# A geocoded location must be at least this precise to search around.
_PRECISE = frozenset({GeocodePrecision.ADDRESS, GeocodePrecision.STREET})


class PhashFetcher(Protocol):
    """The part of :class:`~furnest.dedup.phash.ImageFetcher` the job needs."""

    def fetch_phash(self, url: str) -> str: ...


@dataclass(frozen=True)
class Subject:
    """The property under enrichment, as the finder compares candidates with it."""

    property_id: int
    facts: ListingFacts
    address: Address
    beds: int | None
    price_minor: int | None
    currency: str | None
    # (source, source_id) of every listing of the property.
    own: frozenset[tuple[str, str]]
    sources: frozenset[str]
    located: bool


@dataclass(frozen=True)
class Match:
    listing: NormalizedListing
    score: float
    signals: dict[str, float]


def load_enrichable(session: Session, property_id: int) -> Property | None:
    """The property, unless it was merged, deleted or moved out of the pipeline."""
    row = session.get(Property, property_id)
    if row is None or row.merged_into_id is not None or row.status not in ENRICHABLE:
        return None
    return row


def _load(session: Session, property_id: int) -> Subject | None:
    row = load_enrichable(session, property_id)
    if row is None:
        return None
    primary = primary_listing(session, property_id)
    if primary is None:
        return None
    listings = list(session.scalars(select(Listing).where(Listing.property_id == property_id)))
    hashes = tuple(
        session.scalars(
            select(ListingImage.phash)
            .where(
                ListingImage.listing_id.in_([item.id for item in listings]),
                ListingImage.phash.is_not(None),
            )
            .order_by(ListingImage.listing_id, ListingImage.position)
        )
    )
    listing = NormalizedListing.model_validate(primary.data)
    lat, lng = primary.lat, primary.lng
    if row.lat is not None and row.lng is not None:
        lat, lng = row.lat, row.lng
    facts = ListingFacts(
        listing_id=primary.id,
        address=AddressKey.stored(primary.address_key, primary.street_key),
        lat=lat,
        lng=lng,
        beds=primary.beds,
        phashes=tuple(dict.fromkeys(h for h in hashes if h is not None)),
        title=primary.title,
        description=listing.description,
    )
    return Subject(
        property_id=property_id,
        facts=facts,
        address=listing.address,
        beds=primary.beds,
        price_minor=primary.monthly_price_minor,
        currency=primary.currency,
        own=frozenset((item.source, item.source_id) for item in listings),
        sources=frozenset(item.source for item in listings),
        located=lat is not None and lng is not None,
    )


class FindAlternatives:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        registry: SourceRegistry,
        *,
        sources: SourcesSettings,
        grouping: GroupingSettings,
        settings: EnrichmentSettings,
        geocoder: Geocoder,
        fetcher: PhashFetcher | None = None,
        parser: AddressParser | None = None,
    ) -> None:
        self._factory = session_factory
        self._registry = registry
        self._sources = sources
        self._settings = settings
        self._thresholds = GroupingThresholds.from_settings(grouping)
        self._geocoder = geocoder
        self._fetcher = fetcher
        self._parser = parser or UsAddressParser()

    def __call__(self, ctx: JobContext) -> None:
        self.run(int(ctx.payload["property_id"]), ctx)

    def run(self, property_id: int, ctx: JobContext | None = None) -> int:
        """Find and store the alternatives of one property; returns how many it has."""
        if not self._settings.enabled:
            return 0
        with session_scope(self._factory) as session:
            subject = _load(session, property_id)
        if subject is None:
            log.info("alternatives_skipped", property_id=property_id, reason="not enrichable")
            return 0
        subject = self._place(subject)
        if not subject.located:
            log.info("alternatives_skipped", property_id=property_id, reason="no location")
            return 0

        matches: list[Match] = []
        searched: list[str] = []
        failed = 0
        for adapter in self._adapters(subject):
            if ctx is not None and ctx.cancelled.is_set():
                return 0
            try:
                found = adapter.search(self._criteria(subject))
            except Exception:
                log.exception("alternatives_search_failed", source=adapter.name)
                failed += 1
                continue
            searched.append(adapter.name)
            matches.extend(self._matches(subject, found, ctx))
        if failed and not searched:
            msg = f"every source failed searching for alternatives to property {property_id}"
            raise RuntimeError(msg)
        if ctx is not None and ctx.cancelled.is_set():
            return 0
        return self._store(subject, matches, searched)

    # --- Steps -----------------------------------------------------------------------

    def _place(self, subject: Subject) -> Subject:
        """``subject`` with coordinates, geocoded from its address if it has none."""
        if subject.located:
            return subject
        result = self._geocoder.geocode(subject.address)
        if result is None or result.precision not in _PRECISE:
            return subject
        facts = replace(subject.facts, lat=result.lat, lng=result.lng)
        return replace(subject, facts=facts, located=True)

    def _adapters(self, subject: Subject) -> list[SourceAdapter]:
        stored = SqlSourceStore(self._factory).stored_enabled()
        return [
            source.create()
            for source in self._registry.configured(self._sources, stored)
            if source.config.enabled
            and source.config.disabled_reason is None
            and source.adapter.implemented
            and source.config.name not in subject.sources
        ]

    def _criteria(self, subject: Subject) -> Criteria:
        lat, lng = subject.facts.lat, subject.facts.lng
        assert lat is not None and lng is not None  # noqa: S101 - checked by ``located``
        return Criteria(
            areas=(
                Area(
                    name="alternatives",
                    lat=lat,
                    lng=lng,
                    radius=self._settings.search_radius_km,
                    unit=DistanceUnit.KILOMETERS,
                ),
            ),
            min_beds=subject.beds,
            furnished_required=False,
        )

    def _matches(
        self, subject: Subject, found: list[NormalizedListing], ctx: JobContext | None
    ) -> list[Match]:
        candidates = [item for item in found if (item.source, item.source_id) not in subject.own]
        candidates.sort(key=lambda item: _distance(subject, item))
        matches: list[Match] = []
        for item in candidates[: self._settings.max_candidates_per_source]:
            if ctx is not None and ctx.cancelled.is_set():
                break
            facts = self._facts(item)
            if score_pair(subject.facts, facts, self._thresholds).veto is not None:
                continue
            facts = self._with_photos(facts, item)
            result = score_pair(subject.facts, facts, self._thresholds)
            if result.matched:
                matches.append(Match(item, result.score, result.signals))
        return matches

    def _facts(self, item: NormalizedListing) -> ListingFacts:
        address = item.address
        key = None
        if any((address.line1, address.line2, address.city, address.postal_code)):
            key = AddressKey.of(self._parser.parse(address))
        return ListingFacts(
            listing_id=0,
            address=key,
            lat=item.lat,
            lng=item.lng,
            beds=item.beds,
            title=item.title,
            description=item.description,
        )

    def _with_photos(self, facts: ListingFacts, item: NormalizedListing) -> ListingFacts:
        limit = self._settings.photos_per_candidate
        if self._fetcher is None or limit == 0:
            return facts
        hashes: list[str] = []
        for url in item.images[:limit]:
            try:
                hashes.append(self._fetcher.fetch_phash(str(url)))
            except ImageFetchError as exc:
                log.info("alternatives_photo_failed", url=str(url), reason=exc.reason)
        return replace(facts, phashes=tuple(hashes))

    def _store(self, subject: Subject, matches: list[Match], searched: list[str]) -> int:
        with session_scope(self._factory) as session:
            existing = {
                (row.source, row.source_id): row
                for row in session.scalars(
                    select(EnrichmentResult).where(
                        EnrichmentResult.property_id == subject.property_id,
                        EnrichmentResult.kind == ALTERNATIVE,
                    )
                )
            }
            kept: set[tuple[str, str]] = set()
            for match in matches:
                item = match.listing
                key = (item.source, item.source_id)
                kept.add(key)
                price = item.monthly_price
                cheaper = None
                difference = None
                if (
                    price is not None
                    and subject.price_minor is not None
                    and price.currency == subject.currency
                ):
                    difference = price.amount_minor - subject.price_minor
                    cheaper = difference < 0
                data = {
                    "signals": {name: round(v, 3) for name, v in match.signals.items()},
                    "price_difference_minor": difference,
                }
                row = existing.get(key)
                if row is None:
                    row = EnrichmentResult(
                        property_id=subject.property_id,
                        kind=ALTERNATIVE,
                        source=item.source,
                        source_id=item.source_id,
                    )
                    session.add(row)
                row.url = str(item.url)
                row.title = item.title
                row.monthly_price_minor = None if price is None else price.amount_minor
                row.currency = None if price is None else price.currency
                row.score = round(match.score, 3)
                row.cheaper = cheaper
                row.data = data
            for key, row in existing.items():
                if key not in kept and row.source in searched:
                    session.delete(row)
        log.info(
            "alternatives_found",
            property_id=subject.property_id,
            matches=len(matches),
            sources=searched,
        )
        return len(matches)


def _distance(subject: Subject, item: NormalizedListing) -> float:
    lat, lng = subject.facts.lat, subject.facts.lng
    if lat is None or lng is None or item.lat is None or item.lng is None:
        return math.inf
    return haversine(lat, lng, item.lat, item.lng, DistanceUnit.KILOMETERS)


def register_alternatives(
    jobs: JobRegistry,
    registry: SourceRegistry,
    session_factory: sessionmaker[Session],
    *,
    sources: SourcesSettings,
    grouping: GroupingSettings,
    settings: EnrichmentSettings,
    geocoder: Geocoder,
    fetcher: PhashFetcher | None = None,
    parser: AddressParser | None = None,
) -> None:
    """Register :class:`FindAlternatives` as the ``enrich.alternatives`` job."""
    handler = FindAlternatives(
        session_factory,
        registry,
        sources=sources,
        grouping=grouping,
        settings=settings,
        geocoder=geocoder,
        fetcher=fetcher,
        parser=parser,
    )
    jobs.register(ENRICH_ALTERNATIVES, handler, timeout=timedelta(seconds=settings.timeout_seconds))
