"""The alternatives finder: queued on approval, searches other sources, stores matches."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import EnrichmentSettings, GroupingSettings, SourcesSettings
from furnest.db.image_store import SqlImageStore
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import EnrichmentResult, Job, JobStatus
from furnest.db.property_store import ENRICH_ALTERNATIVES, SqlPropertyStore
from furnest.db.session import session_scope
from furnest.db.source_store import SqlSourceStore
from furnest.dedup.phash import ImageFetchError
from furnest.domain import (
    Actor,
    Address,
    Criteria,
    Money,
    NormalizedListing,
    Price,
    PropertyStatus,
)
from furnest.enrichment.alternatives import FindAlternatives, register_alternatives
from furnest.enrichment.geocoding import GeocodePrecision, GeocodeResult
from furnest.sources import SourceAdapter, SourceRegistry
from furnest.worker.execution import JobContext, JobRegistry

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
PHOTO = "https://img.example.com/own.jpg"
OTHER_PHOTO = "https://img.example.com/other.jpg"
HASH = "ee9d91626a159595"
OTHER_HASH = "f5989e69c93230c7"
ADDRESS = {
    "line1": "123 North Main Street",
    "city": "Springfield",
    "region": "IL",
    "postal_code": "62701",
}
TITLE = "Sunny furnished 1BR near downtown"


def listing(source: str, source_id: str, **fields: Any) -> NormalizedListing:  # noqa: ANN401
    return NormalizedListing.model_validate(
        {
            "source": source,
            "source_id": source_id,
            "url": f"https://{source}.example.com/{source_id}",
            "title": TITLE,
            "lat": 40.0,
            "lng": -105.0,
            "beds": 1,
            "address": ADDRESS,
            **fields,
        }
    )


def money(major: int) -> Price:
    return Price(amount=Money.from_major(major))


class FakeFetcher:
    def __init__(self, hashes: dict[str, str]) -> None:
        self.hashes = hashes
        self.fetched: list[str] = []

    def fetch_phash(self, url: str) -> str:
        self.fetched.append(url)
        if url not in self.hashes:
            raise ImageFetchError("HTTP 404", url=url)
        return self.hashes[url]


class FakeGeocoder:
    def __init__(self, result: GeocodeResult | None) -> None:
        self.result = result
        self.asked: list[Address] = []

    def geocode(self, address: Address) -> GeocodeResult | None:
        self.asked.append(address)
        return self.result


def geocoded(precision: GeocodePrecision) -> GeocodeResult:
    return GeocodeResult(
        lat=40.0,
        lng=-105.0,
        address=Address(**ADDRESS),
        display_name="123 North Main Street, Springfield",
        precision=precision,
        provider="fake",
    )


class Source:
    """A fake source: the adapter class registered, and what its searches did."""

    def __init__(
        self,
        key: str,
        results: list[NormalizedListing] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.results = results or []
        self.error = error
        self.criteria: list[Criteria] = []
        outer = self

        class Adapter(SourceAdapter):
            name: ClassVar[str] = key
            display_name: ClassVar[str] = key.title()
            default_enabled: ClassVar[bool] = True

            def search(self, criteria: Criteria) -> list[NormalizedListing]:
                outer.criteria.append(criteria)
                if outer.error is not None:
                    raise outer.error
                return list(outer.results)

        self.adapter = Adapter


def registry_of(*sources: Source) -> SourceRegistry:
    registry = SourceRegistry(browser_check=lambda _settings: None)
    for source in sources:
        registry.register(source.adapter)
    return registry


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlPropertyStore:
    return SqlPropertyStore(session_factory)


Approve = Callable[..., int]  # (**own listing fields) -> property id, approved


@pytest.fixture
def approve(session_factory: sessionmaker[Session], store: SqlPropertyStore) -> Approve:
    def make(**fields: Any) -> int:  # noqa: ANN401
        own = listing("test", "own", price=money(2000), images=[PHOTO], **fields)
        (listing_id,) = (
            SqlListingStore(session_factory).upsert_batch("test", [own], seen_at=T0).new_ids
        )
        SqlImageStore(session_factory).record(PHOTO, phash=HASH, at=T0)
        state = store.ensure_for_listing(listing_id, at=T0)
        store.transition(state.id, S.SUGGESTED, actor=Actor.SYSTEM, at=T0)
        store.transition(state.id, S.APPROVED, actor=Actor.DASHBOARD, at=T0)
        return state.id

    return make


def finder(
    session_factory: sessionmaker[Session],
    *sources: Source,
    geocoder: FakeGeocoder | None = None,
    fetcher: FakeFetcher | None = None,
    **settings: Any,  # noqa: ANN401
) -> FindAlternatives:
    return FindAlternatives(
        session_factory,
        registry_of(*sources),
        sources=SourcesSettings(),
        grouping=GroupingSettings(),
        settings=EnrichmentSettings(**settings),
        geocoder=geocoder or FakeGeocoder(None),
        fetcher=fetcher,
    )


def results(session_factory: sessionmaker[Session]) -> list[EnrichmentResult]:
    with session_scope(session_factory) as session:
        return list(session.scalars(select(EnrichmentResult).order_by(EnrichmentResult.id)))


def jobs(session_factory: sessionmaker[Session]) -> list[Job]:
    with session_scope(session_factory) as session:
        return list(session.scalars(select(Job).where(Job.kind == ENRICH_ALTERNATIVES)))


# --- Enqueueing ---------------------------------------------------------------------


def test_approving_queues_the_finder_once(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, approve: Approve
) -> None:
    property_id = approve()

    (job,) = jobs(session_factory)
    assert job.payload == {"property_id": property_id}
    assert job.status is JobStatus.QUEUED

    # Drafting an inquiry and throwing the draft away approves again; the job is still waiting.
    store.transition(property_id, S.INQUIRY_DRAFTED, actor=Actor.SYSTEM, at=T0)
    store.transition(property_id, S.APPROVED, actor=Actor.DASHBOARD, at=T0)
    assert len(jobs(session_factory)) == 1


def test_other_transitions_queue_nothing(
    session_factory: sessionmaker[Session],
    store: SqlPropertyStore,
) -> None:
    own = listing("test", "own")
    (listing_id,) = SqlListingStore(session_factory).upsert_batch("test", [own], seen_at=T0).new_ids
    state = store.ensure_for_listing(listing_id, at=T0)

    store.transition(state.id, S.SUGGESTED, actor=Actor.SYSTEM, at=T0)
    store.transition(state.id, S.DENIED, actor=Actor.DASHBOARD, at=T0)

    assert jobs(session_factory) == []


# --- Finding ------------------------------------------------------------------------


def test_stores_a_cheaper_alternative_from_another_source(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()
    cheaper = listing("elsewhere", "x1", price=money(1800), images=[OTHER_PHOTO])
    elsewhere = Source("elsewhere", [cheaper])
    fetcher = FakeFetcher({OTHER_PHOTO: HASH})

    found = finder(session_factory, elsewhere, fetcher=fetcher).run(property_id)

    assert found == 1
    (row,) = results(session_factory)
    assert row.property_id == property_id
    assert (row.kind, row.source, row.source_id) == ("alternative", "elsewhere", "x1")
    assert row.url == "https://elsewhere.example.com/x1"
    assert row.title == TITLE
    assert (row.monthly_price_minor, row.currency) == (180_000, "USD")
    assert row.cheaper is True
    assert row.data["price_difference_minor"] == -20_000
    assert row.data["signals"]["photos"] == 1.0
    assert row.score == pytest.approx(1.0, abs=0.01)


def test_a_dearer_alternative_is_stored_but_not_cheaper(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()
    dearer = Source("elsewhere", [listing("elsewhere", "x1", price=money(2500))])

    finder(session_factory, dearer).run(property_id)

    (row,) = results(session_factory)
    assert row.cheaper is False
    assert row.data["price_difference_minor"] == 50_000


def test_an_alternative_without_a_price_is_stored_with_unknown_cheaper(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()
    unpriced = Source("elsewhere", [listing("elsewhere", "x1")])

    finder(session_factory, unpriced).run(property_id)

    (row,) = results(session_factory)
    assert (row.monthly_price_minor, row.cheaper) == (None, None)


def test_searches_around_the_property_with_the_configured_radius(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()
    elsewhere = Source("elsewhere")

    finder(session_factory, elsewhere, search_radius_km=2.5).run(property_id)

    (criteria,) = elsewhere.criteria
    (area,) = criteria.areas
    assert (area.lat, area.lng, area.radius, area.unit.value) == (40.0, -105.0, 2.5, "km")
    assert criteria.min_beds == 1
    assert criteria.furnished_required is False


def test_ignores_candidates_that_are_not_the_same_unit(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve(address={**ADDRESS, "line2": "Apt 4"})
    elsewhere = Source(
        "elsewhere",
        [
            listing("elsewhere", "far", lat=40.1),
            listing("elsewhere", "two-beds", beds=2),
            listing("elsewhere", "other-unit", address={**ADDRESS, "line2": "Apt 9"}),
            listing(
                "elsewhere",
                "nothing-alike",
                title="Quiet cabin",
                address={"line1": "9 Elm Ave", "city": "Springfield", "region": "IL"},
            ),
        ],
    )

    assert finder(session_factory, elsewhere).run(property_id) == 0
    assert results(session_factory) == []


def test_skips_the_properties_own_sources(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()
    own_source = Source("test", [listing("test", "dupe")])
    elsewhere = Source("elsewhere", [listing("elsewhere", "x1")])

    finder(session_factory, own_source, elsewhere).run(property_id)

    assert own_source.criteria == []
    assert [row.source for row in results(session_factory)] == ["elsewhere"]


def test_skips_sources_switched_off_on_the_dashboard(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()
    off = Source("off", [listing("off", "x1")])
    SqlSourceStore(session_factory).ensure(["off"])
    SqlSourceStore(session_factory).set_enabled("off", False)

    finder(session_factory, off).run(property_id)

    assert off.criteria == []
    assert results(session_factory) == []


def test_photos_are_only_fetched_for_candidates_that_survive_the_vetoes(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()
    fetcher = FakeFetcher({OTHER_PHOTO: OTHER_HASH})
    elsewhere = Source(
        "elsewhere",
        [
            listing("elsewhere", "far", lat=40.1, images=["https://img.example.com/far.jpg"]),
            listing("elsewhere", "other-photo", images=[OTHER_PHOTO]),
            listing("elsewhere", "no-photo", images=["https://img.example.com/404.jpg"]),
        ],
    )

    finder(session_factory, elsewhere, fetcher=fetcher).run(property_id)

    assert fetcher.fetched == [OTHER_PHOTO, "https://img.example.com/404.jpg"]
    # A different photo outweighs the shared address and title; a photo that can't be
    # fetched is no evidence either way.
    (row,) = results(session_factory)
    assert row.source_id == "no-photo"
    assert "photos" not in row.data["signals"]


def test_no_photos_are_fetched_when_the_limit_is_zero(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()
    fetcher = FakeFetcher({OTHER_PHOTO: HASH})
    elsewhere = Source("elsewhere", [listing("elsewhere", "x1", images=[OTHER_PHOTO])])

    finder(session_factory, elsewhere, fetcher=fetcher, photos_per_candidate=0).run(property_id)

    assert fetcher.fetched == []
    assert "photos" not in results(session_factory)[0].data["signals"]


def test_at_most_max_candidates_nearest_first_are_considered(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()
    elsewhere = Source(
        "elsewhere",
        [
            listing("elsewhere", "a-farther", lat=40.0004),
            listing("elsewhere", "b-nearest"),
        ],
    )

    finder(session_factory, elsewhere, max_candidates_per_source=1).run(property_id)

    assert [row.source_id for row in results(session_factory)] == ["b-nearest"]


# --- Re-runs ------------------------------------------------------------------------


def test_a_rerun_updates_in_place_and_drops_what_is_gone(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()
    elsewhere = Source(
        "elsewhere", [listing("elsewhere", "x1", price=money(1900)), listing("elsewhere", "x2")]
    )
    job = finder(session_factory, elsewhere)
    job.run(property_id)
    first = {row.source_id: row.id for row in results(session_factory)}

    elsewhere.results = [listing("elsewhere", "x1", price=money(1700))]
    assert job.run(property_id) == 1

    (row,) = results(session_factory)
    assert (row.source_id, row.id) == ("x1", first["x1"])
    assert row.monthly_price_minor == 170_000


def test_a_failing_source_keeps_its_earlier_results(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()
    flaky = Source("flaky", [listing("flaky", "f1")])
    steady = Source("steady", [listing("steady", "s1")])
    job = finder(session_factory, flaky, steady)
    job.run(property_id)

    flaky.error = RuntimeError("blocked")
    steady.results = []
    job.run(property_id)

    assert [(row.source, row.source_id) for row in results(session_factory)] == [("flaky", "f1")]


def test_every_source_failing_raises_so_the_queue_retries(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()
    broken = Source("broken", error=RuntimeError("blocked"))

    with pytest.raises(RuntimeError, match="every source failed"):
        finder(session_factory, broken).run(property_id)


def test_no_other_sources_is_not_an_error(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()

    assert finder(session_factory).run(property_id) == 0


# --- Skipping -----------------------------------------------------------------------


def test_does_nothing_when_disabled(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()
    elsewhere = Source("elsewhere", [listing("elsewhere", "x1")])

    assert finder(session_factory, elsewhere, enabled=False).run(property_id) == 0
    assert elsewhere.criteria == []


def test_skips_a_property_that_is_no_longer_approved(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, approve: Approve
) -> None:
    property_id = approve()
    store.transition(property_id, S.DENIED, actor=Actor.DASHBOARD, at=T0)
    elsewhere = Source("elsewhere", [listing("elsewhere", "x1")])

    assert finder(session_factory, elsewhere).run(property_id) == 0
    assert elsewhere.criteria == []


def test_skips_a_property_that_does_not_exist(session_factory: sessionmaker[Session]) -> None:
    assert finder(session_factory).run(404) == 0


# --- Placing the property -----------------------------------------------------------


def test_geocodes_a_property_without_coordinates(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve(lat=None, lng=None)
    geocoder = FakeGeocoder(geocoded(GeocodePrecision.STREET))
    elsewhere = Source("elsewhere", [listing("elsewhere", "x1")])

    finder(session_factory, elsewhere, geocoder=geocoder).run(property_id)

    assert [a.line1 for a in geocoder.asked] == ["123 North Main Street"]
    (area,) = elsewhere.criteria[0].areas
    assert (area.lat, area.lng) == (40.0, -105.0)
    assert len(results(session_factory)) == 1


def test_does_not_geocode_when_the_property_has_coordinates(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()
    geocoder = FakeGeocoder(geocoded(GeocodePrecision.ADDRESS))

    finder(session_factory, Source("elsewhere"), geocoder=geocoder).run(property_id)

    assert geocoder.asked == []


@pytest.mark.parametrize("result", [None, geocoded(GeocodePrecision.LOCALITY)])
def test_does_not_search_around_a_city_centroid_or_nothing(
    session_factory: sessionmaker[Session], approve: Approve, result: GeocodeResult | None
) -> None:
    property_id = approve(lat=None, lng=None)
    elsewhere = Source("elsewhere", [listing("elsewhere", "x1")])

    found = finder(session_factory, elsewhere, geocoder=FakeGeocoder(result)).run(property_id)

    assert found == 0
    assert elsewhere.criteria == []


# --- As a job -----------------------------------------------------------------------


def test_registered_as_a_job_handler(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve()
    elsewhere = Source("elsewhere", [listing("elsewhere", "x1")])
    jobs_registry = JobRegistry()
    register_alternatives(
        jobs_registry,
        registry_of(elsewhere),
        session_factory,
        sources=SourcesSettings(),
        grouping=GroupingSettings(),
        settings=EnrichmentSettings(timeout_seconds=120),
        geocoder=FakeGeocoder(None),
    )

    registration = jobs_registry.get(ENRICH_ALTERNATIVES)

    assert registration is not None
    assert registration.timeout == timedelta(seconds=120)
    registration.handler(
        JobContext(
            job_id=1,
            kind=ENRICH_ALTERNATIVES,
            payload={"property_id": property_id},
            attempt=1,
            deadline=T0 + timedelta(hours=1),
        )
    )
    assert [row.source_id for row in results(session_factory)] == ["x1"]
