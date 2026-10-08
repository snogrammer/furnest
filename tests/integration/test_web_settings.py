"""The settings page: what it shows, what it hides, and editing runtime settings."""

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import AppSettings, DiscordSettings, InquiryMode, Settings
from furnest.db.settings_store import SqlSettingsStore
from furnest.sources.registry import SourceRegistry
from furnest.web.app import create_app

pytestmark = pytest.mark.integration

Csrf = Callable[[str], str]
LoginFn = Callable[..., Response]

# Distinct from every env var name, so finding it on the page means a value leaked.
LEAK = "leak-sentinel-7f3a"


def make_client(engine: Engine, login: LoginFn, settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings, engine=engine, source_registry=SourceRegistry())
    with TestClient(app) as client:
        login(client)
        yield client


@pytest.fixture
def client(migrated_engine: Engine, login: LoginFn) -> Iterator[TestClient]:
    """Logged in, with a Discord bot token set and INQUIRY_MODE not pinned."""
    settings = Settings(discord=DiscordSettings(bot_token=SecretStr(LEAK)))
    yield from make_client(migrated_engine, login, settings)


@pytest.fixture
def pinned_client(migrated_engine: Engine, login: LoginFn) -> Iterator[TestClient]:
    """Logged in, with INQUIRY_MODE set in the environment."""
    settings = Settings(app=AppSettings(inquiry_mode=InquiryMode.AUTO))
    yield from make_client(migrated_engine, login, settings)


def post(client: TestClient, csrf: Csrf, path: str, **fields: str) -> Response:
    token = csrf(client.get("/settings").text)
    return client.post(path, data={"csrf_token": token, **fields}, follow_redirects=False)


# --- access -----------------------------------------------------------------


def test_page_needs_auth(migrated_engine: Engine) -> None:
    app = create_app(Settings(), engine=migrated_engine, source_registry=SourceRegistry())
    with TestClient(app) as anonymous:
        assert anonymous.get("/settings", follow_redirects=False).status_code in (303, 401)
        assert anonymous.post(
            "/settings/runtime/inquiry_mode", data={"value": "auto"}, follow_redirects=False
        ).status_code in (303, 401, 403)


def test_posts_need_csrf(client: TestClient) -> None:
    assert client.post("/settings/runtime/inquiry_mode", data={"value": "auto"}).status_code == 403
    assert client.post("/settings/runtime/inquiry_mode/reset").status_code == 403


def test_nav_links_to_the_page(client: TestClient) -> None:
    assert 'href="/settings"' in client.get("/").text


# --- the page ---------------------------------------------------------------


def test_shows_integrations_runtime_settings_and_env_groups(client: TestClient) -> None:
    page = client.get("/settings")

    assert page.status_code == 200
    text = page.text
    assert "Discord notifications" in text
    assert "health-incomplete" in text  # a bot token without a channel
    assert "Missing DISCORD_CHANNEL_ID." in text
    assert 'id="setting-inquiry_mode"' in text
    assert '<option value="review" selected>' in text
    assert 'id="group-discord"' in text
    assert "<code>DISCORD_BOT_TOKEN</code>" in text
    assert "<code>SOURCES_USER_AGENT</code>" in text


def test_page_never_shows_secret_values(client: TestClient, auth_token: str) -> None:
    text = client.get("/settings").text

    assert LEAK not in text
    assert auth_token not in text  # AUTH_TOKEN comes from the environment in these tests
    discord = text[text.index('id="group-discord"') :]
    bot_token = discord[discord.index("<code>DISCORD_BOT_TOKEN</code>") :].split("</tr>")[0]
    assert "<code>set</code>" in bot_token


# --- editing ----------------------------------------------------------------


def test_save_stores_the_value_and_flashes(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    response = post(client, csrf, "/settings/runtime/inquiry_mode", value="auto")

    assert response.status_code == 303
    assert SqlSettingsStore(session_factory).get("inquiry_mode") == "auto"
    page = client.get(response.headers["location"])
    assert "Saved inquiry_mode." in page.text
    assert '<option value="auto" selected>' in page.text


def test_reset_drops_the_stored_value(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    post(client, csrf, "/settings/runtime/inquiry_mode", value="auto")

    response = post(client, csrf, "/settings/runtime/inquiry_mode/reset")

    assert response.status_code == 303
    assert SqlSettingsStore(session_factory).get("inquiry_mode") is None
    page = client.get(response.headers["location"])
    assert "inquiry_mode is back to its default." in page.text
    assert '<option value="review" selected>' in page.text


def test_invalid_value_is_refused(
    client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    response = post(client, csrf, "/settings/runtime/inquiry_mode", value="<b>sometimes</b>")

    assert response.status_code == 422
    assert "is not a valid inquiry_mode" in response.text
    assert "<b>sometimes</b>" not in response.text
    assert SqlSettingsStore(session_factory).get("inquiry_mode") is None


def test_unknown_setting_is_404(client: TestClient, csrf: Csrf) -> None:
    assert post(client, csrf, "/settings/runtime/nope", value="x").status_code == 404
    assert post(client, csrf, "/settings/runtime/nope/reset").status_code == 404


def test_pinned_setting_is_read_only(
    pinned_client: TestClient, csrf: Csrf, session_factory: sessionmaker[Session]
) -> None:
    page = pinned_client.get("/settings").text
    row = page[page.index('id="setting-inquiry_mode"') :].split("</tr>")[0]
    assert "set by INQUIRY_MODE" in row
    assert "<form" not in row

    response = post(pinned_client, csrf, "/settings/runtime/inquiry_mode", value="review")

    assert response.status_code == 409
    assert "inquiry_mode is set by INQUIRY_MODE." in response.text
    assert SqlSettingsStore(session_factory).get("inquiry_mode") is None
