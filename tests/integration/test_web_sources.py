"""The source health page: what it shows, the enable toggle and "run now", end to end."""

from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import AdapterOverrides, Settings, SourceOverrides, SourcesSettings
from furnest.db.criteria_store import CriteriaStore
from furnest.db.listing_store import RunCounts, SqlListingStore
from furnest.db.models import Job, JobStatus, SourceRunStatus
from furnest.db.session import session_scope
from furnest.db.settings_store import SqlSettingsStore
from furnest.db.source_store import SqlSourceStore
from furnest.domain import Area, Criteria, NormalizedListing
from furnest.pipeline.schedule import RUN_SOURCE
from furnest.sources import Capabilities, Credential, SourceAdapter, StubAdapter
from furnest.sources.apify import ApifyAdapter
from furnest.sources.budget import USAGE_KEY, ApifyUsage
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
    credential: ClassVar[Credential | None] = Credential(
        "keyed", "Keyed token", "https://keyed.example.com/tokens"
    )


class Stubby(StubAdapter):
    name: ClassVar[str] = "stubby"
    display_name: ClassVar[str] = "Stubby"
    site_url: ClassVar[str] = "https://stubby.example.com/"


# Distinct from every other string on the page, so finding it means the token leaked.
LEAK = "leak-token-5c21e9"


