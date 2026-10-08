"""The property detail page at ``/properties/{id}``."""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.listing_store import SqlListingStore
from furnest.db.models import EnrichmentResult, Listing, Property, RiskFlag
from furnest.db.property_store import SqlPropertyStore
from furnest.db.session import session_scope
from furnest.domain import Actor, FilterRule, Money, NormalizedListing, PropertyStatus
from furnest.domain.price import Price
from furnest.matching import BreakdownItem, MatchResult, RuleResult, RuleStatus

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
HTML = {"Accept": "text/html"}

Seed = Callable[..., int]  # (**listing fields) -> property id


@pytest.fixture
def seed(session_factory: sessionmaker[Session]) -> Seed:
    listings = SqlListingStore(session_factory)
    store = SqlPropertyStore(session_factory)
    counter = iter(range(1, 1000))

    def add(**fields: object) -> int:
        n = next(counter)
        listing = NormalizedListing.model_validate(
            {
                "source": "test",
                "source_id": f"l{n}",
                "url": f"https://example.com/l{n}",
                "title": f"Furnished place {n}",
                **fields,
            }
        )
        (listing_id,) = listings.upsert_batch("test", [listing], seen_at=T0).new_ids
        return store.ensure_for_listing(listing_id, at=T0).id

    return add


def get(client: TestClient, url: str) -> str:
    response = client.get(url, headers=HTML)
    assert response.status_code == 200, response.text
    return response.text


def test_requires_login(client: TestClient, seed: Seed) -> None:
    response = client.get(f"/properties/{seed()}", headers=HTML, follow_redirects=False)

    assert response.status_code in (303, 401)


def test_unknown_property_is_404(authed_client: TestClient) -> None:
    response = authed_client.get("/properties/999", headers=HTML)

    assert response.status_code == 404
    assert "Page not found" in response.text


def test_non_numeric_id_is_rejected(authed_client: TestClient) -> None:
    assert authed_client.get("/properties/abc", headers=HTML).status_code == 422


def test_page_shows_everything_about_the_property(
    authed_client: TestClient, seed: Seed, session_factory: sessionmaker[Session]
) -> None:
    property_id = seed(
        title="Sunny loft",
        description="Bright and quiet.\n\nClose to transit.",
        price=Price(amount=Money.from_major(2400)),
        beds=1,
        baths=1,
        sqft=650,
        images=["https://img.example.com/1.jpg", "https://img.example.com/2.jpg"],
    )
    store = SqlPropertyStore(session_factory)
    listing_id = store.detail(property_id).primary.summary.id  # type: ignore[union-attr]
    store.record_match(
        listing_id,
        MatchResult(
            passed=True,
            score=77,
            breakdown=(
                BreakdownItem(label="Price fit", points=30, max_points=40, reason="near the top"),
            ),
            rules=(
                RuleResult(
                    rule=FilterRule.MIN_BEDS,
                    status=RuleStatus.PASS,
                    reason="1 bed",
                    blocking=False,
                ),
            ),
            criteria_version=3,
        ),
        at=T0,
    )
    store.transition(property_id, S.APPROVED, actor=Actor.DASHBOARD, at=T0, detail="me")

    html = get(authed_client, f"/properties/{property_id}")

    assert "<h1>Sunny loft</h1>" in html
    assert "status-approved" in html
    assert "$2,400.00/mo" in html
    assert "1 bd · 1 ba · 650 sqft" in html
    # Gallery: remote photos, no referrer.
    assert 'src="https://img.example.com/1.jpg"' in html
    assert 'src="https://img.example.com/2.jpg"' in html
    assert 'referrerpolicy="no-referrer"' in html
    # Listings with the source link.
    assert 'href="https://example.com/l1"' in html
    assert 'rel="noopener noreferrer"' in html
    # Score breakdown and hard filters.
    assert "77 / 100" in html
    assert "criteria v3" in html
    assert "Price fit" in html
    assert "30 / 40" in html
    assert "min beds" in html
    # Description as paragraphs.
    assert "<p>Bright and quiet.</p><p>Close to transit.</p>" in html
    # Email log and enrichment sections.
    for heading in ("Email log", "Price comparison", "Risk flags"):
        assert heading in html
    # Timeline, newest first.
    assert html.index("suggested → approved") < html.index("new → suggested")
    assert html.index("new → suggested") < html.index("Created as new")
    assert "by dashboard (me)" in html


