"""The dashboard at ``/``: tabs, search, sort, paging, list and board, htmx fragments."""

import re
from collections.abc import Callable
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.base import utcnow
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import Property
from furnest.db.property_store import SqlPropertyStore
from furnest.db.session import session_scope
from furnest.domain import Money, NormalizedListing, OwnerType, PropertyStatus
from furnest.domain.price import Price
from furnest.web.property_views import PAGE_SIZE, Filters

pytestmark = pytest.mark.integration

S = PropertyStatus
HTML = {"Accept": "text/html"}
HTMX = {"Accept": "text/html", "HX-Request": "true"}

Seed = Callable[..., int]  # (title, **fields) -> property id


@pytest.fixture
def seed(session_factory: sessionmaker[Session]) -> Seed:
    listings = SqlListingStore(session_factory)
    store = SqlPropertyStore(session_factory)
    counter = iter(range(1, 1000))
    now = utcnow()

    def add(
        title: str = "Furnished place",
        *,
        price: int | None = 2000,
        status: PropertyStatus = S.SUGGESTED,
        score: int | None = 50,
        age: timedelta = timedelta(0),
        snoozed: bool = False,
        source: str = "test",
        owner: OwnerType = OwnerType.UNKNOWN,
        images: tuple[str, ...] = (),
    ) -> int:
        n = next(counter)
        at = now - age - timedelta(seconds=n)
        listing = NormalizedListing.model_validate(
            {
                "source": source,
                "source_id": f"l{n}",
                "url": f"https://example.com/l{n}",
                "title": title,
                "price": None if price is None else Price(amount=Money.from_major(price)),
                "beds": 0,
                "baths": 1,
                "images": list(images),
            }
        )
        (listing_id,) = listings.upsert_batch(source, [listing], seen_at=at).new_ids
        property_id = store.ensure_for_listing(listing_id, at=at).id
        with session_scope(session_factory) as session:
            session.execute(
                update(Property)
                .where(Property.id == property_id)
                .values(
                    status=status,
                    score=score,
                    snoozed_until=now + timedelta(days=1) if snoozed else None,
                    owner_type=owner,
                )
            )
        return property_id

    return add


def card_ids(html: str) -> list[int]:
    return [int(n) for n in re.findall(r'id="property-(\d+)"', html)]


def get(client: TestClient, url: str = "/", headers: dict[str, str] = HTML) -> str:
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    return response.text


def test_requires_login(client: TestClient) -> None:
    response = client.get("/", headers=HTML, follow_redirects=False)

    assert response.status_code in (303, 401)


def test_empty_dashboard(authed_client: TestClient) -> None:
    html = get(authed_client)

    assert "No suggested properties." in html
    assert 'id="filters"' in html


def test_default_tab_is_suggested(authed_client: TestClient, seed: Seed) -> None:
    suggested = seed("Suggested one")
    seed("Denied one", status=S.DENIED)

    html = get(authed_client)

    assert card_ids(html) == [suggested]
    assert "Suggested one" in html
    assert "$2,000.00/mo" in html
    assert "Studio · 1 ba" in html
    assert 'href="https://example.com/l1"' in html
    assert 'rel="noopener noreferrer"' in html


@pytest.mark.parametrize(
    ("tab", "statuses"),
    [
        ("approved", [S.APPROVED]),
        ("in_progress", [S.INQUIRY_SENT, S.TOURING]),
        ("closed", [S.WON, S.LOST]),
        ("denied", [S.DENIED]),
        ("unavailable", [S.UNAVAILABLE]),
        ("archived", [S.ARCHIVED]),
    ],
)
def test_tabs_filter_by_status(
    authed_client: TestClient, seed: Seed, tab: str, statuses: list[PropertyStatus]
) -> None:
    wanted = {seed(status=status) for status in statuses}
    seed(status=S.SUGGESTED)

    assert set(card_ids(get(authed_client, f"/?status={tab}"))) == wanted


def test_all_tab_leaves_out_archived(authed_client: TestClient, seed: Seed) -> None:
    kept = {seed(status=S.SUGGESTED), seed(status=S.DENIED), seed(status=S.WON)}
    seed(status=S.ARCHIVED)

    assert set(card_ids(get(authed_client, "/?status=all"))) == kept


