"""Dashboard auth end to end: login, sessions, Bearer, CSRF, rate limiting, proxies."""

import re
from collections.abc import Callable, Iterator

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine

from furnest.config import LoggingSettings, Settings
from furnest.logging import configure_logging
from furnest.web.app import create_app
from furnest.web.auth import (
    HOST_PREFIX,
    SESSION_COOKIE,
    AuthConfigError,
    require_auth,
    require_bearer,
)
from furnest.web.csrf import CSRF_COOKIE, require_csrf
from furnest.web.headers import SECURITY_HEADERS

pytestmark = pytest.mark.integration

LoginFn = Callable[..., Response]

HTML = {"Accept": "text/html,application/xhtml+xml"}

# How each route that is not a session + CSRF dashboard route authenticates. Every
# other route must depend on both require_auth and require_csrf.
PUBLIC = frozenset[object]()  # reachable without auth
CSRF_ONLY = frozenset({require_csrf})  # login/logout: no session yet, CSRF still checked
BEARER_ONLY = frozenset({require_bearer})  # JSON API and metrics: never cookies
AUTH_ONLY = frozenset({require_auth})  # GET-only API docs
ROUTE_POLICY: dict[str, frozenset[object]] = {
    "/favicon.ico": PUBLIC,
    "/healthz": PUBLIC,
    "/readyz": PUBLIC,
    # Ed25519 request signature, checked in the handler before the body is parsed.
    "/discord/interactions": PUBLIC,
    "/login": CSRF_ONLY,
    "/logout": CSRF_ONLY,
    "/metrics": BEARER_ONLY,
    "/api/openapi.json": AUTH_ONLY,
    "/api/docs": AUTH_ONLY,
}


def _set_cookie_headers(response: object) -> list[str]:
    return response.headers.get_list("set-cookie")  # type: ignore[attr-defined, no-any-return]


def _cookie_header(response: object, name: str) -> str:
    [header] = [h for h in _set_cookie_headers(response) if h.startswith(f"{name}=")]
    return header


# --- Fail closed --------------------------------------------------------------


def test_app_refuses_to_start_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_TOKEN")

    with pytest.raises(AuthConfigError, match="AUTH_TOKEN"):
        create_app(Settings())


def _route_guards(route: APIRoute) -> frozenset[object]:
    guards = {require_auth, require_bearer, require_csrf}
    return frozenset(d.call for d in route.dependant.dependencies if d.call in guards)


def test_every_route_is_guarded_as_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    # Metrics on, so its router is part of the walk.
    monkeypatch.setenv("METRICS_ENABLED", "true")
    app = create_app(Settings())
    # iter_route_contexts descends into included routers; app.routes only holds
    # one wrapper per include_router call.
    routes = [
        ctx.original_route
        for ctx in iter_route_contexts(app.routes)
        if isinstance(ctx.original_route, APIRoute)
    ]
    # Guard against the walk going blind again (it once saw only /favicon.ico).
    assert len(routes) > 40

    for route in routes:
        guards = _route_guards(route)
        expected: frozenset[object]
        if route.path.startswith("/api/v1/"):
            expected = BEARER_ONLY
        else:
            expected = ROUTE_POLICY.get(route.path, frozenset({require_auth, require_csrf}))
        assert guards == expected, f"{route.path} {route.methods} has guards {guards}"


# --- Login ---------------------------------------------------------------------