def test_script_in_a_scraped_description_is_neutralized(
    authed_client: TestClient, seed: Seed
) -> None:
    property_id = seed(
        description=(
            "<p>Nice place</p><script>alert('x')</script>"
            '<img src="https://evil.example.com/x" onerror="alert(1)">'
            '<a href="javascript:alert(2)">deal</a>'
        )
    )

    html = get(authed_client, f"/properties/{property_id}")

    assert "<p>Nice place</p>" in html
    assert "<script>alert" not in html
    assert "alert(" not in html
    assert "onerror" not in html
    assert "javascript:" not in html
    assert "evil.example.com" not in html


def test_plain_title_markup_is_escaped(authed_client: TestClient, seed: Seed) -> None:
    property_id = seed(title="<b>Loft</b>")

    html = get(authed_client, f"/properties/{property_id}")

    assert "&lt;b&gt;Loft&lt;/b&gt;" in html
    assert "<b>Loft</b>" not in html


def test_bare_property_still_renders(
    authed_client: TestClient, seed: Seed, session_factory: sessionmaker[Session]
) -> None:
    property_id = seed()
    with session_scope(session_factory) as session:
        session.execute(update(Listing).values(property_id=None))

    html = get(authed_client, f"/properties/{property_id}")

    assert f"Property #{property_id}" in html
    assert "No photos." in html
    assert "No listings are attached" in html
    assert "Not matched against the criteria yet." in html
    assert "No description." in html


def test_price_history_and_removed_listing(
    authed_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    listings = SqlListingStore(session_factory)

    def listing(price: int) -> NormalizedListing:
        return NormalizedListing.model_validate(
            {
                "source": "test",
                "source_id": "x",
                "url": "https://example.com/x",
                "title": "Place",
                "price": Price(amount=Money.from_major(price)),
            }
        )

    (listing_id,) = listings.upsert_batch("test", [listing(2000)], seen_at=T0).new_ids
    listings.upsert_batch("test", [listing(1900)], seen_at=T0.replace(day=6))
    property_id = SqlPropertyStore(session_factory).ensure_for_listing(listing_id, at=T0).id
    with session_scope(session_factory) as session:
        session.execute(update(Listing).values(removed_at=T0.replace(day=7)))

    html = get(authed_client, f"/properties/{property_id}")

    assert "2 prices" in html
    assert html.index("$1,900.00/mo") < html.index("$2,000.00/mo", html.index("2 prices"))
    assert "removed" in html


def test_snooze_and_manual_contact_notes(
    authed_client: TestClient, seed: Seed, session_factory: sessionmaker[Session]
) -> None:
    property_id = seed()
    store = SqlPropertyStore(session_factory)
    store.transition(property_id, S.SUGGESTED, actor=Actor.SYSTEM, at=T0)
    store.snooze(property_id, until=T0.replace(year=2099), actor=Actor.DASHBOARD, at=T0)
    with session_scope(session_factory) as session:
        session.execute(update(Property).values(manual_contact_required=True))

    html = get(authed_client, f"/properties/{property_id}")

    assert "Snoozed until <time" in html
    assert "Snoozed until 2099-10-05" in html
    assert "Manual contact required" in html


def test_csp_allows_https_images(authed_client: TestClient, seed: Seed) -> None:
    response = authed_client.get(f"/properties/{seed()}", headers=HTML)

    assert "img-src 'self' data: https:" in response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]


def test_dashboard_card_links_to_the_detail_page(
    authed_client: TestClient, seed: Seed, session_factory: sessionmaker[Session]
) -> None:
    property_id = seed()
    SqlPropertyStore(session_factory).transition(
        property_id, S.SUGGESTED, actor=Actor.SYSTEM, at=T0
    )

    html = get(authed_client, "/")

    assert f'href="/properties/{property_id}"' in html