def test_tabs_show_counts_and_mark_the_current_one(authed_client: TestClient, seed: Seed) -> None:
    seed(status=S.APPROVED)
    seed(status=S.APPROVED)
    seed(status=S.TOURING)

    html = get(authed_client, "/?status=approved")

    assert re.search(r'aria-current="page">Approved <span class="count">2</span>', html)
    assert re.search(r'>In progress <span class="count">1</span>', html)
    assert re.search(r'>All <span class="count">3</span>', html)


def test_search(authed_client: TestClient, seed: Seed) -> None:
    loft = seed("Downtown loft")
    seed("Suburban house")

    html = get(authed_client, "/?q=LOFT")

    assert card_ids(html) == [loft]
    assert 'value="LOFT"' in html


def test_search_without_results_says_so(authed_client: TestClient, seed: Seed) -> None:
    seed("Downtown loft")

    assert "No suggested properties match “castle”." in get(authed_client, "/?q=castle")


@pytest.mark.parametrize(
    ("sort", "expected"),
    [("score", ["mid", "high", "low"]), ("price", ["low", "mid", "high"]), ("newest", None)],
)
def test_sort(authed_client: TestClient, seed: Seed, sort: str, expected: list[str] | None) -> None:
    made = {
        "low": seed(price=1000, score=10, age=timedelta(hours=1)),
        "mid": seed(price=2000, score=90, age=timedelta(hours=3)),
        "high": seed(price=3000, score=50, age=timedelta(hours=2)),
    }
    order = expected or ["low", "high", "mid"]

    assert card_ids(get(authed_client, f"/?sort={sort}")) == [made[k] for k in order]


def test_snoozed_hidden_by_default(authed_client: TestClient, seed: Seed) -> None:
    shown = seed()
    snoozed = seed(snoozed=True)

    assert card_ids(get(authed_client)) == [shown]
    html = get(authed_client, "/?snoozed=true")
    assert set(card_ids(html)) == {shown, snoozed}
    assert "Snoozed until" in html


def test_pagination_keeps_filters(authed_client: TestClient, seed: Seed) -> None:
    for _ in range(PAGE_SIZE + 3):
        seed("Loft")

    first = get(authed_client, "/?q=loft&sort=newest")
    second = get(authed_client, "/?q=loft&sort=newest&page=2")

    assert len(card_ids(first)) == PAGE_SIZE
    assert len(card_ids(second)) == 3
    assert set(card_ids(first)).isdisjoint(card_ids(second))
    assert 'href="/?q=loft&amp;sort=newest&amp;page=2"' in first
    assert 'href="/?q=loft&amp;sort=newest"' in second  # page 1 leaves the default out
    assert f"Page 2 of 2 · {PAGE_SIZE + 3} properties" in second


def test_page_past_the_end_shows_the_last(authed_client: TestClient, seed: Seed) -> None:
    pid = seed()

    assert card_ids(get(authed_client, "/?page=99")) == [pid]


def test_board_shows_a_column_per_tab(authed_client: TestClient, seed: Seed) -> None:
    suggested = seed(status=S.SUGGESTED)
    approved = seed(status=S.APPROVED)
    seed(status=S.ARCHIVED)

    html = get(authed_client, "/?view=board")

    columns = re.findall(r'<h2 id="col-([a-z_]+)">', html)
    assert columns == [
        "suggested",
        "new",
        "approved",
        "in_progress",
        "closed",
        "denied",
        "unavailable",
    ]
    assert card_ids(html) == [suggested, approved]


def test_board_caps_columns_and_links_to_the_list(authed_client: TestClient, seed: Seed) -> None:
    for _ in range(PAGE_SIZE + 1):
        seed()

    html = get(authed_client, "/?view=board")

    assert len(card_ids(html)) == PAGE_SIZE
    assert f"Showing {PAGE_SIZE} of {PAGE_SIZE + 1}." in html
    assert '<a href="/" hx-get="/">Open in list</a>' in html  # suggested + list are defaults


def test_htmx_gets_the_results_fragment(authed_client: TestClient, seed: Seed) -> None:
    pid = seed(status=S.APPROVED)

    html = get(authed_client, "/?status=approved", headers=HTMX)

    assert "<html" not in html
    assert 'id="filters"' not in html
    assert card_ids(html) == [pid]
    # The tab lives in the form outside the swap target; it is updated out of band.
    assert re.search(r'id="filter-status" name="status" value="approved" hx-swap-oob="true"', html)


