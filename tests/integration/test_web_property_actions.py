"""Status buttons on the detail page and bulk deny/archive on the dashboard list."""

import re
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.listing_store import SqlListingStore
from furnest.db.models import Property
from furnest.db.property_store import SqlPropertyStore
from furnest.db.session import session_scope
from furnest.domain import Actor, NormalizedListing, OwnerType, PropertyStatus
from furnest.web.routes.pages import MAX_BULK

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
HTML = {"Accept": "text/html"}

Csrf = Callable[[str], str]
MakeProperty = Callable[..., int]


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlPropertyStore:
    return SqlPropertyStore(session_factory)


@pytest.fixture
def make_property(session_factory: sessionmaker[Session], store: SqlPropertyStore) -> MakeProperty:
    """A property for a new listing, forced into ``status``."""
    counter = iter(range(1, 1_000))

    def make(status: PropertyStatus = S.SUGGESTED) -> int:
        n = next(counter)
        listing = NormalizedListing.model_validate(
            {
                "source": "test",
                "source_id": f"l{n}",
                "url": f"https://example.com/l{n}",
                "title": f"Listing {n}",
            }
        )
        (listing_id,) = (
            SqlListingStore(session_factory).upsert_batch("test", [listing], seen_at=T0).new_ids
        )
        property_id = store.ensure_for_listing(listing_id, at=T0).id
        with session_scope(session_factory) as session:
            session.execute(
                update(Property).where(Property.id == property_id).values(status=status)
            )
        return property_id

    return make


def token(client: TestClient, csrf: Csrf) -> str:
    return csrf(client.get("/", headers=HTML).text)


def post(
    client: TestClient,
    csrf: Csrf,
    path: str,
    data: dict[str, str | list[str]],
    headers: dict[str, str] | None = None,
) -> Response:
    return client.post(
        path,
        data={"csrf_token": token(client, csrf), **data},
        headers={**HTML, **(headers or {})},
        follow_redirects=False,
    )


def status_buttons(html: str) -> list[str]:
    return re.findall(r'<button type="submit" name="to" value="([a-z_]+)"', html)


def last_audit_actor(store: SqlPropertyStore, property_id: int) -> Actor:
    return store.audit_events(property_id)[-1].actor


@pytest.mark.parametrize(
    ("status", "buttons", "deny"),
    [
        (S.SUGGESTED, ["approved", "archived"], True),
        # The inquiry flow's moves go through Inquire and the review queue instead.
        (S.APPROVED, ["archived"], True),
        (S.INQUIRY_DRAFTED, ["archived"], False),
        (S.INQUIRY_SENT, ["in_progress", "responded", "lost", "archived"], False),
        (S.DENIED, ["approved", "archived"], False),
        (S.WON, ["archived"], False),
        (S.UNAVAILABLE, ["archived"], False),
        (S.ARCHIVED, [], False),
    ],
)
def test_detail_offers_only_allowed_moves(
    authed_client: TestClient,
    make_property: MakeProperty,
    status: PropertyStatus,
    buttons: list[str],
    deny: bool,
) -> None:
    property_id = make_property(status)

    html = authed_client.get(f"/properties/{property_id}", headers=HTML).text

    assert status_buttons(html) == buttons
    assert ("/deny" in html) is deny
    assert ('class="status-actions"' in html) is bool(buttons)


def test_approve(
    authed_client: TestClient, csrf: Csrf, make_property: MakeProperty, store: SqlPropertyStore
) -> None:
    property_id = make_property(S.SUGGESTED)

    response = post(authed_client, csrf, f"/properties/{property_id}/status", {"to": "approved"})

    assert response.status_code == 303
    assert response.headers["location"] == f"/properties/{property_id}"
    assert store.get(property_id).status is S.APPROVED  # type: ignore[union-attr]
    assert last_audit_actor(store, property_id) is Actor.DASHBOARD


