"""The source health page: what it shows, the enable toggle and "run now", end to end."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import AdapterOverrides, Settings, SourcesSettings
from furnest.db.listing_store import RunCounts, SqlListingStore
from furnest.db.models import Job, JobStatus, SourceRunStatus
from furnest.db.session import session_scope
from furnest.db.source_store import SqlSourceStore
from furnest.domain import Criteria, NormalizedListing
from furnest.pipeline.schedule import RUN_SOURCE
from furnest.sources import Capabilities, SourceAdapter
from furnest.sources.registry import SourceRegistry
from furnest.web.app import create_app

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
Csrf = Callable[[str], str]
LoginFn = Callable[..., Response]


class Alpha(SourceAdapter):
    name: ClassVar[str] = "alpha"
    display_name: ClassVar[str] = "Alpha Rentals"
    default_enabled: ClassVar[bool] = True

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        return []


class Beta(Alpha):
    name: ClassVar[str] = "beta"
    display_name: ClassVar[str] = "Beta Homes"


class Pinned(Alpha):
    name: ClassVar[str] = "pinned"
    display_name: ClassVar[str] = "Pinned Stays"


class Keyed(Alpha):
    name: ClassVar[str] = "keyed"
    display_name: ClassVar[str] = "Keyed Data"
    capabilities: ClassVar[Capabilities] = Capabilities(requires_api_key=True)


@pytest.fixture
def client(migrated_engine: Engine, login: LoginFn) -> Iterator[TestClient]:
    """Logged in, with four fake sources; ``pinned`` is pinned on in SOURCES_ADAPTERS."""
    registry = SourceRegistry()
    for adapter in (Alpha, Beta, Pinned, Keyed):
        registry.register(adapter)
    settings = Settings(
        sources=SourcesSettings(adapters={"pinned": AdapterOverrides(enabled=True)})
    )
    app = create_app(settings, engine=migrated_engine, source_registry=registry)
    with TestClient(app) as client:
        login(client)
        yield client


@pytest.fixture
def sources(session_factory: sessionmaker[Session]) -> SqlSourceStore:
    store = SqlSourceStore(session_factory)
    store.ensure(["alpha", "beta", "pinned", "keyed"])
    return store


def jobs(session_factory: sessionmaker[Session]) -> list[Job]:
    with session_scope(session_factory) as session:
        return list(session.scalars(select(Job).order_by(Job.id)))


def post(client: TestClient, csrf: Csrf, path: str, **fields: str) -> Response:
    token = csrf(client.get("/sources").text)
    return client.post(path, data={"csrf_token": token, **fields}, follow_redirects=False)


# --- access -----------------------------------------------------------------


def test_pages_need_auth(migrated_engine: Engine) -> None:
    app = create_app(Settings(), engine=migrated_engine, source_registry=SourceRegistry())
    with TestClient(app) as anonymous:
        assert anonymous.get("/sources", follow_redirects=False).status_code in (303, 401)
        assert anonymous.post("/sources/alpha/run", follow_redirects=False).status_code in (
            303,
            401,
            403,
        )


def test_posts_need_csrf(client: TestClient) -> None:
    assert client.post("/sources/alpha/run").status_code == 403
    assert client.post("/sources/alpha/enabled", data={"enabled": "false"}).status_code == 403


def test_nav_links_to_the_page(client: TestClient) -> None:
    assert 'href="/sources"' in client.get("/").text


# --- the page ---------------------------------------------------------------


def test_lists_every_source_even_before_it_has_a_row(client: TestClient) -> None:
    page = client.get("/sources")

    assert page.status_code == 200
    for name in ("Alpha Rentals", "Beta Homes", "Pinned Stays", "Keyed Data"):
        assert name in page.text
    assert "never run" in page.text
    assert "set in SOURCES_ADAPTERS" in page.text
    assert "needs an API key" in page.text


def test_shows_health_counts_and_recent_runs(
    client: TestClient, sources: SqlSourceStore, session_factory: sessionmaker[Session]
) -> None:
    listings = SqlListingStore(session_factory)
    ok = listings.start_run("alpha", at=T0)
    listings.finish_run(
        ok, status=SourceRunStatus.SUCCESS, at=T0, counts=RunCounts(found=7, new=3, updated=2)
    )
    for _ in range(2):
        bad = listings.start_run("beta", at=T0)
        listings.finish_run(bad, status=SourceRunStatus.FAILED, at=T0, error="HTTPError: 503")
    sources.record_success("alpha", listings_found=7, at=T0)
    sources.record_failure("beta", error="HTTPError: 503", at=T0)
    sources.record_failure("beta", error="HTTPError: 503", at=T0)

    text = client.get("/sources").text

    alpha = text[text.index('id="source-alpha"') : text.index('id="source-beta"')]
    assert "health-ok" in alpha
    assert "2026-10-05 12:00 UTC" in alpha
    beta = text[text.index('id="source-beta"') : text.index('id="source-keyed"')]
    assert "health-failing" in beta
    assert "<td>2</td>" in beta  # consecutive failures and failed runs
    assert "HTTPError: 503" in beta
    runs = text[text.index("Recent runs") :]
    assert "<td>7</td>" in runs
    assert "health-run-failed" in runs


def test_last_error_is_escaped_and_truncated(client: TestClient, sources: SqlSourceStore) -> None:
    sources.record_failure("alpha", error="<script>alert(1)</script>" + "x" * 500, at=T0)

    text = client.get("/sources").text

    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    summary = text[text.index("<summary><code>") : text.index("</code></summary>")]
    assert len(summary) < 200
    assert "x" * 500 in text  # the full error is still there, inside <details>


# --- enable toggle ----------------------------------------------------------


def test_toggle_turns_a_source_off_and_on(client: TestClient, csrf: Csrf) -> None:
    response = post(client, csrf, "/sources/alpha/enabled", enabled="false")
    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert "Updated alpha." in page.text
    alpha = page.text[page.text.index('id="source-alpha"') :]
    assert "health-disabled" in alpha.split("</tr>")[0]

    post(client, csrf, "/sources/alpha/enabled", enabled="true")
    assert (
        "health-never-run"
        in client.get("/sources").text.split('id="source-alpha"')[1].split("</tr>")[0]
    )


def test_toggle_stores_the_choice(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    post(client, csrf, "/sources/beta/enabled", enabled="false")

    state = SqlSourceStore(session_factory).get("beta")
    assert state is not None
    assert state.enabled is False


@pytest.mark.parametrize(
    ("name", "message"),
    [("pinned", "turned on in SOURCES_ADAPTERS"), ("keyed", "needs an API key")],
)
def test_toggle_refuses_pinned_and_unavailable_sources(
    client: TestClient, csrf: Csrf, name: str, message: str
) -> None:
    response = post(client, csrf, f"/sources/{name}/enabled", enabled="false")

    assert response.status_code == 409
    assert message in response.text


def test_toggle_unknown_source_is_404(client: TestClient, csrf: Csrf) -> None:
    response = post(client, csrf, "/sources/nope/enabled", enabled="false")
    assert response.status_code == 404


# --- run now ----------------------------------------------------------------


def test_run_now_queues_one_job(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    response = post(client, csrf, "/sources/alpha/run")

    assert response.status_code == 303
    [job] = jobs(session_factory)
    assert (job.kind, job.payload, job.max_attempts) == (RUN_SOURCE, {"source": "alpha"}, 1)
    assert job.status is JobStatus.QUEUED
    page = client.get(response.headers["location"])
    assert f"Run queued (job #{job.id})." in page.text


def test_run_now_does_not_queue_twice(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    post(client, csrf, "/sources/alpha/run")
    response = post(client, csrf, "/sources/alpha/run")
    post(client, csrf, "/sources/beta/run")

    assert "A run is already queued" in client.get(response.headers["location"]).text
    assert [job.payload["source"] for job in jobs(session_factory)] == ["alpha", "beta"]


def test_run_now_queues_again_once_the_last_run_finished(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    post(client, csrf, "/sources/alpha/run")
    with session_scope(session_factory) as session:
        for job in session.scalars(select(Job)):
            job.status = JobStatus.SUCCEEDED

    post(client, csrf, "/sources/alpha/run")

    assert len(jobs(session_factory)) == 2


def test_run_now_refuses_disabled_and_unavailable_sources(
    client: TestClient, csrf: Csrf, sources: SqlSourceStore, session_factory: sessionmaker[Session]
) -> None:
    sources.set_enabled("alpha", False)

    off = post(client, csrf, "/sources/alpha/run")
    keyed = post(client, csrf, "/sources/keyed/run")

    assert off.status_code == 409
    assert "turned off" in off.text
    assert keyed.status_code == 409
    assert jobs(session_factory) == []


def test_run_now_unknown_source_is_404(client: TestClient, csrf: Csrf) -> None:
    assert post(client, csrf, "/sources/nope/run").status_code == 404