def test_htmx_pushes_the_canonical_url(authed_client: TestClient, seed: Seed) -> None:
    seed()

    response = authed_client.get("/?status=suggested&q=&sort=score&view=list&page=5", headers=HTMX)

    assert response.headers["HX-Push-Url"] == "/"
    response = authed_client.get("/?status=denied&q=loft&sort=price", headers=HTMX)
    assert response.headers["HX-Push-Url"] == "/?status=denied&q=loft&sort=price"


def test_cards_name_the_source_by_display_name(authed_client: TestClient, seed: Seed) -> None:
    seed(source="furnished_finder")

    assert "View on Furnished Finder</a>" in get(authed_client)


def test_unknown_source_falls_back_to_its_name(authed_client: TestClient, seed: Seed) -> None:
    seed()

    assert "View on test</a>" in get(authed_client)


def test_cards_show_the_first_photo(authed_client: TestClient, seed: Seed) -> None:
    seed("With photos", images=("https://img.example.com/a.jpg", "https://img.example.com/b.jpg"))
    seed("Without photos")

    html = get(authed_client)

    assert html.count('class="card-photo"') == 1
    assert html.count('class="card-photo no-photo"') == 1
    assert 'src="/photo?src=https%3A%2F%2Fimg.example.com%2Fa.jpg"' in html
    assert "b.jpg" not in html


def test_cards_link_photos_directly_when_the_proxy_is_off(
    authed_client: TestClient, seed: Seed
) -> None:
    seed(images=("https://img.example.com/a.jpg",))
    state = authed_client.app.state  # type: ignore[attr-defined]
    images = state.settings.images.model_copy(update={"proxy_enabled": False})
    state.settings = state.settings.model_copy(update={"images": images})

    assert 'src="https://img.example.com/a.jpg"' in get(authed_client)


def test_pages_load_the_photo_placeholder_script(authed_client: TestClient) -> None:
    assert "/static/js/photos.js" in get(authed_client)


def test_full_page_has_no_out_of_band_swap(authed_client: TestClient) -> None:
    html = get(authed_client, "/?status=denied")

    assert "hx-swap-oob" not in html
    assert 'id="filter-status" name="status" value="denied">' in html


def test_scraped_text_is_escaped(authed_client: TestClient, seed: Seed) -> None:
    seed("<script>alert(1)</script>")

    html = get(authed_client)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


@pytest.mark.parametrize(
    "query",
    ["status=bogus", "sort=cheapest", "view=grid", "page=0", "q=" + "x" * 201, "owner=landlord"],
)
def test_bad_params_are_rejected(authed_client: TestClient, query: str) -> None:
    assert authed_client.get(f"/?{query}", headers=HTML).status_code == 422


def test_filters_url_leaves_defaults_out() -> None:
    assert Filters().url() == "/"
    assert Filters(owner=OwnerType.AGENT).url() == "/?owner=agent"
    assert Filters(q="a b").url(page=2) == "/?q=a+b&page=2"
    assert Filters(snoozed=True).url(status="all") == "/?status=all&snoozed=true"


# --- Owner type ----------------------------------------------------------------------


def test_owner_filter_narrows_cards_and_counts(authed_client: TestClient, seed: Seed) -> None:
    agent = seed("Agent listed", owner=OwnerType.AGENT)
    seed("Landlord listed", owner=OwnerType.INDIVIDUAL)
    seed("Unknown lister")

    html = get(authed_client, "/?owner=agent")

    assert card_ids(html) == [agent]
    assert 'value="agent" selected' in html
    assert len(card_ids(get(authed_client, "/?owner=unknown"))) == 1
    assert len(card_ids(get(authed_client))) == 3


def test_cards_show_a_known_owner_type_only(authed_client: TestClient, seed: Seed) -> None:
    seed("Agent listed", owner=OwnerType.AGENT)
    seed("Unknown lister")

    html = get(authed_client)

    assert html.count('class="status owner-') == 1
    assert '<span class="status owner-agent">Agent / broker</span>' in html


def test_owner_filter_survives_a_bulk_action(
    authed_client: TestClient, seed: Seed, csrf: Callable[[str], str]
) -> None:
    agent = seed("Agent listed", owner=OwnerType.AGENT)
    html = get(authed_client, "/?owner=agent")
    assert '<input type="hidden" name="owner" value="agent">' in html

    response = authed_client.post(
        "/properties/bulk",
        data={"csrf_token": csrf(html), "action": "deny", "ids": [str(agent)], "owner": "agent"},
        headers=HTML,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?owner=agent"
