"""The criteria editor, version history, activation and the re-run job, end to end."""

import threading
import time
from collections.abc import Callable
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import Settings, WorkerSettings
from furnest.db.criteria_store import CriteriaStore
from furnest.db.models import Job, JobStatus
from furnest.db.session import session_scope
from furnest.domain import Criteria, FilterRule, Money
from furnest.worker.execution import default_registry
from furnest.worker.runtime import Worker

pytestmark = pytest.mark.integration

HTMX = {"HX-Request": "true"}
Csrf = Callable[[str], str]


def form(client: TestClient, csrf: Csrf, **fields: object) -> dict[str, object]:
    """A minimal valid submission plus ``fields``, with a fresh CSRF token."""
    return {"csrf_token": csrf(client.get("/criteria").text), "currency": "USD", **fields}


def jobs(session_factory: sessionmaker[Session]) -> list[Job]:
    with session_scope(session_factory) as session:
        return list(session.scalars(select(Job)))


# --- access -----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/criteria",
        "/criteria/history",
        "/criteria/rows/area?index=0",
        "/criteria/places?q=springf",
        "/criteria/rows/area/lookup?index=0",
    ],
)
def test_pages_need_auth(client: TestClient, path: str) -> None:
    assert client.get(path, follow_redirects=False).status_code in (303, 401)


def test_post_without_csrf_is_rejected(authed_client: TestClient) -> None:
    assert authed_client.post("/criteria", data={"min_beds": "1"}).status_code == 403
    assert authed_client.post("/criteria/rerun").status_code == 403


# --- editor -----------------------------------------------------------------


def test_editor_renders_every_section(authed_client: TestClient) -> None:
    page = authed_client.get("/criteria")
    assert page.status_code == 200
    for legend in (
        "Location",
        "Price (monthly)",
        "Property",
        "Dates",
        "Amenities",
        "Keywords",
        "Scoring",
        "Missing data",
    ):
        assert f"<legend>{legend}" in page.text
    assert "Not saved yet" in page.text
    assert "Denied owner types" in page.text
    assert 'name="owner_types_deny" value="agent"' in page.text
    assert 'name="owner_types_deny" value="unknown"' not in page.text
    assert 'name="unknown_fails" value="owner_type"' in page.text
    assert 'name="areas-0-name"' in page.text
    assert 'list="areas-0-places"' in page.text
    assert "<summary>Coordinates</summary>" in page.text
    assert 'name="nice-0-tag"' in page.text
    assert 'aria-current="page"' in page.text
    assert "https://" not in page.text


