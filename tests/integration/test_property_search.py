"""SqlPropertyStore.search and status_counts: the dashboard's reads."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.listing_store import SqlListingStore
from furnest.db.models import Listing, Property
from furnest.db.property_store import PropertySort, SqlPropertyStore
from furnest.db.session import session_scope
from furnest.domain import Money, NormalizedListing, PropertyStatus
from furnest.domain.price import Price

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)

Seed = Callable[..., int]  # (title, **fields) -> property id


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlPropertyStore:
    return SqlPropertyStore(session_factory)


@pytest.fixture
def seed(session_factory: sessionmaker[Session], store: SqlPropertyStore) -> Seed:
    """Add a listing and its property, then set the property's fields directly."""
    listings = SqlListingStore(session_factory)
    counter = iter(range(1, 1000))

    def add(
        title: str = "Furnished place",
        *,
        price: int | None = None,
        status: PropertyStatus = S.SUGGESTED,
        score: int | None = None,
        address: str | None = None,
        at: datetime = T0,
        snoozed_until: datetime | None = None,
    ) -> int:
        source_id = f"l{next(counter)}"
        listing = NormalizedListing.model_validate(
            {
                "source": "test",
                "source_id": source_id,
                "url": f"https://example.com/{source_id}",
                "title": title,
                "price": None if price is None else Price(amount=Money.from_major(price)),
                "beds": 2,
                "baths": 1.5,
            }
        )
        (listing_id,) = listings.upsert_batch("test", [listing], seen_at=at).new_ids
        property_id = store.ensure_for_listing(listing_id, at=at).id
        with session_scope(session_factory) as session:
            session.execute(
                update(Property)
                .where(Property.id == property_id)
                .values(status=status, score=score, address=address, snoozed_until=snoozed_until)
            )
        return property_id

    return add


def ids(store: SqlPropertyStore, **kwargs: object) -> list[int]:
    page = store.search(now=T0, limit=50, **kwargs)  # type: ignore[arg-type]
    return [row.property.id for row in page.rows]


def test_rows_carry_the_listing_summary(store: SqlPropertyStore, seed: Seed) -> None:
    pid = seed("Sunny loft", price=2400, score=70, address="1 Main St, Springfield, IL")

    page = store.search(now=T0, limit=10)

    assert page.total == 1
    [row] = page.rows
    assert row.property.id == pid
    assert row.property.score == 70
    assert row.listing is not None
    assert row.listing.title == "Sunny loft"
    assert row.listing.source == "test"
    assert row.listing.url.startswith("https://example.com/")
    assert row.listing.monthly_price == Money.from_major(2400)
    assert (row.listing.beds, row.listing.baths) == (2, 1.5)


def test_property_without_listings_has_no_summary(
    store: SqlPropertyStore, seed: Seed, session_factory: sessionmaker[Session]
) -> None:
    pid = seed()
    with session_scope(session_factory) as session:
        session.execute(update(Listing).values(property_id=None))

    [row] = store.search(now=T0, limit=10).rows

    assert row.property.id == pid
    assert row.listing is None


def test_unknown_price_has_no_money(store: SqlPropertyStore, seed: Seed) -> None:
    seed(price=None)

    [row] = store.search(now=T0, limit=10).rows

    assert row.listing is not None
    assert row.listing.monthly_price is None


def test_status_filter(store: SqlPropertyStore, seed: Seed) -> None:
    suggested = seed(status=S.SUGGESTED)
    approved = seed(status=S.APPROVED)
    seed(status=S.DENIED)

    assert ids(store, statuses=[S.SUGGESTED]) == [suggested]
    assert set(ids(store, statuses=[S.SUGGESTED, S.APPROVED])) == {suggested, approved}
    assert ids(store, statuses=[]) == []


def test_text_matches_title_or_address_case_insensitively(
    store: SqlPropertyStore, seed: Seed
) -> None:
    by_title = seed("Cozy Cabin")
    by_address = seed("Plain flat", address="9 Lakeview Dr, Cabin Creek, CO")
    seed("Something else")

    assert set(ids(store, text="cabin")) == {by_title, by_address}
    assert ids(store, text="LAKEVIEW") == [by_address]
    assert ids(store, text="nothing like it") == []


