"""The Bearer JSON API under ``/api/v1`` and its docs at ``/api/docs``."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.base import utcnow
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import Property
from furnest.db.property_store import MAX_NOTE_LENGTH, SqlPropertyStore
from furnest.db.session import session_scope
from furnest.domain import Actor, FilterRule, Money, NormalizedListing, PropertyStatus
from furnest.domain.price import Price
from furnest.matching import BreakdownItem, MatchResult, RuleResult, RuleStatus
from furnest.web.routes.api import MAX_LIMIT

pytestmark = pytest.mark.integration

S = PropertyStatus
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

Seed = Callable[..., int]  # (status=..., **listing fields) -> property id


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlPropertyStore:
    return SqlPropertyStore(session_factory)


@pytest.fixture
def seed(session_factory: sessionmaker[Session], store: SqlPropertyStore) -> Seed:
    """A property for a new listing, forced into ``status``, with a fixed score."""
    listings = SqlListingStore(session_factory)
    counter = iter(range(1, 1000))

    def add(
        status: PropertyStatus = S.SUGGESTED, score: int | None = None, **fields: object
    ) -> int:
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
        property_id = store.ensure_for_listing(listing_id, at=T0).id
        with session_scope(session_factory) as session:
            session.execute(
                update(Property)
                .where(Property.id == property_id)
                .values(status=status, score=score)
            )
        return property_id

    return add


@pytest.fixture
def api(client: TestClient, auth_token: str) -> TestClient:
    """``client`` sending the Bearer token on every request."""
    client.headers["Authorization"] = f"Bearer {auth_token}"
    return client


# --- auth --------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/api/v1/properties", "/api/v1/properties/1", "/api/v1/properties/1/listings"]
)
def test_reads_need_a_token(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Not authenticated"}


def test_a_wrong_token_is_401(client: TestClient) -> None:
    response = client.get("/api/v1/properties", headers={"Authorization": "Bearer nope"})

    assert response.status_code == 401


def test_a_browser_page_load_is_not_redirected_to_login(client: TestClient) -> None:
    response = client.get(
        "/api/v1/properties", headers={"Accept": "text/html"}, follow_redirects=False
    )

    assert response.status_code == 401


def test_the_session_cookie_is_not_enough(authed_client: TestClient, seed: Seed) -> None:
    property_id = seed()

    assert authed_client.get("/api/v1/properties").status_code == 401
    response = authed_client.post(
        f"/api/v1/properties/{property_id}/notes", json={"body": "via cookie"}
    )
    assert response.status_code == 401


def test_writes_need_a_token(client: TestClient, seed: Seed, store: SqlPropertyStore) -> None:
    property_id = seed()

    notes = client.post(f"/api/v1/properties/{property_id}/notes", json={"body": "hi"})
    moves = client.post(f"/api/v1/properties/{property_id}/transitions", json={"to": "approved"})

    assert (notes.status_code, moves.status_code) == (401, 401)
    assert store.notes(property_id) == []
    assert store.get(property_id).status is S.SUGGESTED  # type: ignore[union-attr]


def test_bearer_writes_need_no_csrf_token(api: TestClient, seed: Seed) -> None:
    response = api.post(f"/api/v1/properties/{seed()}/notes", json={"body": "no csrf here"})

    assert response.status_code == 201


# --- list --------------------------------------------------------------------


def test_list_returns_properties_with_their_primary_listing(api: TestClient, seed: Seed) -> None:
    property_id = seed(
        status=S.APPROVED,
        score=80,
        title="Sunny loft",
        price=Price(amount=Money.from_major(2400)),
        beds=1,
        baths=1.5,
    )

    body = api.get("/api/v1/properties").json()

    assert body["total"] == 1
    assert (body["limit"], body["offset"]) == (50, 0)
    (item,) = body["items"]
    assert item["property"]["id"] == property_id
    assert item["property"]["status"] == "approved"
    assert item["property"]["score"] == 80
    assert item["property"]["allowed_transitions"] == ["denied", "inquiry_drafted", "archived"]
    assert item["listing"]["title"] == "Sunny loft"
    assert item["listing"]["url"] == "https://example.com/l1"
    assert item["listing"]["monthly_price"] == {"amount_minor": 240000, "currency": "USD"}
    assert (item["listing"]["beds"], item["listing"]["baths"]) == (1, 1.5)
    assert item["listing"]["removed_at"] is None


def test_list_is_empty_without_properties(api: TestClient) -> None:
    assert api.get("/api/v1/properties").json() == {
        "items": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
    }


def test_list_filters_by_one_or_more_statuses(api: TestClient, seed: Seed) -> None:
    suggested = seed(status=S.SUGGESTED)
    approved = seed(status=S.APPROVED)
    seed(status=S.DENIED)

    def ids(query: str) -> set[int]:
        return {item["property"]["id"] for item in api.get(query).json()["items"]}

    assert ids("/api/v1/properties?status=approved") == {approved}
    assert ids("/api/v1/properties?status=approved&status=suggested") == {approved, suggested}
    assert len(ids("/api/v1/properties")) == 3


def test_list_searches_title_and_address(api: TestClient, seed: Seed) -> None:
    loft = seed(title="Sunny loft")
    seed(title="Dark basement")

    body = api.get("/api/v1/properties", params={"q": "  LOFT "}).json()

    assert [item["property"]["id"] for item in body["items"]] == [loft]
    assert body["total"] == 1


def test_list_sorts(api: TestClient, seed: Seed) -> None:
    low = seed(score=10, price=Price(amount=Money.from_major(900)))
    high = seed(score=90, price=Price(amount=Money.from_major(3000)))

    def order(sort: str) -> list[int]:
        items = api.get("/api/v1/properties", params={"sort": sort}).json()["items"]
        return [item["property"]["id"] for item in items]

    assert order("score") == [high, low]
    assert order("price") == [low, high]
    assert order("newest") == [high, low]


def test_list_pages_with_limit_and_offset(api: TestClient, seed: Seed) -> None:
    ids = [seed(score=score) for score in (50, 40, 30)]

    first = api.get("/api/v1/properties", params={"limit": 2}).json()
    second = api.get("/api/v1/properties", params={"limit": 2, "offset": 2}).json()

    assert [item["property"]["id"] for item in first["items"]] == ids[:2]
    assert [item["property"]["id"] for item in second["items"]] == ids[2:]
    assert first["total"] == second["total"] == 3
    assert (second["limit"], second["offset"]) == (2, 2)


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": MAX_LIMIT + 1},
        {"offset": -1},
        {"status": "nope"},
        {"sort": "nope"},
        {"q": "x" * 201},
    ],
)
def test_list_rejects_bad_query_parameters(api: TestClient, params: dict[str, str | int]) -> None:
    assert api.get("/api/v1/properties", params=params).status_code == 422


def test_list_hides_snoozed_properties_unless_asked(
    api: TestClient, seed: Seed, store: SqlPropertyStore
) -> None:
    visible = seed()
    snoozed = seed()
    store.snooze(snoozed, until=utcnow() + timedelta(days=3), actor=Actor.DASHBOARD, at=utcnow())

    def ids(params: dict[str, str]) -> set[int]:
        items = api.get("/api/v1/properties", params=params).json()["items"]
        return {item["property"]["id"] for item in items}

    assert ids({}) == {visible}
    assert ids({"include_snoozed": "true"}) == {visible, snoozed}


# --- get + listings ----------------------------------------------------------


def test_get_returns_everything_about_the_property(
    api: TestClient, seed: Seed, store: SqlPropertyStore
) -> None:
    property_id = seed(
        status=S.NEW,
        title="Sunny loft",
        description="Bright and quiet.",
        price=Price(amount=Money.from_major(2400)),
        beds=1,
        sqft=650,
        amenities=["washer", "dishwasher"],
        images=["https://img.example.com/1.jpg"],
        address={"line1": "1 Main St", "city": "Springfield", "region": "IL"},
        contact={"name": "Host", "email": "host@example.com"},
    )
    listing_id = store.detail(property_id).primary.summary.id  # type: ignore[union-attr]
    store.record_match(
        listing_id,
        MatchResult(
            passed=True,
            score=77,
            breakdown=(BreakdownItem(label="Price fit", points=30, max_points=40, reason="ok"),),
            rules=(
                RuleResult(
                    rule=FilterRule.MIN_BEDS, status=RuleStatus.PASS, reason="1 bed", blocking=False
                ),
            ),
            criteria_version=3,
        ),
        at=T0,
    )
    store.add_note(property_id, "Call **Monday**", actor=Actor.DASHBOARD, at=T0)

    body = api.get(f"/api/v1/properties/{property_id}").json()

    assert body["property"]["id"] == property_id
    assert body["match"]["score"] == 77
    assert body["match"]["breakdown"][0] == {
        "label": "Price fit",
        "points": 30,
        "max_points": 40,
        "reason": "ok",
    }
    assert body["match"]["rules"][0]["rule"] == "min_beds"
    assert body["match"]["criteria_version"] == 3
    (listing,) = body["listings"]
    assert listing["id"] == listing_id
    assert listing["title"] == "Sunny loft"
    assert listing["description"] == "Bright and quiet."
    assert listing["price"]["amount"] == {"amount_minor": 240000, "currency": "USD"}
    assert listing["price"]["period"] == "monthly"
    assert listing["sqft"] == 650
    assert listing["amenities"] == ["dishwasher", "washer"]
    assert listing["images"] == ["https://img.example.com/1.jpg"]
    assert listing["address"]["city"] == "Springfield"
    assert listing["contact"]["email"] == "host@example.com"
    assert listing["price_history"][0]["price"]["amount"]["amount_minor"] == 240000
    assert "raw" not in listing
    # Created, then suggested by the passing match.
    assert [(e["from_status"], e["to_status"]) for e in body["audit"]] == [
        (None, "new"),
        ("new", "suggested"),
    ]
    assert body["audit"][0]["actor"] == "system"
    (note,) = body["notes"]
    assert note["body_md"] == "Call **Monday**"
    assert note["actor"] == "dashboard"


def test_get_an_unmatched_property_has_no_match(api: TestClient, seed: Seed) -> None:
    body = api.get(f"/api/v1/properties/{seed()}").json()

    assert body["match"] is None
    assert body["notes"] == []


def test_listings_lists_the_propertys_listings(api: TestClient, seed: Seed) -> None:
    property_id = seed(title="Sunny loft")

    (listing,) = api.get(f"/api/v1/properties/{property_id}/listings").json()

    assert listing["title"] == "Sunny loft"
    assert listing["source"] == "test"
    assert listing["source_id"] == "l1"


@pytest.mark.parametrize("suffix", ["", "/listings"])
def test_an_unknown_property_is_404(api: TestClient, suffix: str) -> None:
    response = api.get(f"/api/v1/properties/999{suffix}")

    assert response.status_code == 404
    assert response.json() == {"detail": "no property with id 999"}


def test_a_404_is_json_even_for_a_browser(api: TestClient) -> None:
    response = api.get("/api/v1/properties/999", headers={"Accept": "text/html"})

    assert response.status_code == 404
    assert response.json() == {"detail": "no property with id 999"}


def test_a_non_numeric_id_is_422(api: TestClient) -> None:
    assert api.get("/api/v1/properties/abc").status_code == 422


# --- transitions ---------------------------------------------------------------


def transition(api: TestClient, property_id: int, **body: object) -> dict[str, object]:
    response = api.post(f"/api/v1/properties/{property_id}/transitions", json=body)
    assert response.status_code == 200, response.text
    result: dict[str, object] = response.json()
    return result


def test_an_allowed_transition_moves_and_is_audited_as_the_api(
    api: TestClient, seed: Seed, store: SqlPropertyStore
) -> None:
    property_id = seed(status=S.SUGGESTED)

    body = transition(api, property_id, to="approved")

    assert body["status"] == "approved"
    assert body["allowed_transitions"] == ["denied", "inquiry_drafted", "archived"]
    last = store.audit_events(property_id)[-1]
    assert (last.from_status, last.to_status, last.actor) == (S.SUGGESTED, S.APPROVED, Actor.API)


def test_a_forbidden_transition_is_409_with_the_reason(
    api: TestClient, seed: Seed, store: SqlPropertyStore
) -> None:
    property_id = seed(status=S.SUGGESTED)

    response = api.post(f"/api/v1/properties/{property_id}/transitions", json={"to": "won"})

    assert response.status_code == 409
    assert response.json() == {"detail": "not an allowed transition"}
    assert store.get(property_id).status is S.SUGGESTED  # type: ignore[union-attr]


def test_the_api_cannot_mark_a_property_unavailable(api: TestClient, seed: Seed) -> None:
    response = api.post(f"/api/v1/properties/{seed()}/transitions", json={"to": "unavailable"})

    assert response.status_code == 409
    assert response.json() == {"detail": "only the system marks a property unavailable"}


def test_deny_with_a_reason_saves_it_as_a_note(
    api: TestClient, seed: Seed, store: SqlPropertyStore
) -> None:
    property_id = seed(status=S.SUGGESTED)

    body = transition(api, property_id, to="denied", reason="Too far from work")

    assert body["status"] == "denied"
    (note,) = store.notes(property_id)
    assert (note.body_md, note.actor) == ("Too far from work", Actor.API)
    last = store.audit_events(property_id)[-1]
    assert last.to_status is S.DENIED
    assert last.payload == {"note_id": note.id}


def test_deny_without_a_reason_adds_no_note(
    api: TestClient, seed: Seed, store: SqlPropertyStore
) -> None:
    property_id = seed(status=S.SUGGESTED)

    transition(api, property_id, to="denied")

    assert store.notes(property_id) == []


def test_a_too_long_deny_reason_is_422_and_changes_nothing(
    api: TestClient, seed: Seed, store: SqlPropertyStore
) -> None:
    property_id = seed(status=S.SUGGESTED)

    response = api.post(
        f"/api/v1/properties/{property_id}/transitions",
        json={"to": "denied", "reason": "x" * (MAX_NOTE_LENGTH + 1)},
    )

    assert response.status_code == 422
    assert store.get(property_id).status is S.SUGGESTED  # type: ignore[union-attr]
    assert store.notes(property_id) == []


def test_only_deny_takes_a_reason(api: TestClient, seed: Seed, store: SqlPropertyStore) -> None:
    property_id = seed(status=S.SUGGESTED)

    response = api.post(
        f"/api/v1/properties/{property_id}/transitions", json={"to": "approved", "reason": "nice"}
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "only a deny takes a reason"}
    assert store.get(property_id).status is S.SUGGESTED  # type: ignore[union-attr]


@pytest.mark.parametrize("body", [{"to": "nope"}, {}, {"to": "approved", "extra": 1}, {"to": None}])
def test_an_invalid_transition_body_is_422(api: TestClient, seed: Seed, body: object) -> None:
    response = api.post(f"/api/v1/properties/{seed()}/transitions", json=body)

    assert response.status_code == 422


@pytest.mark.parametrize("to", ["approved", "denied"])
def test_transitioning_an_unknown_property_is_404(api: TestClient, to: str) -> None:
    response = api.post("/api/v1/properties/999/transitions", json={"to": to})

    assert response.status_code == 404


# --- notes ---------------------------------------------------------------------


def test_add_note(api: TestClient, seed: Seed, store: SqlPropertyStore) -> None:
    property_id = seed()

    response = api.post(
        f"/api/v1/properties/{property_id}/notes", json={"body": "  Ask about **parking**  "}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["property_id"] == property_id
    assert body["body_md"] == "Ask about **parking**"
    assert body["actor"] == "api"
    assert body["actor_detail"] is None
    assert [note.id for note in store.notes(property_id)] == [body["id"]]


@pytest.mark.parametrize("text", ["", "   ", "x" * (MAX_NOTE_LENGTH + 1)])
def test_an_empty_or_too_long_note_is_422(
    api: TestClient, seed: Seed, store: SqlPropertyStore, text: str
) -> None:
    property_id = seed()

    response = api.post(f"/api/v1/properties/{property_id}/notes", json={"body": text})

    assert response.status_code == 422
    assert store.notes(property_id) == []


def test_a_note_without_a_body_is_422(api: TestClient, seed: Seed) -> None:
    assert api.post(f"/api/v1/properties/{seed()}/notes", json={}).status_code == 422


def test_a_note_on_an_unknown_property_is_404(api: TestClient) -> None:
    response = api.post("/api/v1/properties/999/notes", json={"body": "hi"})

    assert response.status_code == 404


# --- docs ----------------------------------------------------------------------


def test_the_schema_needs_auth(client: TestClient) -> None:
    assert client.get("/api/openapi.json").status_code == 401


def test_the_schema_documents_only_the_api(api: TestClient) -> None:
    schema = api.get("/api/openapi.json").json()

    assert set(schema["paths"]) == {
        "/api/v1/properties",
        "/api/v1/properties/{property_id}",
        "/api/v1/properties/{property_id}/listings",
        "/api/v1/properties/{property_id}/transitions",
        "/api/v1/properties/{property_id}/notes",
    }
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    get = schema["paths"]["/api/v1/properties/{property_id}"]["get"]
    assert {"200", "401", "404"} <= set(get["responses"])
    post = schema["paths"]["/api/v1/properties/{property_id}/transitions"]["post"]
    assert "409" in post["responses"]


def test_a_logged_in_browser_can_read_the_schema(authed_client: TestClient) -> None:
    assert authed_client.get("/api/openapi.json").status_code == 200


def test_fastapis_own_docs_routes_are_off(api: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert api.get(path).status_code == 404


def test_the_docs_page_redirects_to_login(client: TestClient) -> None:
    response = client.get("/api/docs", headers={"Accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fapi%2Fdocs"


def test_the_docs_page_loads_the_vendored_swagger_ui(authed_client: TestClient) -> None:
    response = authed_client.get("/api/docs", headers={"Accept": "text/html"})

    assert response.status_code == 200
    html = response.text
    assert 'href="/static/vendor/swagger-ui/swagger-ui.css"' in html
    assert 'src="/static/vendor/swagger-ui/swagger-ui-bundle.js"' in html
    assert 'src="/static/js/api-docs.js"' in html
    assert 'data-spec-url="/api/openapi.json"' in html
    assert "<script>" not in html
    assert "script-src 'self'" in response.headers["content-security-policy"]
    for asset in (
        "/static/vendor/swagger-ui/swagger-ui.css",
        "/static/vendor/swagger-ui/swagger-ui-bundle.js",
        "/static/js/api-docs.js",
    ):
        assert authed_client.get(asset).status_code == 200


def test_the_nav_links_to_the_api_docs(authed_client: TestClient) -> None:
    assert 'href="/api/docs"' in authed_client.get("/", headers={"Accept": "text/html"}).text


def test_a_merged_property_says_where_it_went_and_refuses_changes(
    api: TestClient, seed: Seed, session_factory: sessionmaker[Session]
) -> None:
    survivor, absorbed = seed(), seed()
    with session_scope(session_factory) as session:
        session.execute(
            update(Property).where(Property.id == absorbed).values(merged_into_id=survivor)
        )

    body = api.get(f"/api/v1/properties/{absorbed}").json()
    assert body["property"]["merged_into_id"] == survivor
    assert body["property"]["allowed_transitions"] == []
    assert api.get(f"/api/v1/properties/{survivor}").json()["property"]["merged_into_id"] is None
    listed = api.get("/api/v1/properties").json()
    assert [item["property"]["id"] for item in listed["items"]] == [survivor]

    moved = api.post(f"/api/v1/properties/{absorbed}/transitions", json={"to": "approved"})
    assert moved.status_code == 409
    assert moved.json()["detail"] == f"property {absorbed} was merged into {survivor}"
    noted = api.post(f"/api/v1/properties/{absorbed}/notes", json={"body": "x"})
    assert noted.status_code == 409
