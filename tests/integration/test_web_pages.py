"""Pages, static files, security headers and error pages."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from furnest.config import Settings
from furnest.web.app import create_app
from furnest.web.headers import CONTENT_SECURITY_POLICY, SECURITY_HEADERS

pytestmark = pytest.mark.integration

HTML = {"Accept": "text/html,application/xhtml+xml"}


def _assert_security_headers(headers: object) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert headers[name] == value, name  # type: ignore[index]


def test_csp_is_strict_self_only() -> None:
    assert "default-src 'self'" in CONTENT_SECURITY_POLICY
    assert "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY


def test_index_renders_layout_with_security_headers(authed_client: TestClient) -> None:
    response = authed_client.get("/", headers=HTML)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    _assert_security_headers(response.headers)
    assert '<meta name="viewport"' in response.text
    assert "/static/vendor/htmx.min.js" in response.text
    assert "/static/css/app.css" in response.text
    assert '"allowEval": false' in response.text
    assert 'aria-current="page"' in response.text


def test_index_answers_head(authed_client: TestClient) -> None:
    response = authed_client.head("/")

    assert response.status_code == 200
    assert response.content == b""
    _assert_security_headers(response.headers)


def test_pages_load_no_external_resources(authed_client: TestClient) -> None:
    text = authed_client.get("/", headers=HTML).text

    assert "http://" not in text
    assert "https://" not in text


@pytest.mark.parametrize(
    ("path", "content_type"),
    [("/static/vendor/htmx.min.js", "javascript"), ("/static/css/app.css", "text/css")],
)
def test_static_assets_are_served(client: TestClient, path: str, content_type: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_health_json_also_gets_security_headers(client: TestClient) -> None:
    _assert_security_headers(client.get("/healthz").headers)


def test_unknown_page_renders_html_404(client: TestClient) -> None:
    response = client.get("/no-such-page", headers=HTML)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Page not found" in response.text
    _assert_security_headers(response.headers)


def test_unknown_path_returns_json_404_for_api_clients(client: TestClient) -> None:
    response = client.get("/no-such-page", headers={"Accept": "application/json"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_missing_static_file_is_404(client: TestClient) -> None:
    assert client.get("/static/nope.js").status_code == 404


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_interactive_docs_are_disabled(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def _app_that_crashes(engine: Engine) -> FastAPI:
    app = create_app(Settings(), engine=engine)

    @app.get("/boom")
    def boom() -> None:
        msg = "secret internal detail"
        raise RuntimeError(msg)

    return app


@pytest.mark.parametrize(
    ("accept", "content_type", "marker"),
    [
        (HTML["Accept"], "text/html", "Something went wrong"),
        ("application/json", "application/json", "Internal Server Error"),
    ],
)
def test_unhandled_error_hides_stack_trace(
    migrated_engine: Engine, accept: str, content_type: str, marker: str
) -> None:
    app = _app_that_crashes(migrated_engine)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom", headers={"Accept": accept})

    assert response.status_code == 500
    assert response.headers["content-type"].startswith(content_type)
    assert marker in response.text
    assert "Traceback" not in response.text
    assert "secret internal detail" not in response.text
    assert "RuntimeError" not in response.text
    _assert_security_headers(response.headers)