def test_saving_creates_versions(
    authed_client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    first = authed_client.post(
        "/criteria",
        data=form(
            authed_client,
            csrf,
            **{
                "areas-0-name": "Downtown",
                "areas-0-lat": "40.0",
                "areas-0-lng": "-105.0",
                "areas-0-radius": "5",
                "areas-0-unit": "km",
                "price_max": "2,400",
                "min_beds": "1",
                "furnished_required": "on",
                "must_have_amenities": ["in_unit_laundry", "ac"],
                "nice-0-tag": "pool",
                "nice-0-weight": "2",
                "keywords_deny": "shared bathroom, no smoking",
                "unknown_fails": ["price"],
            },
        ),
        follow_redirects=False,
    )
    assert first.status_code == 303
    assert first.headers["location"] == "/criteria?saved=1"

    store = CriteriaStore(session_factory)
    saved = store.active()
    assert saved is not None
    assert saved.version == 1
    assert saved.created_by == "dashboard"
    criteria = saved.criteria
    assert criteria.areas[0].name == "Downtown"
    assert criteria.price_max == Money(amount_minor=240000)
    assert criteria.must_have_amenities == ("ac", "in_unit_laundry")
    assert criteria.nice_to_have_amenities == {"pool": 2}
    assert criteria.keywords_deny == ("no smoking", "shared bathroom")
    assert criteria.unknown_fails == (FilterRule.PRICE,)

    page = authed_client.get("/criteria?saved=1").text
    assert "Saved as version 1." in page
    assert "Editing version 1 (active)" in page
    assert 'value="Downtown"' in page

    second = authed_client.post(
        "/criteria", data=form(authed_client, csrf, min_beds="2"), follow_redirects=False
    )
    assert second.headers["location"] == "/criteria?saved=2"
    active = store.active()
    assert active is not None
    assert (active.version, active.criteria.min_beds) == (2, 2)


def test_unchanged_save_creates_no_version(
    authed_client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    CriteriaStore(session_factory).save(Criteria(min_beds=1), created_by="cli")
    response = authed_client.post(
        "/criteria", data=form(authed_client, csrf, min_beds="1", furnished_required="on")
    )
    assert response.status_code == 200
    assert "No changes; version 1 is still active." in response.text
    assert len(CriteriaStore(session_factory).history()) == 1


@pytest.mark.parametrize(
    ("fields", "field", "message"),
    [
        ({"min_beds": "lots"}, "min_beds", "valid integer"),
        ({"price_min": "3000", "price_max": "2000"}, None, "price_min must not exceed price_max"),
        ({"price_max": "cheap"}, "price_max", "enter an amount"),
        ({"currency": "XYZ"}, "currency", "unknown currency"),
        (
            {"areas-0-name": "x", "areas-0-lat": "95", "areas-0-lng": "0", "areas-0-radius": "1"},
            "areas-0-lat",
            "less than or equal to 90",
        ),
        ({"nice-0-tag": "pool"}, "nice-0-weight", "enter a weight"),
    ],
)
def test_invalid_input_is_rejected_inline(
    authed_client: TestClient,
    csrf: Csrf,
    session_factory: sessionmaker[Session],
    fields: dict[str, str],
    field: str | None,
    message: str,
) -> None:
    response = authed_client.post("/criteria", data=form(authed_client, csrf, **fields))
    assert response.status_code == 422
    assert "Not saved. Fix the highlighted fields." in response.text
    assert message in response.text
    if field is not None:
        assert f'id="{field}-error"' in response.text
        assert f'aria-describedby="{field}-error"' in response.text
    assert CriteriaStore(session_factory).history() == []


def test_htmx_save_returns_the_form_partial(authed_client: TestClient, csrf: Csrf) -> None:
    response = authed_client.post(
        "/criteria", data=form(authed_client, csrf, min_beds="3"), headers=HTMX
    )
    assert response.status_code == 200
    assert response.text.lstrip().startswith("<form")
    assert "<html" not in response.text
    assert "Saved as version 1." in response.text

    invalid = authed_client.post(
        "/criteria", data=form(authed_client, csrf, min_beds="-1"), headers=HTMX
    )
    assert invalid.status_code == 422
    assert invalid.text.lstrip().startswith("<form")


def test_removed_rows_are_dropped(
    authed_client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    authed_client.post(
        "/criteria",
        data=form(
            authed_client,
            csrf,
            **{
                "nice-0-tag": "pool",
                "nice-0-weight": "1",
                "nice-0-remove": "on",
                "nice-3-tag": "gym",
                "nice-3-weight": "4",
            },
        ),
    )
    active = CriteriaStore(session_factory).active()
    assert active is not None
    assert active.criteria.nice_to_have_amenities == {"gym": 4}


@pytest.mark.parametrize(("kind", "field"), [("area", "areas-4-lat"), ("amenity", "nice-4-tag")])
def test_new_row_partials(authed_client: TestClient, kind: str, field: str) -> None:
    response = authed_client.get(f"/criteria/rows/{kind}?index=4", headers=HTMX)
    assert response.status_code == 200
    assert f'name="{field}"' in response.text
    # The add button is swapped out of band to point at the next index.
    assert 'hx-swap-oob="true"' in response.text
    assert f"/criteria/rows/{kind}?index=5" in response.text


# --- City, State lookup (#179) ----------------------------------------------


def test_places_suggests_matches(authed_client: TestClient) -> None:
    response = authed_client.get("/criteria/places?q=springfield", headers=HTMX)
    assert response.status_code == 200
    assert '<option value="Springfield, IL">' in response.text
    assert response.text.count("<option") <= 10


def test_places_reads_the_row_input(authed_client: TestClient) -> None:
    response = authed_client.get("/criteria/places?index=2&areas-2-name=springfield+il")
    assert '<option value="Springfield, IL">' in response.text
    assert "<option" not in authed_client.get("/criteria/places?q=zzzz").text


def test_row_lookup_fills_coordinates(authed_client: TestClient) -> None:
    response = authed_client.get(
        "/criteria/rows/area/lookup?index=2&areas-2-name=springfield+il&areas-2-unit=km",
        headers=HTMX,
    )
    assert response.status_code == 200
    assert 'value="Springfield, IL"' in response.text
    assert 'name="areas-2-lat" inputmode="decimal" value="39.' in response.text
    assert 'name="areas-2-radius" inputmode="decimal" value="50"' in response.text
    assert '<option value="mi" selected>' in response.text
    assert "hx-swap-oob" not in response.text


def test_row_lookup_reports_an_unknown_city(authed_client: TestClient) -> None:
    response = authed_client.get(
        "/criteria/rows/area/lookup?index=0&areas-0-name=Nowhereville+QQ", headers=HTMX
    )
    assert response.status_code == 200
    assert 'id="areas-0-name-error"' in response.text
    assert 'value="Nowhereville QQ"' in response.text


def test_saving_a_city_name_stores_its_coordinates(
    authed_client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    response = authed_client.post(
        "/criteria",
        data=form(authed_client, csrf, **{"areas-0-name": "springfield, illinois"}),
    )
    assert response.status_code == 200
    active = CriteriaStore(session_factory).active()
    assert active is not None
    (area,) = active.criteria.areas
    assert area.name == "Springfield, IL"
    assert area.radius == 50
    assert 39.5 < area.lat < 40.1


def test_lookup_button_resolves_without_saving(
    authed_client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    response = authed_client.post(
        "/criteria",
        data=form(authed_client, csrf, **{"areas-0-name": "springfield il", "lookup": "0"}),
    )
    assert response.status_code == 200
    assert "Looked up. Review and save." in response.text
    assert 'value="Springfield, IL"' in response.text
    assert CriteriaStore(session_factory).history() == []

    unknown = authed_client.post(
        "/criteria",
        data=form(authed_client, csrf, **{"areas-0-name": "zzzz", "lookup": "0"}),
        headers=HTMX,
    )
    assert unknown.status_code == 422
    assert 'id="areas-0-name-error"' in unknown.text
    assert CriteriaStore(session_factory).history() == []


def test_unknown_row_kind_is_404(authed_client: TestClient) -> None:
    assert authed_client.get("/criteria/rows/castle?index=0").status_code == 404


# --- history and activation -------------------------------------------------


def test_history_shows_versions_and_diffs(
    authed_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    store = CriteriaStore(session_factory)
    store.save(Criteria(min_beds=1), created_by="cli")
    store.save(Criteria(min_beds=2, keywords_deny=("shared",)), created_by="dashboard")

    page = authed_client.get("/criteria/history").text
    assert page.index("Version 2") < page.index("Version 1")
    assert "Active" in page
    assert "min beds" in page
    assert "<code>1</code>" in page
    assert "<code>2</code>" in page
    assert "keywords deny" in page
    assert "First version." in page
    assert 'action="/criteria/history/1/activate"' in page
    assert 'action="/criteria/history/2/activate"' not in page


def test_activate_rolls_back(
    authed_client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    store = CriteriaStore(session_factory)
    store.save(Criteria(min_beds=1), created_by="cli")
    store.save(Criteria(min_beds=2), created_by="cli")
    token = csrf(authed_client.get("/criteria/history").text)

    response = authed_client.post(
        "/criteria/history/1/activate", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    active = store.active()
    assert active is not None
    assert active.version == 1

    missing = authed_client.post("/criteria/history/9/activate", data={"csrf_token": token})
    assert missing.status_code == 404


def test_empty_history(authed_client: TestClient) -> None:
    assert "No versions yet." in authed_client.get("/criteria/history").text


# --- re-run -----------------------------------------------------------------


def test_rerun_needs_saved_criteria(
    authed_client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    token = csrf(authed_client.get("/criteria").text)
    htmx = authed_client.post("/criteria/rerun", data={"csrf_token": token}, headers=HTMX)
    assert htmx.status_code == 409
    assert "Save criteria first" in htmx.text
    assert "<html" not in htmx.text

    plain = authed_client.post("/criteria/rerun", data={"csrf_token": token})
    assert plain.status_code == 409
    assert "Save criteria first" in plain.text
    assert jobs(session_factory) == []


def test_rerun_is_queued_and_processed(
    authed_client: TestClient,
    csrf: Csrf,
    session_factory: sessionmaker[Session],
    migrated_engine: Engine,
) -> None:
    CriteriaStore(session_factory).save(Criteria(min_beds=1), created_by="cli")
    token = csrf(authed_client.get("/criteria").text)

    response = authed_client.post("/criteria/rerun", data={"csrf_token": token}, headers=HTMX)
    assert response.status_code == 200
    [job] = jobs(session_factory)
    assert response.text.strip().endswith(f"Re-run queued (job #{job.id}).</p>")
    assert (job.kind, job.payload, job.status) == (
        "rerun_criteria",
        {"criteria_version": 1},
        JobStatus.QUEUED,
    )

    plain = authed_client.post(
        "/criteria/rerun", data={"csrf_token": token}, follow_redirects=False
    )
    assert plain.status_code == 303
    assert plain.headers["location"] == f"/criteria?rerun={job.id + 1}"
    assert (
        f"Re-run queued (job #{job.id + 1})." in authed_client.get(plain.headers["location"]).text
    )

    settings = Settings(
        worker=WorkerSettings(
            heartbeat_interval_seconds=1,
            poll_interval_seconds=0.05,
            concurrency=1,
            shutdown_timeout_seconds=5,
        )
    )
    worker = Worker(settings, migrated_engine, default_registry(session_factory))
    thread = threading.Thread(target=worker.run, kwargs={"install_signal_handlers": False})
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if all(j.status is JobStatus.SUCCEEDED for j in jobs(session_factory)):
                break
            time.sleep(0.05)
    finally:
        worker.request_stop()
        thread.join(timeout=timedelta(seconds=10).total_seconds())
    assert [j.status for j in jobs(session_factory)] == [JobStatus.SUCCEEDED] * 2
