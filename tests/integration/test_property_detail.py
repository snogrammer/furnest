"""SqlPropertyStore.detail: one property whole, for the detail page."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.listing_store import SqlListingStore
from furnest.db.models import Listing, Property
from furnest.db.property_store import SqlPropertyStore
from furnest.db.session import session_scope
from furnest.domain import Actor, FilterRule, Money, NormalizedListing, PropertyStatus
from furnest.domain.price import Price
from furnest.matching import BreakdownItem, MatchResult, RuleResult, RuleStatus

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)

Upsert = Callable[..., int]  # (source_id, *, seen_at=T0, **listing fields) -> listing id


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlPropertyStore:
    return SqlPropertyStore(session_factory)


@pytest.fixture
def upsert(session_factory: sessionmaker[Session]) -> Upsert:
    listings = SqlListingStore(session_factory)

    def add(source_id: str, *, seen_at: datetime = T0, **fields: object) -> int:
        listing = NormalizedListing.model_validate(
            {
                "source": "test",
                "source_id": source_id,
                "url": f"https://example.com/{source_id}",
                "title": f"Listing {source_id}",
                **fields,
            }
        )
        result = listings.upsert_batch("test", [listing], seen_at=seen_at)
        return next(iter(result.seen_ids))

    return add


def attach(
    session_factory: sessionmaker[Session], listing_id: int, property_id: int | None
) -> None:
    with session_scope(session_factory) as session:
        session.execute(
            update(Listing).where(Listing.id == listing_id).values(property_id=property_id)
        )


def test_unknown_property_is_none(store: SqlPropertyStore) -> None:
    assert store.detail(999) is None


def test_detail_of_a_fresh_property(store: SqlPropertyStore, upsert: Upsert) -> None:
    listing_id = upsert(
        "a",
        description="Sunny.",
        sqft=600,
        images=["https://img.example.com/1.jpg", "https://img.example.com/2.jpg"],
        price=Price(amount=Money.from_major(2000)),
    )
    property_id = store.ensure_for_listing(listing_id, at=T0).id

    detail = store.detail(property_id)

    assert detail is not None
    assert detail.property.id == property_id
    assert detail.property.status is S.NEW
    assert detail.match is None
    assert detail.primary is not None
    assert detail.primary.summary.id == listing_id
    assert detail.primary.summary.monthly_price == Money.from_major(2000)
    assert detail.primary.listing.description == "Sunny."
    assert detail.primary.listing.sqft == 600
    assert [p.price.monthly for p in detail.primary.price_history] == [Money.from_major(2000)]
    assert detail.images == ["https://img.example.com/1.jpg", "https://img.example.com/2.jpg"]
    assert [(e.from_status, e.to_status) for e in detail.audit] == [(None, S.NEW)]


def test_price_history_is_oldest_first(store: SqlPropertyStore, upsert: Upsert) -> None:
    listing_id = upsert("a", price=Price(amount=Money.from_major(2000)))
    upsert("a", seen_at=T0 + HOUR, price=Price(amount=Money.from_major(1800)))
    property_id = store.ensure_for_listing(listing_id, at=T0).id

    detail = store.detail(property_id)

    assert detail is not None and detail.primary is not None
    assert [p.price.monthly for p in detail.primary.price_history] == [
        Money.from_major(2000),
        Money.from_major(1800),
    ]
    assert detail.primary.listing.last_seen == T0 + HOUR


def test_listings_put_active_ones_first_and_merge_photos(
    store: SqlPropertyStore, upsert: Upsert, session_factory: sessionmaker[Session]
) -> None:
    first = upsert("a", images=["https://img.example.com/a.jpg", "https://img.example.com/s.jpg"])
    second = upsert("b", images=["https://img.example.com/s.jpg", "https://img.example.com/b.jpg"])
    third = upsert("c")
    property_id = store.ensure_for_listing(first, at=T0).id
    attach(session_factory, second, property_id)
    attach(session_factory, third, property_id)
    with session_scope(session_factory) as session:
        session.execute(update(Listing).where(Listing.id == first).values(removed_at=T0))

    detail = store.detail(property_id)

    assert detail is not None
    assert [listing.summary.id for listing in detail.listings] == [second, third, first]
    assert detail.primary is not None and detail.primary.summary.id == second
    assert detail.images == [
        "https://img.example.com/s.jpg",
        "https://img.example.com/b.jpg",
        "https://img.example.com/a.jpg",
    ]


def test_match_and_audit_trail(store: SqlPropertyStore, upsert: Upsert) -> None:
    listing_id = upsert("a")
    result = MatchResult(
        passed=True,
        score=82,
        breakdown=(BreakdownItem(label="Price fit", points=40, max_points=50, reason="in range"),),
        rules=(
            RuleResult(
                rule=FilterRule.PRICE, status=RuleStatus.PASS, reason="in budget", blocking=False
            ),
        ),
    )
    property_id = store.record_match(listing_id, result, at=T0).id
    store.transition(property_id, S.APPROVED, actor=Actor.DASHBOARD, at=T0 + HOUR)

    detail = store.detail(property_id)

    assert detail is not None
    assert detail.match == result
    assert [(e.from_status, e.to_status) for e in detail.audit] == [
        (None, S.NEW),
        (S.NEW, S.SUGGESTED),
        (S.SUGGESTED, S.APPROVED),
    ]


def test_unreadable_stored_match_is_none(
    store: SqlPropertyStore, upsert: Upsert, session_factory: sessionmaker[Session]
) -> None:
    property_id = store.ensure_for_listing(upsert("a"), at=T0).id
    with session_scope(session_factory) as session:
        session.execute(
            update(Property).where(Property.id == property_id).values(match_result={"x": 1})
        )

    detail = store.detail(property_id)

    assert detail is not None and detail.match is None


def test_property_without_listings(
    store: SqlPropertyStore, upsert: Upsert, session_factory: sessionmaker[Session]
) -> None:
    listing_id = upsert("a")
    property_id = store.ensure_for_listing(listing_id, at=T0).id
    attach(session_factory, listing_id, None)

    detail = store.detail(property_id)

    assert detail is not None
    assert detail.listings == []
    assert detail.primary is None
    assert detail.images == []