@pytest.fixture
def client(migrated_engine: Engine, login: LoginFn) -> Iterator[TestClient]:
    """Logged in, with five fake sources; ``pinned`` has ``enabled`` and ``max_results``
    set in SOURCES_ADAPTERS, ``keyed`` needs the shared "Keyed token", ``stubby`` is a stub."""
    registry = SourceRegistry()
    for adapter in (Alpha, Beta, Pinned, Keyed, Stubby):
        registry.register(adapter)
    settings = Settings(
        sources=SourcesSettings(
            default_interval_minutes=60,
            adapters={"pinned": AdapterOverrides(enabled=True, max_results=9)},
        )
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
        assert anonymous.post("/sources/run-all", follow_redirects=False).status_code in (
            303,
            401,
            403,
        )


def test_posts_need_csrf(client: TestClient) -> None:
    assert client.post("/sources/alpha/run").status_code == 403
    assert client.post("/sources/run-all").status_code == 403
    assert client.post("/sources/alpha/enabled", data={"enabled": "false"}).status_code == 403


def test_nav_links_to_the_page(client: TestClient) -> None:
    assert 'href="/sources"' in client.get("/").text


# --- the page ---------------------------------------------------------------


def test_lists_every_source_even_before_it_has_a_row(client: TestClient) -> None:
    page = client.get("/sources")

    assert page.status_code == 200
    for name in ("Alpha Rentals", "Beta Homes", "Pinned Stays", "Keyed Data", "Stubby"):
        assert name in page.text
    assert "never run" in page.text
    assert "set in SOURCES_ADAPTERS" in page.text
    assert "needs the Keyed token" in page.text


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
    [("pinned", "turned on in SOURCES_ADAPTERS"), ("keyed", "needs the Keyed token")],
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


# --- run all ----------------------------------------------------------------


def test_run_all_queues_one_job_per_runnable_source(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    assert "Run all now" in client.get("/sources").text

    response = post(client, csrf, "/sources/run-all")

    assert response.status_code == 303
    queued = jobs(session_factory)
    # keyed has no token and stubby is a stub, so neither can run.
    assert sorted(job.payload["source"] for job in queued) == ["alpha", "beta", "pinned"]
    assert all((job.kind, job.max_attempts) == (RUN_SOURCE, 1) for job in queued)
    page = client.get(response.headers["location"])
    assert "Queued runs for 3 sources.</p>" in page.text


def test_run_all_skips_sources_already_queued(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    post(client, csrf, "/sources/alpha/run")

    response = post(client, csrf, "/sources/run-all")

    sources = [job.payload["source"] for job in jobs(session_factory)]
    assert sorted(sources) == ["alpha", "beta", "pinned"]
    page = client.get(response.headers["location"])
    assert "Queued runs for 2 sources. 1 already queued." in page.text


def test_run_all_with_nothing_runnable_is_409(
    migrated_engine: Engine,
    login: LoginFn,
    csrf: Csrf,
    sources: SqlSourceStore,
    session_factory: sessionmaker[Session],
) -> None:
    registry = SourceRegistry()
    for adapter in (Alpha, Keyed, Stubby):
        registry.register(adapter)
    sources.set_enabled("alpha", False)
    app = create_app(Settings(), engine=migrated_engine, source_registry=registry)
    with TestClient(app) as client:
        login(client)
        assert "Run all now" not in client.get("/sources").text

        response = post(client, csrf, "/sources/run-all")

    assert response.status_code == 409
    assert "No source can run now" in response.text
    assert jobs(session_factory) == []


# --- the switch ---------------------------------------------------------------


def test_switch_renders_checked_for_an_enabled_source(client: TestClient) -> None:
    text = client.get("/sources").text

    alpha = text[text.index('id="source-alpha"') : text.index('id="source-beta"')]
    assert "data-autosubmit" in alpha
    assert '<input type="checkbox" name="enabled" value="true" checked>' in alpha
    assert "switch-apply" in alpha  # the no-JS fallback button
    assert "js/sources.js" in text


def test_switch_posts_without_enabled_mean_off(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    response = post(client, csrf, "/sources/alpha/enabled")

    assert response.status_code == 303
    state = SqlSourceStore(session_factory).get("alpha")
    assert state is not None
    assert state.enabled is False
    text = client.get("/sources").text
    alpha = text[text.index('id="source-alpha"') : text.index('id="source-beta"')]
    assert '<input type="checkbox" name="enabled" value="true">' in alpha


# --- credentials --------------------------------------------------------------


def credential_row(text: str) -> str:
    start = text.index('id="credential-keyed"')
    return text[start : text.index("</tr>", start)]


def test_credentials_section_lists_each_credential_and_its_users(client: TestClient) -> None:
    text = client.get("/sources").text

    row = credential_row(text)
    assert "Keyed token" in row
    assert 'href="https://keyed.example.com/tokens"' in row
    assert "not set" in row
    assert "Keyed Data" in row
    keyed = text[text.index('id="source-keyed"') : text.index('id="source-pinned"')]
    assert "needs the Keyed token" in keyed
    assert 'href="#credential-keyed"' in keyed
    assert "Settings" in keyed


def test_saving_a_credential_unblocks_its_sources_and_never_shows_the_token(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    response = post(client, csrf, "/sources/credentials/keyed", value=f"  {LEAK}  ")

    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert "Saved the Keyed token." in page.text
    assert LEAK not in page.text
    assert LEAK not in client.get("/settings").text
    assert ">set<" in credential_row(page.text)
    assert "Clear" in credential_row(page.text)
    keyed = page.text[page.text.index('id="source-keyed"') : page.text.index('id="source-pinned"')]
    assert "health-never-run" in keyed
    assert "needs the Keyed token" not in keyed
    assert SqlSourceStore(session_factory).credential("keyed") == LEAK

    # The toggle and "run now" work now that the source can run.
    assert post(client, csrf, "/sources/keyed/enabled", enabled="false").status_code == 303


def test_clearing_a_credential_blocks_its_sources_again(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    post(client, csrf, "/sources/credentials/keyed", value=LEAK)

    response = post(client, csrf, "/sources/credentials/keyed/clear")

    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert "Cleared the Keyed token." in page.text
    assert "not set" in credential_row(page.text)
    assert "needs the Keyed token" in page.text
    assert SqlSourceStore(session_factory).credential("keyed") is None


def test_a_blank_credential_is_refused(client: TestClient, csrf: Csrf) -> None:
    response = post(client, csrf, "/sources/credentials/keyed", value="   ")

    assert response.status_code == 422
    assert "Enter the Keyed token." in response.text


def test_unknown_credential_is_404(client: TestClient, csrf: Csrf) -> None:
    assert post(client, csrf, "/sources/credentials/nope", value="x").status_code == 404
    assert post(client, csrf, "/sources/credentials/nope/clear").status_code == 404
    assert client.get("/sources?credential=nope").status_code == 404


def test_a_source_with_its_own_env_key_is_marked_on_the_credential(
    migrated_engine: Engine, login: LoginFn
) -> None:
    registry = SourceRegistry()
    registry.register(Keyed)
    settings = Settings(
        sources=SourcesSettings(
            adapters={"keyed": AdapterOverrides(api_key="env-key-0123456789")}  # type: ignore[arg-type]
        )
    )
    app = create_app(settings, engine=migrated_engine, source_registry=registry)
    with TestClient(app) as client:
        login(client)
        text = client.get("/sources").text

    assert "own key in SOURCES_ADAPTERS" in credential_row(text)
    assert "env-key-0123456789" not in text
    assert "needs the Keyed token" not in text


# --- per-source settings -------------------------------------------------------


def settings_form(text: str, name: str) -> str:
    start = text.index(f'id="settings-{name}"')
    return text[start : text.index("</details>", start)]


def test_settings_form_shows_defaults_as_placeholders_and_the_hint(client: TestClient) -> None:
    text = client.get("/sources").text

    form = settings_form(text, "alpha")
    assert 'placeholder="60"' in form  # SOURCES_DEFAULT_INTERVAL_MINUTES
    assert 'placeholder="100"' in form  # SOURCES_MAX_RESULTS
    assert "Place names, one per line" in form
    assert 'id="settings-stubby"' not in text  # a stub has no settings form


def test_saving_settings_stores_them_and_shows_them_back(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    response = post(
        client,
        csrf,
        "/sources/alpha/settings",
        search_locations="Springfield, IL\n Capital City, IL \r\n\nShelbyville, IL\n\n",
        interval_minutes=" 30 ",
        max_results="",
    )

    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert "Saved the settings for alpha." in page.text
    state = SqlSourceStore(session_factory).get("alpha")
    assert state is not None
    assert state.overrides == SourceOverrides(
        search_locations=("Springfield, IL", "Capital City, IL", "Shelbyville, IL"),
        interval_minutes=30,
    )
    form = settings_form(page.text, "alpha")
    assert "Springfield, IL\nCapital City, IL\nShelbyville, IL</textarea>" in form
    assert 'name="interval_minutes" min="1" step="1" placeholder="60" value="30"' in form
    assert 'name="max_results" min="1" step="1" placeholder="100" value=""' in form


def test_invalid_settings_are_refused_with_the_field_marked(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    response = post(
        client, csrf, "/sources/alpha/settings", interval_minutes="soon", max_results="0"
    )

    assert response.status_code == 422
    assert "were not saved" in response.text
    form = settings_form(response.text, "alpha")
    assert 'id="settings-alpha" open' in form
    assert 'value="soon" aria-invalid="true"' in form
    assert "enter a whole number" in form
    assert "enter a number of 1 or more" in form
    assert 'id="settings-beta" open' not in response.text
    state = SqlSourceStore(session_factory).get("alpha")
    assert state is None or state.overrides == SourceOverrides()


def test_reset_clears_the_stored_settings(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    post(client, csrf, "/sources/alpha/settings", max_results="5")

    response = post(client, csrf, "/sources/alpha/settings/reset")

    assert response.status_code == 303
    assert "alpha is back to its defaults." in client.get(response.headers["location"]).text
    state = SqlSourceStore(session_factory).get("alpha")
    assert state is not None
    assert state.overrides == SourceOverrides()


def test_a_field_set_in_env_is_read_only(client: TestClient, csrf: Csrf) -> None:
    text = client.get("/sources").text
    form = settings_form(text, "pinned")
    assert 'type="number" value="9" disabled' in form
    assert "set in SOURCES_ADAPTERS" in form
    assert 'name="interval_minutes"' in form  # the other fields stay editable

    refused = post(client, csrf, "/sources/pinned/settings", max_results="3")
    assert refused.status_code == 409
    assert "max_results for pinned is set in SOURCES_ADAPTERS" in refused.text

    allowed = post(client, csrf, "/sources/pinned/settings", interval_minutes="15")
    assert allowed.status_code == 303


def test_settings_refuse_a_stub_and_an_unknown_source(client: TestClient, csrf: Csrf) -> None:
    assert post(client, csrf, "/sources/stubby/settings", max_results="3").status_code == 409
    assert post(client, csrf, "/sources/stubby/settings/reset").status_code == 409
    assert post(client, csrf, "/sources/nope/settings", max_results="3").status_code == 404
    assert post(client, csrf, "/sources/nope/settings/reset").status_code == 404


# --- criteria cities as default search locations ----------------------------


def save_areas(session_factory: sessionmaker[Session], *names: str) -> None:
    areas = tuple(Area(name=name, lat=30.27, lng=-97.74, radius=50) for name in names)
    CriteriaStore(session_factory).save(Criteria(areas=areas), created_by="test")


def test_criteria_cities_are_offered_when_locations_are_empty(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    save_areas(session_factory, "Austin, TX", "Austin, TX", "Dallas, TX", "Downtown")

    form = settings_form(client.get("/sources").text, "alpha")

    assert "Use criteria cities: Austin, TX; Dallas, TX" in form
    assert 'action="/sources/alpha/settings/use-criteria-cities"' in form


def test_no_offer_without_city_areas_or_with_locations_set(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    assert "Use criteria cities" not in client.get("/sources").text
    save_areas(session_factory, "Downtown")
    assert "Use criteria cities" not in client.get("/sources").text
    save_areas(session_factory, "Austin, TX")
    post(client, csrf, "/sources/alpha/settings", search_locations="Waco, TX")
    assert "Use criteria cities" not in settings_form(client.get("/sources").text, "alpha")


def test_no_offer_for_sources_that_take_site_names(
    client: TestClient, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Alpha, "search_locations_are_places", False)
    monkeypatch.setattr(Beta, "search_locations_are_places", True)
    save_areas(session_factory, "Austin, TX")

    text = client.get("/sources").text

    assert "Use criteria cities" not in settings_form(text, "alpha")
    assert "Use criteria cities" in settings_form(text, "beta")


def test_using_criteria_cities_stores_them_and_keeps_other_settings(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    save_areas(session_factory, "Austin, TX")
    post(client, csrf, "/sources/alpha/settings", interval_minutes="30")

    response = post(client, csrf, "/sources/alpha/settings/use-criteria-cities")

    assert response.status_code == 303
    state = SqlSourceStore(session_factory).get("alpha")
    assert state is not None
    assert state.overrides == SourceOverrides(search_locations=("Austin, TX",), interval_minutes=30)
    assert "Use criteria cities" not in settings_form(client.get("/sources").text, "alpha")


def test_using_criteria_cities_is_refused_when_there_are_none(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    response = post(client, csrf, "/sources/alpha/settings/use-criteria-cities")

    assert response.status_code == 409
    state = SqlSourceStore(session_factory).get("alpha")
    assert state is None or state.overrides.search_locations is None


# --- Apify spend (#216) ------------------------------------------------------------


class Billed(ApifyAdapter):
    name: ClassVar[str] = "billed"
    display_name: ClassVar[str] = "Billed Data"
    default_enabled: ClassVar[bool] = True
    job: ClassVar[str] = "someone/some-actor"
    site: ClassVar[str] = "https://example.com"
    start_cost_usd: ClassVar[float] = 0.05
    result_cost_usd: ClassVar[float] = 0.01

    def build_input(self, criteria: Criteria) -> dict[str, Any]:
        return {}

    def parse_items(
        self, items: list[dict[str, Any]], run_input: Mapping[str, Any]
    ) -> list[NormalizedListing]:
        return []


class BrokenBilled(Billed):
    name: ClassVar[str] = "broken_billed"
    display_name: ClassVar[str] = "Broken Billed"
    broken_reason: ClassVar[str | None] = "its actor rejects every search"


def seed_usage(
    session_factory: sessionmaker[Session], used_usd: float, plan_limit_usd: float = 19.0
) -> None:
    """A fresh usage reading on a Starter plan, so the page doesn't call Apify."""
    usage = ApifyUsage(
        used_usd=used_usd,
        plan_limit_usd=plan_limit_usd,
        cycle_end=datetime(2026, 11, 6, 23, 59, 59, tzinfo=UTC),
        fetched_at=datetime.now(UTC),
    )
    SqlSettingsStore(session_factory).set(USAGE_KEY, usage.to_json())


@pytest.fixture
def billed_client(migrated_engine: Engine, login: LoginFn) -> Iterator[TestClient]:
    """Alpha (direct), Billed (Apify, with a token in SOURCES_ADAPTERS) and a broken
    Apify source."""
    registry = SourceRegistry()
    for adapter in (Alpha, Billed, BrokenBilled):
        registry.register(adapter)
    settings = Settings(
        sources=SourcesSettings(
            adapters={"billed": AdapterOverrides(api_key=SecretStr("apify-token"))}
        )
    )
    app = create_app(settings, engine=migrated_engine, source_registry=registry)
    with TestClient(app) as client:
        login(client)
        yield client


def test_the_apify_panel_shows_usage_budget_and_projection(
    billed_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    seed_usage(session_factory, used_usd=1.25)

    page = billed_client.get("/sources").text

    assert 'id="apify-spend"' in page
    assert "$1.25 of $19.00 (the plan's limit)" in page
    assert "the plan's limit (<code>SOURCES_APIFY_MONTHLY_BUDGET_USD</code> unset)" in page
    assert "the next cycle starts 2026-11-07" in page
    # 30 runs a month of $0.30 each.
    assert "$9.00 a month at the current intervals: Billed Data $9.00" in page
    assert "would cost more than the budget" not in page


def test_the_apify_panel_uses_a_budget_set_below_the_plan_limit(
    migrated_engine: Engine, login: LoginFn, session_factory: sessionmaker[Session]
) -> None:
    registry = SourceRegistry()
    registry.register(Billed)
    settings = Settings(
        sources=SourcesSettings(
            apify_monthly_budget_usd=5,
            adapters={"billed": AdapterOverrides(api_key=SecretStr("apify-token"))},
        )
    )
    seed_usage(session_factory, used_usd=1.25)
    app = create_app(settings, engine=migrated_engine, source_registry=registry)
    with TestClient(app) as client:
        login(client)
        page = client.get("/sources").text

    assert "$1.25 of $5.00;" in page
    assert "$5.00 a month (<code>SOURCES_APIFY_MONTHLY_BUDGET_USD</code>)" in page
    # $9.00 a month of daily runs is over the $5 budget.
    assert "would cost more than the budget" in page


def test_the_apify_panel_warns_when_the_projection_is_over_budget(
    migrated_engine: Engine, login: LoginFn, session_factory: sessionmaker[Session]
) -> None:
    registry = SourceRegistry()
    registry.register(Billed)
    settings = Settings(
        sources=SourcesSettings(
            adapters={
                "billed": AdapterOverrides(api_key=SecretStr("apify-token"), interval_minutes=60)
            }
        )
    )
    seed_usage(session_factory, used_usd=0.0)
    app = create_app(settings, engine=migrated_engine, source_registry=registry)
    with TestClient(app) as client:
        login(client)
        page = client.get("/sources").text

    assert "would cost more than the budget" in page


def test_over_budget_apify_sources_are_paused_and_left_out_of_run_all(
    billed_client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    seed_usage(session_factory, used_usd=18.9)

    page = billed_client.get("/sources").text

    assert "health-paused" in page
    assert "Apify monthly budget reached ($18.90 of $19.00 used); resumes Nov 7" in page
    response = post(billed_client, csrf, "/sources/run-all")
    assert response.status_code == 303
    assert [job.payload["source"] for job in jobs(session_factory)] == ["alpha"]
    refused = post(billed_client, csrf, "/sources/billed/run")
    assert refused.status_code == 409
    assert "Apify monthly budget reached" in refused.text


def test_a_broken_apify_source_is_unavailable_with_its_reason(
    billed_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    seed_usage(session_factory, used_usd=0.0)

    page = billed_client.get("/sources").text

    assert "turned off: its actor rejects every search" in page
