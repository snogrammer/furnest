"""Fixing grouping from the detail page: merging properties and splitting a listing off."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import GroupingSettings
from furnest.db.listing_store import SqlListingStore
from furnest.db.property_store import SqlPropertyStore
from furnest.domain import Actor, NormalizedListing, PropertyStatus

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
HTML = {"Accept": "text/html"}
ADDRESS = {"line1": "123 N Main St", "city": "Springfield", "region": "IL", "postal_code": "62701"}

Csrf = Callable[[str], str]
AddListing = Callable[..., int]


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlPropertyStore:
    return SqlPropertyStore(session_factory, grouping=GroupingSettings())


@pytest.fixture
def add_listing(session_factory: sessionmaker[Session], store: SqlPropertyStore) -> AddListing:
    """A listing with its property (grouped with others at ``address``); its id."""
    listings = SqlListingStore(session_factory)

    def add(source: str, address: dict[str, Any] = ADDRESS) -> int:
        listing = NormalizedListing.model_validate(
            {
                "source": source,
                "source_id": "1",
                "url": f"https://example.com/{source}/1",
                "title": f"Furnished 2BR on {source}",
                "beds": 2,
                "address": address,
            }
        )
        (listing_id,) = listings.upsert_batch(source, [listing], seen_at=T0).new_ids
        store.ensure_for_listing(listing_id, at=T0)
        return listing_id

    return add


def post(client: TestClient, csrf: Csrf, path: str, data: dict[str, str] | None = None) -> Response:
    token = csrf(client.get("/", headers=HTML).text)
    return client.post(
        path,
        data={"csrf_token": token, **(data or {})},
        headers=HTML,
        follow_redirects=False,
    )


def property_of(store: SqlPropertyStore, listing_id: int) -> int:
    state = store.for_listing(listing_id)
    assert state is not None
    return state.id


def test_merge_moves_the_other_property_here(
    authed_client: TestClient, csrf: Csrf, store: SqlPropertyStore, add_listing: AddListing
) -> None:
    a = property_of(store, add_listing("zillow"))
    b = property_of(store, add_listing("craigslist", {"city": "Elsewhere"}))
    assert a != b

    response = post(authed_client, csrf, f"/properties/{a}/merge", {"other": str(b)})

    assert response.status_code == 303
    assert response.headers["location"] == f"/properties/{a}?grouping=merged"
    assert store.get(b).merged_into_id == a  # type: ignore[union-attr]
    page = authed_client.get(response.headers["location"], headers=HTML)
    assert "Properties merged." in page.text
    assert f"Merged #{b} into this property" in page.text
    # Two listings now: each can be split off again.
    assert page.text.count("Split off</button>") == 2
    # The merged property's page sends the browser here.
    gone = authed_client.get(f"/properties/{b}", headers=HTML, follow_redirects=False)
    assert gone.headers["location"] == f"/properties/{a}"


@pytest.mark.parametrize(
    ("other", "error"), [("999", "No property #999."), ("self", "is already this property")]
)
def test_merge_rejects_an_unknown_or_the_same_property(
    authed_client: TestClient,
    csrf: Csrf,
    store: SqlPropertyStore,
    add_listing: AddListing,
    other: str,
    error: str,
) -> None:
    a = property_of(store, add_listing("zillow"))

    target = str(a) if other == "self" else other
    response = post(authed_client, csrf, f"/properties/{a}/merge", {"other": target})

    assert response.status_code == 422
    assert error in response.text


def test_merge_into_an_unknown_or_merged_property(
    authed_client: TestClient, csrf: Csrf, store: SqlPropertyStore, add_listing: AddListing
) -> None:
    a = property_of(store, add_listing("zillow"))
    b = property_of(store, add_listing("craigslist", {"city": "Elsewhere"}))
    store.merge_properties(a, b, actor=Actor.DASHBOARD, at=T0)

    assert post(authed_client, csrf, "/properties/999/merge", {"other": str(a)}).status_code == 404
    response = post(authed_client, csrf, f"/properties/{b}/merge", {"other": str(a)})
    assert response.status_code == 303
    assert response.headers["location"] == f"/properties/{a}"


def test_split_moves_the_listing_to_a_new_property(
    authed_client: TestClient, csrf: Csrf, store: SqlPropertyStore, add_listing: AddListing
) -> None:
    first = add_listing("zillow")
    second = add_listing("craigslist")
    prop = property_of(store, first)
    assert property_of(store, second) == prop
    page = authed_client.get(f"/properties/{prop}", headers=HTML)
    assert f'action="/properties/{prop}/listings/{second}/split"' in page.text

    response = post(authed_client, csrf, f"/properties/{prop}/listings/{second}/split")

    assert response.status_code == 303
    new = property_of(store, second)
    assert new != prop
    assert response.headers["location"] == f"/properties/{prop}?grouping=split&new={new}"
    page = authed_client.get(response.headers["location"], headers=HTML)
    assert f"Listing split off into property #{new}." in page.text
    assert f"Listing #{second} split off into #{new}" in page.text
    # One listing left: nothing to split off.
    assert "Split off</button>" not in page.text
    new_page = authed_client.get(f"/properties/{new}", headers=HTML)
    assert f"(split from #{prop})" in new_page.text


def test_split_errors(
    authed_client: TestClient, csrf: Csrf, store: SqlPropertyStore, add_listing: AddListing
) -> None:
    only = add_listing("zillow", {"city": "Elsewhere"})
    a = property_of(store, only)
    first = add_listing("craigslist")
    second = add_listing("furnishedfinder")
    prop = property_of(store, first)

    response = post(authed_client, csrf, f"/properties/{a}/listings/{only}/split")
    assert response.status_code == 422
    assert "Can&#39;t split: a property&#39;s only listing can&#39;t be split off." in response.text
    # A listing of another property, and an unknown property.
    assert post(authed_client, csrf, f"/properties/{a}/listings/{first}/split").status_code == 404
    assert post(authed_client, csrf, f"/properties/999/listings/{first}/split").status_code == 404

    store.merge_properties(a, prop, actor=Actor.DASHBOARD, at=T0)
    response = post(authed_client, csrf, f"/properties/{prop}/listings/{second}/split")
    assert response.status_code == 303
    assert response.headers["location"] == f"/properties/{a}"


def test_grouping_posts_need_the_csrf_token(
    authed_client: TestClient, store: SqlPropertyStore, add_listing: AddListing
) -> None:
    first = add_listing("zillow")
    add_listing("craigslist")
    prop = property_of(store, first)

    for path in (f"/properties/{prop}/merge", f"/properties/{prop}/listings/{first}/split"):
        response = authed_client.post(
            path, data={"other": "1"}, headers=HTML, follow_redirects=False
        )
        assert response.status_code == 403, path