def test_page_redirects_browser_to_login(client: TestClient) -> None:
    response = client.get("/?tab=new", headers=HTML, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F%3Ftab%3Dnew"


def test_api_client_gets_401_not_redirect(client: TestClient) -> None:
    response = client.get("/", headers={"Accept": "application/json"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Not authenticated"}


def test_login_page_renders_with_csrf_cookie_and_security_headers(
    client: TestClient, csrf: Callable[[str], str]
) -> None:
    response = client.get("/login")

    assert response.status_code == 200
    assert 'type="password"' in response.text
    csrf(response.text)
    header = _cookie_header(response, CSRF_COOKIE)
    assert "HttpOnly" in header
    assert "SameSite=strict" in header
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert "Log out" not in response.text


def test_wrong_token_is_rejected(client: TestClient, login: LoginFn) -> None:
    response = login(client, "not-the-token")

    assert response.status_code == 401
    assert "That token is not valid." in response.text
    assert SESSION_COOKIE not in client.cookies
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 303


def test_correct_token_sets_session_cookie_and_redirects(
    client: TestClient, login: LoginFn, auth_token: str
) -> None:
    response = login(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    header = _cookie_header(response, SESSION_COOKIE)
    assert "HttpOnly" in header
    assert "SameSite=lax" in header
    assert "Max-Age=604800" in header
    assert "Secure" not in header  # AUTH_COOKIE_SECURE=false and plain HTTP
    assert auth_token not in header

    page = client.get("/", headers=HTML)
    assert page.status_code == 200
    assert "Log out" in page.text


def test_login_retires_pre_session_csrf_cookie(
    client: TestClient, login: LoginFn, csrf: Callable[[str], str]
) -> None:
    pre_session = csrf(client.get("/login").text)
    assert CSRF_COOKIE in client.cookies
    login(client)

    assert CSRF_COOKIE not in client.cookies
    assert csrf(client.get("/", headers=HTML).text) != pre_session


def test_csrf_token_is_bound_to_the_session(
    client: TestClient, login: LoginFn, csrf: Callable[[str], str]
) -> None:
    login(client)
    first = csrf(client.get("/", headers=HTML).text)
    assert csrf(client.get("/", headers=HTML).text) == first

    client.post("/logout", data={"csrf_token": first})
    login(client)
    assert csrf(client.get("/", headers=HTML).text) != first


def test_pre_session_pair_is_useless_with_a_session(
    client: TestClient, login: LoginFn, csrf: Callable[[str], str]
) -> None:
    # What a sibling subdomain can get: a validly signed pair from /login.
    page = client.get("/login")
    planted_token = csrf(page.text)
    planted_cookie = client.cookies[CSRF_COOKIE]
    login(client)
    client.cookies.set(CSRF_COOKIE, planted_cookie)

    response = client.post("/logout", data={"csrf_token": planted_token})

    assert response.status_code == 403


def test_cross_site_requests_are_refused(
    client: TestClient, login: LoginFn, csrf: Callable[[str], str]
) -> None:
    login(client)
    token = csrf(client.get("/", headers=HTML).text)
    data = {"csrf_token": token}

    for headers in (
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
    ):
        assert client.post("/logout", data=data, headers=headers).status_code == 403, headers

    ok = client.post(
        "/logout",
        data=data,
        headers={"Sec-Fetch-Site": "same-origin", "Origin": str(client.base_url)},
        follow_redirects=False,
    )
    assert ok.status_code == 303


def test_session_cookie_is_secure_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    migrated_engine: Engine,
    csrf: Callable[[str], str],
    auth_token: str,
) -> None:
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")
    with TestClient(create_app(Settings(), engine=migrated_engine)) as client:
        page = client.get("/login")
        # httpx will not send a Secure cookie over plain HTTP, so hand it back manually.
        csrf_name = HOST_PREFIX + CSRF_COOKIE
        client.cookies.set(csrf_name, re.split("[=;]", _cookie_header(page, csrf_name))[1])
        response = client.post(
            "/login",
            data={"token": auth_token, "csrf_token": csrf(page.text)},
            follow_redirects=False,
        )

    assert response.status_code == 303
    session_header = _cookie_header(response, HOST_PREFIX + SESSION_COOKIE)
    assert "Secure" in session_header
    assert "Path=/" in session_header
    assert "Domain" not in session_header
    assert "Secure" in _cookie_header(page, HOST_PREFIX + CSRF_COOKIE)


def test_login_follows_safe_next_only(
    client: TestClient, csrf: Callable[[str], str], auth_token: str
) -> None:
    form_token = csrf(client.get("/login?next=/x").text)
    ok = client.post(
        "/login",
        data={"token": auth_token, "csrf_token": form_token, "next": "/settings?a=1"},
        follow_redirects=False,
    )
    assert ok.headers["location"] == "/settings?a=1"

    client.cookies.clear()
    form_token = csrf(client.get("/login").text)
    evil = client.post(
        "/login",
        data={"token": auth_token, "csrf_token": form_token, "next": "//evil.example.com/"},
        follow_redirects=False,
    )
    assert evil.headers["location"] == "/"


def test_login_form_carries_sanitised_next(client: TestClient) -> None:
    assert 'name="next" value="/listings"' in client.get("/login?next=/listings").text
    assert 'name="next" value="/"' in client.get("/login?next=https://evil.example.com").text


def test_logged_in_visit_to_login_redirects(authed_client: TestClient) -> None:
    response = authed_client.get("/login?next=/x", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/x"


def test_tampered_session_cookie_is_rejected(authed_client: TestClient) -> None:
    value = authed_client.cookies[SESSION_COOKIE]
    authed_client.cookies.set(SESSION_COOKIE, value[:-2] + ("AA" if value[-2:] != "AA" else "BB"))

    assert authed_client.get("/", headers=HTML, follow_redirects=False).status_code == 303
    assert authed_client.get("/").status_code == 401


def test_logout_clears_session(authed_client: TestClient, csrf: Callable[[str], str]) -> None:
    form_token = csrf(authed_client.get("/", headers=HTML).text)
    response = authed_client.post(
        "/logout", data={"csrf_token": form_token}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert SESSION_COOKIE not in authed_client.cookies
    assert authed_client.get("/", headers=HTML, follow_redirects=False).status_code == 303


def test_log_out_everywhere_revokes_stolen_cookie(
    authed_client: TestClient, csrf: Callable[[str], str]
) -> None:
    stolen = authed_client.cookies[SESSION_COOKIE]
    form_token = csrf(authed_client.get("/settings", headers=HTML).text)
    response = authed_client.post(
        "/settings/sessions/revoke", data={"csrf_token": form_token}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    authed_client.cookies.clear()
    authed_client.cookies.set(SESSION_COOKIE, stolen)
    assert authed_client.get("/settings", headers=HTML, follow_redirects=False).status_code == 303
    assert authed_client.get("/").status_code == 401


def test_settings_page_offers_log_out_everywhere(authed_client: TestClient) -> None:
    assert "Log out everywhere" in authed_client.get("/settings", headers=HTML).text


# --- Bearer --------------------------------------------------------------------


def test_bearer_token_works(client: TestClient, auth_token: str) -> None:
    response = client.get("/", headers={**HTML, "Authorization": f"Bearer {auth_token}"})

    assert response.status_code == 200
    assert "Log out" not in response.text  # nothing to log out of
    assert not _set_cookie_headers(client.get("/healthz"))


@pytest.mark.parametrize(
    "authorization", ["Bearer wrong-token", "Bearer ", "Basic abc", "Token {token}"]
)
def test_bad_authorization_is_rejected(
    client: TestClient, authorization: str, auth_token: str
) -> None:
    assert (
        client.get(
            "/", headers={"Authorization": authorization.format(token=auth_token)}
        ).status_code
        == 401
    )


def test_bearer_scheme_is_case_insensitive(client: TestClient, auth_token: str) -> None:
    assert client.get("/", headers={"Authorization": f"bearer {auth_token}"}).status_code == 200


def test_wrong_bearer_is_not_rescued_by_session(authed_client: TestClient) -> None:
    response = authed_client.get("/", headers={"Authorization": "Bearer wrong"})

    assert response.status_code == 401


def test_wrong_bearer_tokens_are_rate_limited(
    client: TestClient, auth_token: str, login: LoginFn
) -> None:
    for _ in range(5):
        assert (
            client.get("/api/v1/properties", headers={"Authorization": "Bearer x"}).status_code
            == 401
        )

    blocked = client.get("/api/v1/properties", headers={"Authorization": f"Bearer {auth_token}"})

    # Locked out: even the right token waits, so the lockout can't be guessed past.
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert client.get("/", headers={"Authorization": f"Bearer {auth_token}"}).status_code == 429
    # One budget per client: the login form is locked out too.
    assert login(client).status_code == 429


def test_valid_bearer_does_not_count_against_the_limit(client: TestClient, auth_token: str) -> None:
    for _ in range(10):
        response = client.get(
            "/api/v1/properties", headers={"Authorization": f"Bearer {auth_token}"}
        )
        assert response.status_code == 200


# --- CSRF ----------------------------------------------------------------------


@pytest.fixture
def post_client(migrated_engine: Engine, login: LoginFn) -> Iterator[TestClient]:
    """An authed client on an app with an extra protected POST route."""
    from fastapi import APIRouter, Depends

    app = create_app(Settings(), engine=migrated_engine)
    router = APIRouter(dependencies=[Depends(require_auth), Depends(require_csrf)])

    @router.post("/action")
    def action() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    with TestClient(app) as client:
        login(client)
        yield client


def test_missing_csrf_is_rejected(post_client: TestClient) -> None:
    response = post_client.post("/action")

    assert response.status_code == 403
    assert response.json() == {"detail": "CSRF token missing or invalid"}


def test_wrong_csrf_is_rejected(post_client: TestClient) -> None:
    assert post_client.post("/action", headers={"X-CSRF-Token": "nope"}).status_code == 403
    assert post_client.post("/action", data={"csrf_token": "nope"}).status_code == 403


def test_csrf_header_from_htmx_is_accepted(post_client: TestClient) -> None:
    page = post_client.get("/", headers=HTML).text
    [token] = re.findall(r'hx-headers=\'\{"X-CSRF-Token": "([^"]+)"\}\'', page)

    response = post_client.post("/action", headers={"X-CSRF-Token": token})

    assert response.status_code == 200


def test_csrf_form_field_is_accepted(post_client: TestClient, csrf: Callable[[str], str]) -> None:
    token = csrf(post_client.get("/", headers=HTML).text)

    assert post_client.post("/action", data={"csrf_token": token}).status_code == 200


def test_csrf_token_from_another_session_is_rejected(
    post_client: TestClient, csrf: Callable[[str], str], login: LoginFn
) -> None:
    token = csrf(post_client.get("/", headers=HTML).text)
    post_client.cookies.clear()
    login(post_client)

    assert post_client.post("/action", data={"csrf_token": token}).status_code == 403


def test_bearer_requests_skip_csrf(post_client: TestClient, auth_token: str) -> None:
    post_client.cookies.clear()

    assert (
        post_client.post("/action", headers={"Authorization": f"Bearer {auth_token}"}).status_code
        == 200
    )
    assert post_client.post("/action", headers={"Authorization": "Bearer x"}).status_code == 401


def test_login_without_csrf_is_rejected(client: TestClient, auth_token: str) -> None:
    client.get("/login")
    response = client.post("/login", data={"token": auth_token}, follow_redirects=False)

    assert response.status_code == 403
    assert SESSION_COOKIE not in client.cookies


def test_logout_without_csrf_is_rejected(authed_client: TestClient) -> None:
    assert authed_client.post("/logout").status_code == 403
    assert authed_client.get("/").status_code == 200


def test_wrong_bearer_does_not_skip_csrf(client: TestClient, auth_token: str) -> None:
    client.get("/login")
    response = client.post(
        "/login",
        data={"token": auth_token},
        headers={"Authorization": "Bearer wrong"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert SESSION_COOKIE not in client.cookies


# --- Rate limiting -------------------------------------------------------------


def test_failed_logins_are_rate_limited(client: TestClient, login: LoginFn) -> None:
    for _ in range(5):
        assert login(client, "wrong-token").status_code == 401

    blocked = login(client, "wrong-token")
    assert blocked.status_code == 429
    assert 1 <= int(blocked.headers["retry-after"]) <= 300
    assert "Too many failed attempts" in blocked.text

    # Even the right token is refused while locked out.
    assert login(client).status_code == 429
    assert SESSION_COOKIE not in client.cookies


def test_successful_login_resets_failures(client: TestClient, login: LoginFn) -> None:
    for _ in range(4):
        login(client, "wrong-token")
    assert login(client).status_code == 303

    client.cookies.delete(SESSION_COOKIE)
    for _ in range(4):
        assert login(client, "wrong-token").status_code == 401


def _proxied_app(engine: Engine, monkeypatch: pytest.MonkeyPatch, trusted: str) -> TestClient:
    monkeypatch.setenv("WEB_TRUSTED_PROXIES", trusted)
    monkeypatch.setenv("AUTH_LOGIN_MAX_ATTEMPTS", "1")
    # TestClient connects from the literal host "testclient".
    return TestClient(create_app(Settings(), engine=engine))


def _login_from(client: TestClient, forwarded_for: str) -> int:
    [token] = re.findall(r'name="csrf_token" value="([^"]+)"', client.get("/login").text)
    return client.post(
        "/login",
        data={"token": "wrong-token", "csrf_token": token},
        headers={"X-Forwarded-For": forwarded_for},
        follow_redirects=False,
    ).status_code


def test_trusted_proxy_forwarded_for_is_the_rate_limit_key(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _proxied_app(migrated_engine, monkeypatch, "testclient, 10.0.0.0/8")

    assert _login_from(client, "203.0.113.7") == 401
    assert _login_from(client, "203.0.113.7") == 429
    # Another client behind the same proxy is unaffected; the proxy hop is skipped.
    assert _login_from(client, "198.51.100.9, 10.0.0.2") == 401


def test_forwarded_for_is_ignored_without_trusted_proxies(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _proxied_app(migrated_engine, monkeypatch, "")

    assert _login_from(client, "203.0.113.7") == 401
    # A spoofed header does not buy a fresh budget: the socket peer is the key.
    assert _login_from(client, "198.51.100.9") == 429


def test_forwarded_proto_https_marks_cookies_secure(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _proxied_app(migrated_engine, monkeypatch, "testclient")

    response = client.get("/login", headers={"X-Forwarded-Proto": "https"})

    assert "Secure" in _cookie_header(response, HOST_PREFIX + CSRF_COOKIE)


# --- Logging -------------------------------------------------------------------


def test_login_attempts_are_logged_without_the_token(
    client: TestClient, capsys: pytest.CaptureFixture[str], login: LoginFn, auth_token: str
) -> None:
    # No secrets handed to the redactor: the token must simply never be logged.
    configure_logging(LoggingSettings())
    login(client, "wrong-but-long-token-value")
    login(client)

    err = capsys.readouterr().err
    assert '"event": "login_failed"' in err
    assert '"event": "login_succeeded"' in err
    assert '"client_ip": "testclient"' in err
    assert auth_token not in err
    assert "wrong-but-long-token-value" not in err