def test_price_history_across_listings(
    authed_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    listings = SqlListingStore(session_factory)

    def listing(source: str, price: int) -> NormalizedListing:
        return NormalizedListing.model_validate(
            {
                "source": source,
                "source_id": "x",
                "url": f"https://example.com/{source}/x",
                "title": "Place",
                "price": Price(amount=Money.from_major(price)),
            }
        )

    (first,) = listings.upsert_batch("test", [listing("test", 2000)], seen_at=T0).new_ids
    listings.upsert_batch("test", [listing("test", 1900)], seen_at=T0.replace(day=6))
    (second,) = listings.upsert_batch(
        "other", [listing("other", 2100)], seen_at=T0.replace(day=7)
    ).new_ids
    property_id = SqlPropertyStore(session_factory).ensure_for_listing(first, at=T0).id
    with session_scope(session_factory) as session:
        session.execute(update(Listing).where(Listing.id == second).values(property_id=property_id))

    html = get(authed_client, f"/properties/{property_id}")

    section = html[html.index('id="prices"') : html.index('id="score"')]
    assert "First $2,000.00/mo · lowest $1,900.00/mo · now $2,100.00/mo" in section
    # Newest first, each with its change from the one before and a link to its listing.
    rows = section[section.index("<tbody>") :]
    assert rows.index("▲ +$200.00") < rows.index("▼ -$100.00")
    assert rows.index("$2,100.00/mo") < rows.index("$1,900.00/mo") < rows.index("$2,000.00/mo")
    assert f'href="#listing-{second}"' in rows


def test_a_single_price_has_no_price_history(authed_client: TestClient, seed: Seed) -> None:
    property_id = seed(price=Price(amount=Money.from_major(2400)))

    assert 'id="prices"' not in get(authed_client, f"/properties/{property_id}")


def test_relisting_events_on_the_timeline(
    authed_client: TestClient, seed: Seed, session_factory: sessionmaker[Session]
) -> None:
    property_id = seed(price=Price(amount=Money.from_major(2400)))
    store = SqlPropertyStore(session_factory)
    store.transition(property_id, S.DENIED, actor=Actor.DASHBOARD, at=T0)
    listing_id = store.detail(property_id).primary.summary.id  # type: ignore[union-attr]
    with session_scope(session_factory) as session:
        session.execute(update(Listing).values(removed_at=T0))
    store.mark_gone([listing_id], at=T0)
    with session_scope(session_factory) as session:
        session.execute(update(Listing).values(removed_at=None))
    store.restore_relisted([listing_id], at=T0.replace(day=8))

    html = get(authed_client, f"/properties/{property_id}")

    assert "All listings removed" in html
    assert f"Relisted (listing #{listing_id}) after 3 days · $2,400.00/mo" in html


def test_enrichment_sections_empty(authed_client: TestClient, seed: Seed) -> None:
    html = get(authed_client, f"/properties/{seed()}")

    assert "No alternative listings found." in html
    assert "No risk signals." in html
    assert "risk-banner" not in html


def test_comparison_risk_banner_and_chart(
    authed_client: TestClient, seed: Seed, session_factory: sessionmaker[Session]
) -> None:
    property_id = seed(price=Price(amount=Money.from_major(2000)))
    with session_scope(session_factory) as session:
        session.add(
            EnrichmentResult(
                property_id=property_id,
                kind="alternative",
                source="other",
                source_id="a1",
                url="https://example.com/a1",
                title="Same <b>unit</b>",
                monthly_price_minor=185000,
                currency="USD",
                score=0.91,
                cheaper=True,
                data={"price_difference_minor": -15000},
            )
        )
        session.add(
            RiskFlag(
                property_id=property_id,
                kind="off_platform_payment",
                severity="high",
                summary="Asks for a wire transfer",
            )
        )
    listings = SqlListingStore(session_factory)
    detail = SqlPropertyStore(session_factory).detail(property_id)
    assert detail is not None and detail.primary is not None
    listing = NormalizedListing.model_validate(
        {
            "source": "test",
            "source_id": detail.primary.listing.source_id,
            "url": "https://example.com/l",
            "title": "Place",
            "price": Price(amount=Money.from_major(1900)),
        }
    )
    listings.upsert_batch("test", [listing], seen_at=T0.replace(day=8))

    html = get(authed_client, f"/properties/{property_id}")

    assert 'class="risk-banner risk-high"' in html
    assert "Asks for a wire transfer" in html
    assert "off platform payment" in html
    assert "$1,850.00/mo" in html
    assert "▼ -$150.00" in html
    assert "91%" in html
    assert "Same &lt;b&gt;unit&lt;/b&gt;" in html
    assert '<svg class="price-chart"' in html


def test_alternative_with_unknown_currency_shows_unknown_price(
    authed_client: TestClient, seed: Seed, session_factory: sessionmaker[Session]
) -> None:
    property_id = seed()
    with session_scope(session_factory) as session:
        session.add(
            EnrichmentResult(
                property_id=property_id,
                kind="alternative",
                source="other",
                source_id="a1",
                url="https://example.com/a1",
                title="Odd currency",
                monthly_price_minor=185000,
                currency="XXZ",
                score=0.5,
                cheaper=None,
                data={"price_difference_minor": -15000},
            )
        )

    html = get(authed_client, f"/properties/{property_id}")

    assert "Odd currency" in html
    assert '<span class="muted">unknown</span>' in html
    assert "▼" not in html