def test_text_wildcards_are_literal(store: SqlPropertyStore, seed: Seed) -> None:
    percent = seed("100% furnished")
    seed("Fully furnished")
    underscore = seed("unit_b")
    seed("unit-b")

    assert ids(store, text="100%") == [percent]
    assert ids(store, text="%") == [percent]
    assert ids(store, text="unit_b") == [underscore]


def test_sort_by_score_puts_unscored_last(store: SqlPropertyStore, seed: Seed) -> None:
    low = seed(score=10)
    unscored = seed(score=None)
    high = seed(score=90)

    assert ids(store, sort=PropertySort.SCORE) == [high, low, unscored]


def test_sort_by_price_puts_unknown_last(store: SqlPropertyStore, seed: Seed) -> None:
    dear = seed(price=3000)
    unknown = seed(price=None)
    cheap = seed(price=1500)

    assert ids(store, sort=PropertySort.PRICE) == [cheap, dear, unknown]


def test_sort_newest(store: SqlPropertyStore, seed: Seed) -> None:
    old = seed(at=T0 - 2 * HOUR)
    new = seed(at=T0 - HOUR)

    assert ids(store, sort=PropertySort.NEWEST) == [new, old]


def test_ties_fall_back_to_newest_then_id(store: SqlPropertyStore, seed: Seed) -> None:
    first = seed(score=50)
    second = seed(score=50)
    older = seed(score=50, at=T0 - HOUR)

    assert ids(store, sort=PropertySort.SCORE) == [second, first, older]


def test_pages_are_stable_and_report_the_total(store: SqlPropertyStore, seed: Seed) -> None:
    every = [seed(score=50) for _ in range(5)]

    first = store.search(now=T0, limit=2, offset=0)
    second = store.search(now=T0, limit=2, offset=2)
    third = store.search(now=T0, limit=2, offset=4)

    assert first.total == second.total == third.total == 5
    paged = [row.property.id for page in (first, second, third) for row in page.rows]
    assert paged == list(reversed(every))


def test_snoozed_hidden_unless_included(store: SqlPropertyStore, seed: Seed) -> None:
    shown = seed()
    snoozed = seed(snoozed_until=T0 + HOUR)
    expired = seed(snoozed_until=T0)

    assert set(ids(store)) == {shown, expired}
    assert set(ids(store, include_snoozed=True)) == {shown, snoozed, expired}
    assert store.search(now=T0, limit=1).total == 2


def test_primary_listing_prefers_an_active_one(
    store: SqlPropertyStore, seed: Seed, session_factory: sessionmaker[Session]
) -> None:
    pid = seed("Removed one")
    other = seed("Active one")
    with session_scope(session_factory) as session:
        # Group the second listing under the first property, then remove the first.
        session.execute(
            update(Listing).where(Listing.title == "Active one").values(property_id=pid)
        )
        session.execute(update(Listing).where(Listing.title == "Removed one").values(removed_at=T0))
        session.execute(update(Property).where(Property.id == other).values(status=S.ARCHIVED))

    [row] = store.search(now=T0, limit=10, statuses=[S.SUGGESTED]).rows

    assert row.listing is not None
    assert row.listing.title == "Active one"
    assert ids(store, statuses=[S.SUGGESTED], text="removed") == []


def test_status_counts_follow_text_and_snooze(store: SqlPropertyStore, seed: Seed) -> None:
    seed("Loft A", status=S.SUGGESTED)
    seed("Loft B", status=S.SUGGESTED)
    seed("Loft C", status=S.APPROVED, snoozed_until=T0 + HOUR)
    seed("House", status=S.DENIED)

    assert store.status_counts(now=T0) == {S.SUGGESTED: 2, S.DENIED: 1}
    assert store.status_counts(now=T0, include_snoozed=True) == {
        S.SUGGESTED: 2,
        S.APPROVED: 1,
        S.DENIED: 1,
    }
    assert store.status_counts(now=T0, text="loft", include_snoozed=True) == {
        S.SUGGESTED: 2,
        S.APPROVED: 1,
    }
