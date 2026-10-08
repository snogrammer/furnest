"""The risk check: queued on approval, evaluates the signals, stores ``risk_flags``."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import RiskSettings
from furnest.db.image_store import SqlImageStore
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import Job, JobStatus, RiskFlag
from furnest.db.property_store import ENRICH_RISK, SqlPropertyStore
from furnest.db.session import session_scope
from furnest.domain import Actor, Money, NormalizedListing, Price, PropertyStatus
from furnest.enrichment.risk import (
    OFF_PLATFORM_PAYMENT,
    PHOTO_REUSE,
    PRICE_BELOW_MARKET,
    CheckRisk,
    register_risk,
)
from furnest.worker.execution import JobContext, JobRegistry

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
HASH = "ee9d91626a159595"
OTHER_HASH = "f5989e69c93230c7"
ADDRESS = {
    "line1": "123 North Main Street",
    "city": "Springfield",
    "region": "IL",
    "postal_code": "62701",
}
ELSEWHERE = {
    "line1": "987 South Elm Avenue",
    "city": "Springfield",
    "region": "IL",
    "postal_code": "62704",
}


def listing(source_id: str, price: int, **fields: Any) -> NormalizedListing:  # noqa: ANN401
    return NormalizedListing.model_validate(
        {
            "source": "test",
            "source_id": source_id,
            "url": f"https://test.example.com/{source_id}",
            "title": "Sunny furnished 1BR near downtown",
            "lat": 40.0,
            "lng": -105.0,
            "beds": 1,
            "address": ADDRESS,
            "price": Price(amount=Money.from_major(price)),
            **fields,
        }
    )


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlPropertyStore:
    return SqlPropertyStore(session_factory)


Approve = Callable[..., int]  # (price, **listing fields) -> property id, approved


@pytest.fixture
def approve(session_factory: sessionmaker[Session], store: SqlPropertyStore) -> Approve:
    def make(price: int = 2000, **fields: Any) -> int:  # noqa: ANN401
        own = listing("own", price, images=["https://img.example.com/own.jpg"], **fields)
        (listing_id,) = (
            SqlListingStore(session_factory).upsert_batch("test", [own], seen_at=T0).new_ids
        )
        SqlImageStore(session_factory).record("https://img.example.com/own.jpg", phash=HASH, at=T0)
        state = store.ensure_for_listing(listing_id, at=T0)
        store.transition(state.id, S.SUGGESTED, actor=Actor.SYSTEM, at=T0)
        store.transition(state.id, S.APPROVED, actor=Actor.DASHBOARD, at=T0)
        return state.id

    return make


def add_comparables(
    session_factory: sessionmaker[Session],
    *prices: int,
    **fields: Any,  # noqa: ANN401
) -> None:
    tag = "-".join(f"{key}{value}" for key, value in sorted(fields.items()))
    items = [listing(f"cmp{i}-{tag}", price, **fields) for i, price in enumerate(prices)]
    SqlListingStore(session_factory).upsert_batch("test", items, seen_at=T0)


def add_photo_thief(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, phash: str
) -> None:
    url = "https://img.example.com/thief.jpg"
    item = listing("thief", 2000, address=ELSEWHERE, images=[url])
    (listing_id,) = (
        SqlListingStore(session_factory).upsert_batch("test", [item], seen_at=T0).new_ids
    )
    SqlImageStore(session_factory).record(url, phash=phash, at=T0)
    store.ensure_for_listing(listing_id, at=T0)


def checker(session_factory: sessionmaker[Session], **settings: Any) -> CheckRisk:  # noqa: ANN401
    return CheckRisk(session_factory, RiskSettings(**settings))


def flags(session_factory: sessionmaker[Session]) -> dict[str, RiskFlag]:
    with session_scope(session_factory) as session:
        rows = session.scalars(select(RiskFlag))
        return {row.kind: row for row in rows}


def test_approving_queues_the_check_once(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, approve: Approve
) -> None:
    property_id = approve()
    store.transition(property_id, S.INQUIRY_DRAFTED, actor=Actor.SYSTEM, at=T0)
    store.transition(property_id, S.APPROVED, actor=Actor.DASHBOARD, at=T0)

    with session_scope(session_factory) as session:
        (job,) = session.scalars(select(Job).where(Job.kind == ENRICH_RISK))
    assert job.payload == {"property_id": property_id}
    assert job.status is JobStatus.QUEUED


def test_a_normal_listing_raises_nothing(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve(2000)
    add_comparables(session_factory, 1900, 2000, 2100)

    assert checker(session_factory).run(property_id) == []
    assert flags(session_factory) == {}


def test_price_far_below_comparables_is_flagged(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve(900)
    add_comparables(session_factory, 1900, 2000, 2100)
    # A different bedroom count and a faraway listing don't count.
    add_comparables(session_factory, 5000, beds=2)
    add_comparables(session_factory, 5000, lat=41.0)

    checker(session_factory).run(property_id)

    flag = flags(session_factory)[PRICE_BELOW_MARKET]
    assert flag.property_id == property_id
    assert flag.severity == "medium"
    assert flag.detail["comparables"] == 3
    assert flag.detail["median_minor"] == 200_000


def test_price_check_needs_enough_comparables(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve(100)
    add_comparables(session_factory, 2000, 2100)

    checker(session_factory).run(property_id)

    assert PRICE_BELOW_MARKET not in flags(session_factory)


def test_price_threshold_comes_from_settings(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve(1500)
    add_comparables(session_factory, 2000, 2000, 2000)

    checker(session_factory).run(property_id)
    assert flags(session_factory) == {}

    checker(session_factory, price_below_median_pct=20).run(property_id)
    assert PRICE_BELOW_MARKET in flags(session_factory)


def test_photos_at_a_different_address_are_flagged(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, approve: Approve
) -> None:
    property_id = approve()
    add_photo_thief(session_factory, store, HASH)

    checker(session_factory).run(property_id)

    flag = flags(session_factory)[PHOTO_REUSE]
    assert flag.severity == "high"
    assert flag.detail["photos_matched"] == 1
    assert len(flag.detail["other_addresses"]) == 1


def test_different_photos_or_the_same_address_are_not_flagged(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, approve: Approve
) -> None:
    property_id = approve()
    add_photo_thief(session_factory, store, OTHER_HASH)
    # The same unit listed elsewhere: same address, same photo.
    url = "https://img.example.com/same.jpg"
    same = listing("same", 2000, images=[url])
    (same_id,) = SqlListingStore(session_factory).upsert_batch("test", [same], seen_at=T0).new_ids
    SqlImageStore(session_factory).record(url, phash=HASH, at=T0)
    store.ensure_for_listing(same_id, at=T0)

    checker(session_factory).run(property_id)

    assert PHOTO_REUSE not in flags(session_factory)


def test_payment_language_is_flagged(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve(description="Deposit by wire transfer or gift cards only.")

    checker(session_factory).run(property_id)

    flag = flags(session_factory)[OFF_PLATFORM_PAYMENT]
    assert flag.severity == "high"
    assert set(flag.detail["matches"]) == {"wire transfer", "gift card"}


def test_rerun_updates_in_place_and_clears_flags_that_no_longer_apply(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve(description="Please pay by gift card.")
    check = checker(session_factory)
    check.run(property_id)
    (first,) = flags(session_factory).values()

    check.run(property_id)
    (again,) = flags(session_factory).values()
    assert again.id == first.id

    SqlListingStore(session_factory).upsert_batch(
        "test", [listing("own", 2000, description="Pay on lease signing.")], seen_at=T0
    )
    check.run(property_id)
    assert flags(session_factory) == {}


def test_disabled_and_unenrichable_properties_are_skipped(
    session_factory: sessionmaker[Session], store: SqlPropertyStore, approve: Approve
) -> None:
    property_id = approve(description="Please pay by gift card.")

    assert checker(session_factory, enabled=False).run(property_id) == []
    assert checker(session_factory).run(property_id + 100) == []
    store.transition(property_id, S.ARCHIVED, actor=Actor.DASHBOARD, at=T0)
    assert checker(session_factory).run(property_id) == []
    assert flags(session_factory) == {}


def test_registered_as_a_job_handler(
    session_factory: sessionmaker[Session], approve: Approve
) -> None:
    property_id = approve(description="Western Union only")
    registry = JobRegistry()
    register_risk(registry, session_factory, RiskSettings())

    registration = registry.get(ENRICH_RISK)

    assert registration is not None
    assert registration.timeout == timedelta(seconds=120)
    registration.handler(
        JobContext(
            job_id=1,
            kind=ENRICH_RISK,
            payload={"property_id": property_id},
            attempt=1,
            deadline=T0 + timedelta(hours=1),
        )
    )
    assert OFF_PLATFORM_PAYMENT in flags(session_factory)
