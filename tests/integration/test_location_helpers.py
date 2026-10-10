"""ZIP prefill, neighborhood suggestions and match counts in the criteria editor (#180)."""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.criteria_store import CriteriaStore
from furnest.db.listing_store import SqlListingStore
from furnest.db.location_stats import AreaCircle, count_matches, stored_neighborhoods
from furnest.domain import Address, NormalizedListing, resolve_place

pytestmark = pytest.mark.integration

Csrf = Callable[[str], str]
T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
AREA = {
    "areas-0-name": "Springfield, IL",
    "areas-0-lat": "39.78",
    "areas-0-lng": "-89.65",
    "areas-0-radius": "50",
    "areas-0-unit": "mi",
}


def listing(source_id: str, **address: str) -> NormalizedListing:
    lat = address.pop("lat", None)
    return NormalizedListing(
        source="fake",
        source_id=source_id,
        url="https://example.com/" + source_id,  # type: ignore[arg-type]
        title="Furnished place",
        address=Address(**address),
        lat=float(lat) if lat else None,
        lng=-89.65 if lat else None,
    )


@pytest.fixture
def stored(session_factory: sessionmaker[Session]) -> None:
    SqlListingStore(session_factory).upsert_batch(
        "fake",
        [
            listing("a", city="Springfield", region="IL", neighborhood="Hyde Park", lat="39.78"),
            listing("b", city="springfield", region="Illinois", neighborhood="hyde park"),
            listing("c", postal_code="62701-1234", neighborhood="Downtown"),
            listing("d", city="Peoria", region="IL", neighborhood="Deep Ellum"),
            listing("e", city="Springfield", region="IL"),
        ],
        seen_at=T0,
    )


def form(client: TestClient, csrf: Csrf, **fields: object) -> dict[str, object]:
    return {"csrf_token": csrf(client.get("/criteria").text), "currency": "USD", **fields}


def test_stored_neighborhoods_in_the_city(
    stored: None, session_factory: sessionmaker[Session]
) -> None:
    place = resolve_place("Springfield, IL")
    assert place is not None
    names = stored_neighborhoods(session_factory, place)
    assert [n.casefold() for n in names] == ["hyde park", "downtown"]


def test_count_matches(stored: None, session_factory: sessionmaker[Session]) -> None:
    near = AreaCircle(39.78, -89.65, 5)
    far = AreaCircle(10.0, 10.0, 5)
    counts = count_matches(session_factory, [near, far], ["62701", "99999"], [" HYDE  park"])
    assert counts.areas == (1, 0)
    assert counts.zip_codes == 1
    assert counts.neighborhoods == 2
    empty = count_matches(session_factory, [], [], [])
    assert (empty.areas, empty.zip_codes, empty.neighborhoods) == ((), 0, 0)


def test_one_click_adds_the_citys_zips_and_the_form_saves(
    authed_client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    place = resolve_place("Springfield, IL")
    assert place is not None
    zips = set(place.zip_codes)

    page = authed_client.post(
        "/criteria",
        data=form(authed_client, csrf, **AREA, zip_codes="62701", add_zips="0"),
    )
    assert page.status_code == 200
    assert f"Added {len(zips) - 1} ZIP codes for Springfield, IL" in page.text
    assert CriteriaStore(session_factory).active() is None

    body = page.text.split('<textarea id="zip_codes"')[1].split("</textarea>")[0]
    shown = set(body.split(">", 1)[1].split())
    assert shown == zips

    saved = authed_client.post(
        "/criteria",
        data=form(authed_client, csrf, **AREA, zip_codes="\n".join(sorted(shown))),
        follow_redirects=False,
    )
    assert saved.status_code == 303
    active = CriteriaStore(session_factory).active()
    assert active is not None
    assert set(active.criteria.zip_codes) == zips


def test_add_zips_is_idempotent_over_htmx(authed_client: TestClient, csrf: Csrf) -> None:
    data = form(authed_client, csrf, **AREA, add_zips="0")
    first = authed_client.post("/criteria", data=data, headers={"HX-Request": "true"})
    zips_text = first.text.split('<textarea id="zip_codes"')[1].split("</textarea>")[0]
    again = authed_client.post(
        "/criteria",
        data={**data, "zip_codes": zips_text.split(">", 1)[1], "add_zips": "0"},
    )
    assert "Added 0 ZIP codes" in again.text
    assert "are listed" in again.text


def test_add_zips_needs_a_known_city(authed_client: TestClient, csrf: Csrf) -> None:
    response = authed_client.post(
        "/criteria",
        data=form(authed_client, csrf, **{"areas-0-name": "Nowhereville QQ"}, add_zips="0"),
    )
    assert response.status_code == 422
    assert "no US city matches" in response.text


def test_editor_offers_zip_button_chips_and_counts(
    stored: None, authed_client: TestClient, csrf: Csrf
) -> None:
    page = authed_client.post(
        "/criteria",
        data=form(authed_client, csrf, **AREA, neighborhoods="Downtown", lookup="0"),
    )
    assert 'name="add_zips" value="0"' in page.text
    assert 'name="add_neighborhood" value="Hyde Park"' in page.text
    assert 'name="add_neighborhood" value="Downtown"' not in page.text
    assert "Stored listings matching today" in page.text
    assert "Springfield, IL: 1;" in page.text
    assert "neighborhoods: 1." in page.text


def test_neighborhood_chip_appends_once(authed_client: TestClient, csrf: Csrf) -> None:
    page = authed_client.post(
        "/criteria",
        data=form(
            authed_client, csrf, **AREA, neighborhoods="Downtown", add_neighborhood="hyde Park"
        ),
    )
    assert "Added hyde Park" in page.text
    again = authed_client.post(
        "/criteria",
        data=form(
            authed_client, csrf, **AREA, neighborhoods="Hyde Park", add_neighborhood="hyde park"
        ),
    )
    assert "already listed" in again.text