def test_status_change_over_htmx_uses_the_csrf_header(
    authed_client: TestClient, csrf: Csrf, make_property: MakeProperty, store: SqlPropertyStore
) -> None:
    property_id = make_property(S.WON)

    response = authed_client.post(
        f"/properties/{property_id}/status",
        data={"to": "archived"},
        headers={**HTML, "HX-Request": "true", "X-CSRF-Token": token(authed_client, csrf)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert store.get(property_id).status is S.ARCHIVED  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("status", "to"),
    [(S.SUGGESTED, S.WON), (S.SUGGESTED, S.UNAVAILABLE), (S.UNAVAILABLE, S.SUGGESTED)],
)
def test_disallowed_move_is_409(
    authed_client: TestClient,
    csrf: Csrf,
    make_property: MakeProperty,
    store: SqlPropertyStore,
    status: PropertyStatus,
    to: PropertyStatus,
) -> None:
    property_id = make_property(status)
    events = len(store.audit_events(property_id))

    response = post(authed_client, csrf, f"/properties/{property_id}/status", {"to": to.value})

    assert response.status_code == 409
    assert 'class="form-error"' in response.text
    assert "move to" in response.text
    assert store.get(property_id).status is status  # type: ignore[union-attr]
    assert len(store.audit_events(property_id)) == events


def test_unknown_status_is_422(
    authed_client: TestClient, csrf: Csrf, make_property: MakeProperty
) -> None:
    property_id = make_property()

    response = post(authed_client, csrf, f"/properties/{property_id}/status", {"to": "bogus"})

    assert response.status_code == 422


def test_status_on_unknown_property_is_404(authed_client: TestClient, csrf: Csrf) -> None:
    assert (
        post(authed_client, csrf, "/properties/999/status", {"to": "approved"}).status_code == 404
    )


def test_status_needs_csrf(
    authed_client: TestClient, make_property: MakeProperty, store: SqlPropertyStore
) -> None:
    property_id = make_property()

    response = authed_client.post(
        f"/properties/{property_id}/status", data={"to": "approved"}, follow_redirects=False
    )

    assert response.status_code == 403
    assert store.get(property_id).status is S.SUGGESTED  # type: ignore[union-attr]


def test_status_needs_login(client: TestClient, make_property: MakeProperty) -> None:
    property_id = make_property()

    response = client.post(
        f"/properties/{property_id}/status", data={"to": "approved"}, follow_redirects=False
    )

    assert response.status_code in (303, 401, 403)


# Bulk


def test_list_view_has_checkboxes_board_view_does_not(
    authed_client: TestClient, make_property: MakeProperty
) -> None:
    property_id = make_property()

    listing = authed_client.get("/", headers=HTML).text
    board = authed_client.get("/?view=board", headers=HTML).text

    assert f'name="ids" value="{property_id}"' in listing
    assert 'id="bulk"' in listing
    assert 'name="ids"' not in board
    assert 'id="bulk"' not in board


def test_bulk_deny(
    authed_client: TestClient, csrf: Csrf, make_property: MakeProperty, store: SqlPropertyStore
) -> None:
    ids = [make_property(), make_property()]

    response = post(
        authed_client,
        csrf,
        "/properties/bulk",
        {"action": "deny", "ids": [str(i) for i in ids], "status": "suggested", "q": "Listing"},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?q=Listing"
    for property_id in ids:
        assert store.get(property_id).status is S.DENIED  # type: ignore[union-attr]
        assert last_audit_actor(store, property_id) is Actor.DASHBOARD
        assert store.notes(property_id) == []


def test_bulk_archive_over_htmx_skips_what_it_cannot_move(
    authed_client: TestClient, csrf: Csrf, make_property: MakeProperty, store: SqlPropertyStore
) -> None:
    archived = make_property(S.ARCHIVED)
    suggested = make_property(S.SUGGESTED)

    response = post(
        authed_client,
        csrf,
        "/properties/bulk",
        {"action": "archive", "ids": [str(archived), str(suggested)], "status": "all"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert response.headers["HX-Push-Url"] == "/?status=all"
    assert "<html" not in response.text
    assert "Archived 1. Skipped 1" in response.text
    assert f"#{archived}: archived" in response.text
    assert store.get(suggested).status is S.ARCHIVED  # type: ignore[union-attr]
    assert last_audit_actor(store, suggested) is Actor.DASHBOARD


def test_bulk_deny_over_htmx_shows_a_notice(
    authed_client: TestClient, csrf: Csrf, make_property: MakeProperty
) -> None:
    property_id = make_property()

    response = post(
        authed_client,
        csrf,
        "/properties/bulk",
        {"action": "deny", "ids": [str(property_id)]},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert '<p class="flash" role="status">Denied 1.</p>' in response.text


def test_bulk_where_nothing_can_move_is_409(
    authed_client: TestClient, csrf: Csrf, make_property: MakeProperty, store: SqlPropertyStore
) -> None:
    won = make_property(S.WON)

    response = post(
        authed_client, csrf, "/properties/bulk", {"action": "deny", "ids": [str(won), "999"]}
    )

    assert response.status_code == 409
    assert "Denied 0. Skipped 2" in response.text
    assert "#999: not found" in response.text
    assert store.get(won).status is S.WON  # type: ignore[union-attr]


def test_bulk_without_selection_is_422(authed_client: TestClient, csrf: Csrf) -> None:
    response = post(authed_client, csrf, "/properties/bulk", {"action": "deny"})

    assert response.status_code == 422
    assert "Select at least one property." in response.text


def test_bulk_with_too_many_is_422(
    authed_client: TestClient, csrf: Csrf, make_property: MakeProperty, store: SqlPropertyStore
) -> None:
    property_id = make_property()
    ids = [str(property_id)] + [str(1_000 + i) for i in range(MAX_BULK)]

    response = post(authed_client, csrf, "/properties/bulk", {"action": "deny", "ids": ids})

    assert response.status_code == 422
    assert store.get(property_id).status is S.SUGGESTED  # type: ignore[union-attr]


def test_bulk_with_unknown_action_is_422(
    authed_client: TestClient, csrf: Csrf, make_property: MakeProperty
) -> None:
    response = post(
        authed_client, csrf, "/properties/bulk", {"action": "won", "ids": [str(make_property())]}
    )

    assert response.status_code == 422


def test_bulk_needs_csrf(
    authed_client: TestClient, make_property: MakeProperty, store: SqlPropertyStore
) -> None:
    property_id = make_property()

    response = authed_client.post(
        "/properties/bulk",
        data={"action": "deny", "ids": [str(property_id)]},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert store.get(property_id).status is S.SUGGESTED  # type: ignore[union-attr]


# --- Merged properties --------------------------------------------------------------


def merge(session_factory: sessionmaker[Session], absorbed: int, into: int) -> None:
    with session_scope(session_factory) as session:
        session.execute(update(Property).where(Property.id == absorbed).values(merged_into_id=into))


def test_a_merged_property_redirects_to_the_survivor(
    authed_client: TestClient,
    csrf: Csrf,
    make_property: MakeProperty,
    store: SqlPropertyStore,
    session_factory: sessionmaker[Session],
) -> None:
    survivor, absorbed = make_property(S.SUGGESTED), make_property(S.SUGGESTED)
    merge(session_factory, absorbed, survivor)
    target = f"/properties/{survivor}"

    page = authed_client.get(f"/properties/{absorbed}", headers=HTML, follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == target

    actions: list[tuple[str, dict[str, str | list[str]]]] = [
        ("status", {"to": "approved"}),
        ("deny", {"reason": "no"}),
        ("notes", {"body": "hello"}),
        ("inquire", {}),
    ]
    for path, data in actions:
        response = post(authed_client, csrf, f"/properties/{absorbed}/{path}", data)
        assert response.status_code == 303, path
        assert response.headers["location"] == target, path
    assert store.get(absorbed).status is S.SUGGESTED  # type: ignore[union-attr]
    assert store.notes(absorbed) == []


def test_bulk_skips_a_merged_property(
    authed_client: TestClient,
    csrf: Csrf,
    make_property: MakeProperty,
    session_factory: sessionmaker[Session],
) -> None:
    survivor, absorbed = make_property(S.SUGGESTED), make_property(S.SUGGESTED)
    merge(session_factory, absorbed, survivor)

    response = post(
        authed_client,
        csrf,
        "/properties/bulk",
        {"action": "archive", "ids": [str(absorbed), str(survivor)]},
    )

    assert response.status_code == 303
    html = authed_client.get("/", headers=HTML).text
    assert f"Listing {absorbed}" not in html


# --- Owner type ----------------------------------------------------------------------


def test_set_and_clear_the_owner_type(
    authed_client: TestClient, csrf: Csrf, make_property: MakeProperty, store: SqlPropertyStore
) -> None:
    property_id = make_property(S.SUGGESTED)

    response = post(
        authed_client, csrf, f"/properties/{property_id}/owner-type", {"owner_type": "agent"}
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/properties/{property_id}"
    state = store.get(property_id)
    assert state is not None
    assert (state.owner_type, state.owner_type_override) == (OwnerType.AGENT, OwnerType.AGENT)
    assert last_audit_actor(store, property_id) is Actor.DASHBOARD

    response = post(
        authed_client, csrf, f"/properties/{property_id}/owner-type", {"owner_type": ""}
    )

    assert response.status_code == 303
    state = store.get(property_id)
    assert state is not None
    assert (state.owner_type, state.owner_type_override) == (OwnerType.UNKNOWN, None)


@pytest.mark.parametrize("value", ["landlord", "unknown"])
def test_an_owner_type_that_is_not_offered_is_422(
    authed_client: TestClient, csrf: Csrf, make_property: MakeProperty, value: str
) -> None:
    property_id = make_property(S.SUGGESTED)
    response = post(
        authed_client, csrf, f"/properties/{property_id}/owner-type", {"owner_type": value}
    )
    assert response.status_code == 422


def test_owner_type_on_unknown_property_is_404(authed_client: TestClient, csrf: Csrf) -> None:
    response = post(authed_client, csrf, "/properties/999/owner-type", {"owner_type": "agent"})
    assert response.status_code == 404


def test_owner_type_on_a_merged_property_redirects_to_the_survivor(
    authed_client: TestClient,
    csrf: Csrf,
    make_property: MakeProperty,
    session_factory: sessionmaker[Session],
) -> None:
    absorbed, survivor = make_property(S.SUGGESTED), make_property(S.SUGGESTED)
    merge(session_factory, absorbed, survivor)

    response = post(
        authed_client, csrf, f"/properties/{absorbed}/owner-type", {"owner_type": "agent"}
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/properties/{survivor}"


def test_owner_type_needs_csrf(authed_client: TestClient, make_property: MakeProperty) -> None:
    property_id = make_property(S.SUGGESTED)
    response = authed_client.post(
        f"/properties/{property_id}/owner-type", data={"owner_type": "agent"}, headers=HTML
    )
    assert response.status_code == 403


def test_owner_type_needs_login(client: TestClient, make_property: MakeProperty) -> None:
    property_id = make_property(S.SUGGESTED)
    response = client.post(
        f"/properties/{property_id}/owner-type",
        data={"owner_type": "agent"},
        follow_redirects=False,
    )
    assert response.status_code in (303, 401, 403)
