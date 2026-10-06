"""Adding notes and denying with a reason on the property detail page."""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.listing_store import SqlListingStore
from furnest.db.models import Property
from furnest.db.property_store import MAX_NOTE_LENGTH, SqlPropertyStore
from furnest.db.session import session_scope
from furnest.domain import Actor, NormalizedListing, PropertyStatus

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
HTML = {"Accept": "text/html"}

Csrf = Callable[[str], str]


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlPropertyStore:
    return SqlPropertyStore(session_factory)


@pytest.fixture
def property_id(session_factory: sessionmaker[Session], store: SqlPropertyStore) -> int:
    listing = NormalizedListing.model_validate(
        {"source": "test", "source_id": "a", "url": "https://example.com/a", "title": "A"}
    )
    (listing_id,) = (
        SqlListingStore(session_factory).upsert_batch("test", [listing], seen_at=T0).new_ids
    )
    return store.ensure_for_listing(listing_id, at=T0).id


def post(client: TestClient, csrf: Csrf, path: str, **fields: str) -> Response:
    token = csrf(client.get("/", headers=HTML).text)
    return client.post(
        path, data={"csrf_token": token, **fields}, headers=HTML, follow_redirects=False
    )


def page(client: TestClient, property_id: int) -> str:
    response = client.get(f"/properties/{property_id}", headers=HTML)
    assert response.status_code == 200
    return response.text


def test_add_note_renders_markdown(
    authed_client: TestClient, csrf: Csrf, property_id: int, store: SqlPropertyStore
) -> None:
    response = post(
        authed_client,
        csrf,
        f"/properties/{property_id}/notes",
        body="Call the **landlord**, see [map](https://example.com/map)",
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/properties/{property_id}#notes"
    (note,) = store.notes(property_id)
    assert note.actor is Actor.DASHBOARD
    html = page(authed_client, property_id)
    assert "<strong>landlord</strong>" in html
    assert '<a href="https://example.com/map" rel="noopener noreferrer nofollow">map</a>' in html


def test_note_html_is_neutralized(authed_client: TestClient, csrf: Csrf, property_id: int) -> None:
    post(
        authed_client,
        csrf,
        f"/properties/{property_id}/notes",
        body='<script>alert(1)</script> <img src=x onerror="alert(2)"> [x](javascript:alert(3))',
    )

    html = page(authed_client, property_id)
    assert "<script>alert(1)" not in html
    assert "<img src=x" not in html
    assert 'href="javascript:' not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_note_needs_csrf(authed_client: TestClient, property_id: int) -> None:
    response = authed_client.post(
        f"/properties/{property_id}/notes", data={"body": "hi"}, follow_redirects=False
    )

    assert response.status_code == 403


def test_note_needs_login(client: TestClient, property_id: int) -> None:
    response = client.post(
        f"/properties/{property_id}/notes", data={"body": "hi"}, follow_redirects=False
    )

    assert response.status_code in (303, 401, 403)


@pytest.mark.parametrize("body", ["   ", "x" * (MAX_NOTE_LENGTH + 1)])
def test_bad_note_shows_the_error(
    authed_client: TestClient, csrf: Csrf, property_id: int, store: SqlPropertyStore, body: str
) -> None:
    response = post(authed_client, csrf, f"/properties/{property_id}/notes", body=body)

    assert response.status_code == 422
    assert 'class="form-error"' in response.text
    assert store.notes(property_id) == []


def test_note_on_unknown_property_is_404(authed_client: TestClient, csrf: Csrf) -> None:
    assert post(authed_client, csrf, "/properties/999/notes", body="hi").status_code == 404


def test_deny_with_reason(
    authed_client: TestClient, csrf: Csrf, property_id: int, store: SqlPropertyStore
) -> None:
    assert f'action="/properties/{property_id}/deny"' in page(authed_client, property_id)

    response = post(authed_client, csrf, f"/properties/{property_id}/deny", reason="Too _noisy_")

    assert response.status_code == 303
    assert store.get(property_id).status is S.DENIED  # type: ignore[union-attr]
    html = page(authed_client, property_id)
    assert "<em>noisy</em>" in html
    assert "(reason in notes)" in html
    # Denied → only approve or archive remain; deny is no longer offered.
    assert "/deny" not in html


def test_deny_without_reason(
    authed_client: TestClient, csrf: Csrf, property_id: int, store: SqlPropertyStore
) -> None:
    response = post(authed_client, csrf, f"/properties/{property_id}/deny")

    assert response.status_code == 303
    assert store.get(property_id).status is S.DENIED  # type: ignore[union-attr]
    assert store.notes(property_id) == []


def test_forbidden_deny_is_409(
    authed_client: TestClient,
    csrf: Csrf,
    property_id: int,
    store: SqlPropertyStore,
    session_factory: sessionmaker[Session],
) -> None:
    with session_scope(session_factory) as session:
        session.execute(update(Property).where(Property.id == property_id).values(status=S.WON))

    response = post(authed_client, csrf, f"/properties/{property_id}/deny", reason="x")

    assert response.status_code == 409
    assert "Can&#39;t deny" in response.text or "Can't deny" in response.text
    assert store.notes(property_id) == []


def test_too_long_deny_reason_is_422(
    authed_client: TestClient, csrf: Csrf, property_id: int, store: SqlPropertyStore
) -> None:
    response = post(
        authed_client, csrf, f"/properties/{property_id}/deny", reason="x" * (MAX_NOTE_LENGTH + 1)
    )

    assert response.status_code == 422
    assert store.get(property_id).status is S.NEW  # type: ignore[union-attr]


def test_deny_unknown_property_is_404(authed_client: TestClient, csrf: Csrf) -> None:
    assert post(authed_client, csrf, "/properties/999/deny").status_code == 404
