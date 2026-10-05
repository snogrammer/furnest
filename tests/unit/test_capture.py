"""Fixture capture: fetch as the adapter would, sanitize, write; and the script around it.

Pages are served by ``httpx2.MockTransport``, adapters are fakes in their own
registry, and ``environ`` is passed explicitly because CI itself sets ``CI``.
"""

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

import httpx2
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from furnest.config import AdapterOverrides, SourcesSettings
from furnest.domain import Criteria, NormalizedListing
from furnest.sources import SourceBlockedError, SourceRegistry
from furnest.sources.base import Capabilities, SourceAdapter
from furnest.sources.capture import CaptureError, CaptureResult, capture_fixture, default_name
from furnest.sources.sanitize import EMAIL_PLACEHOLDER, NAME_PLACEHOLDER

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "capture_fixture.py"
PAGE_URL = "https://listings.example.com/rentals/denver/4821?ref=x"
APIFY_URL = "https://api.apify.com/v2/datasets/abc123/items?clean=true&format=json"
TOKEN = "apify_api_test_token"  # noqa: S105 - a placeholder, not a credential


class PageSource(SourceAdapter):
    name: ClassVar[str] = "page_source"
    display_name: ClassVar[str] = "Page source"

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        return []


class ApiSource(SourceAdapter):
    name: ClassVar[str] = "api_source"
    display_name: ClassVar[str] = "API source"
    capabilities: ClassVar[Capabilities] = Capabilities(has_api=True, requires_api_key=True)

    def search(self, criteria: Criteria) -> list[NormalizedListing]:
        return []


def registry() -> SourceRegistry:
    reg = SourceRegistry(browser_check=lambda _settings: None)
    reg.register(PageSource)
    reg.register(ApiSource)
    return reg


def settings(api_key: str | None = TOKEN) -> SourcesSettings:
    key = SecretStr(api_key) if api_key is not None else None
    return SourcesSettings(
        respect_robots=False,
        requests_per_minute=600,
        adapters={"api_source": AdapterOverrides(api_key=key)},
    )


class Server:
    """Answers every request with ``response`` and remembers the requests."""

    def __init__(self, response: httpx2.Response) -> None:
        self.response = response
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.response


def html(body: str, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


@pytest.fixture
def capture(tmp_path: Path) -> Callable[..., CaptureResult]:
    def run(server: Server, adapter: str = "page_source", **kwargs: Any) -> CaptureResult:  # noqa: ANN401
        url = kwargs.pop("url", PAGE_URL)
        options: dict[str, Any] = {
            "settings": settings(),
            "out_dir": tmp_path / "fixtures",
            "registry": registry(),
            "transport": httpx2.MockTransport(server),
            "environ": {},
        }
        options.update(kwargs)
        return capture_fixture(adapter, url, **options)

    return run


# --- Capturing ----------------------------------------------------------------------


def test_writes_a_sanitized_page(capture: Callable[..., CaptureResult], tmp_path: Path) -> None:
    server = Server(html("<p>Ask Rosalind at rq@mail.io</p>"))

    result = capture(server, redact=["Rosalind"])

    assert result.path == tmp_path / "fixtures" / "page_source" / "rentals-denver-4821.html"
    assert result.path.read_text(encoding="utf-8") == (
        f"<p>Ask {NAME_PLACEHOLDER} at {EMAIL_PLACEHOLDER}</p>"
    )
    assert result.counts == {"email": 1, "name": 1}
    assert result.content_type == "text/html; charset=utf-8"
    (request,) = server.requests
    assert str(request.url) == PAGE_URL
    assert request.headers["user-agent"] == SourcesSettings().user_agent
    assert "authorization" not in request.headers


def test_apify_urls_carry_the_adapter_token_in_a_header(
    capture: Callable[..., CaptureResult],
) -> None:
    items = [{"id": "1", "landlord": {"name": "Rosalind Quill"}}]
    server = Server(httpx2.Response(200, json=items))

    result = capture(server, "api_source", url=APIFY_URL, name="dataset_items.json")

    (request,) = server.requests
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(request.url)
    assert result.path.name == "dataset_items.json"
    assert json.loads(result.path.read_text(encoding="utf-8")) == [
        {"id": "1", "landlord": {"name": NAME_PLACEHOLDER}}
    ]


def test_apify_robots_txt_is_not_consulted(capture: Callable[..., CaptureResult]) -> None:
    # With robots on, the first request would be robots.txt; the API call isn't crawling.
    server = Server(httpx2.Response(200, json=[]))
    robots_on = settings().model_copy(update={"respect_robots": True})

    capture(server, "api_source", url=APIFY_URL, settings=robots_on)

    assert [r.url.path for r in server.requests] == ["/v2/datasets/abc123/items"]


def test_apify_needs_the_adapter_api_key(capture: Callable[..., CaptureResult]) -> None:
    server = Server(httpx2.Response(200, json=[]))

    with pytest.raises(CaptureError, match='set "api_key"'):
        capture(server, "api_source", url=APIFY_URL, settings=settings(api_key=None))
    assert server.requests == []


def test_a_token_in_the_url_is_refused(capture: Callable[..., CaptureResult]) -> None:
    server = Server(httpx2.Response(200, json=[]))

    with pytest.raises(CaptureError, match='remove "token"'):
        capture(server, "api_source", url=f"{APIFY_URL}&token=secret")
    assert server.requests == []


@pytest.mark.parametrize(
    "response",
    [html("Forbidden", status=403), html('<div class="g-recaptcha"></div>')],
    ids=["403", "captcha"],
)
def test_a_blocked_page_writes_nothing(
    capture: Callable[..., CaptureResult], tmp_path: Path, response: httpx2.Response
) -> None:
    with pytest.raises(SourceBlockedError):
        capture(Server(response))

    assert not (tmp_path / "fixtures").exists()


def test_an_existing_fixture_needs_force(
    capture: Callable[..., CaptureResult], tmp_path: Path
) -> None:
    target = tmp_path / "fixtures" / "page_source" / "page.html"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")

    with pytest.raises(CaptureError, match="--force"):
        capture(Server(html("new")), name="page.html")
    with pytest.raises(CaptureError, match="--force"):
        capture(Server(html("new")), url="https://listings.example.com/page")
    assert target.read_text(encoding="utf-8") == "old"

    capture(Server(html("new")), name="page.html", force=True)
    assert target.read_text(encoding="utf-8") == "new"


def test_a_named_fixture_is_checked_before_fetching(
    capture: Callable[..., CaptureResult], tmp_path: Path
) -> None:
    target = tmp_path / "fixtures" / "page_source" / "page.html"
    target.parent.mkdir(parents=True)
    target.touch()
    server = Server(html("new"))

    with pytest.raises(CaptureError):
        capture(server, name="page.html")
    assert server.requests == []


# --- Refusals -----------------------------------------------------------------------


def test_refuses_to_run_in_ci(capture: Callable[..., CaptureResult]) -> None:
    server = Server(html("x"))

    with pytest.raises(CaptureError, match="manual use only"):
        capture(server, environ={"CI": "true"})
    assert server.requests == []


def test_unknown_adapters_list_the_known_ones(capture: Callable[..., CaptureResult]) -> None:
    with pytest.raises(CaptureError, match="known: api_source, page_source"):
        capture(Server(html("x")), "nope")


@pytest.mark.parametrize("url", ["ftp://example.com/x", "listings.example.com/x", "https:///x"])
def test_only_http_urls(capture: Callable[..., CaptureResult], url: str) -> None:
    with pytest.raises(CaptureError, match="not an http"):
        capture(Server(html("x")), url=url)


@pytest.mark.parametrize("name", ["", "../escape.html", "sub/page.html", ".."])
def test_names_are_plain_file_names(capture: Callable[..., CaptureResult], name: str) -> None:
    with pytest.raises(CaptureError, match="plain file name"):
        capture(Server(html("x")), name=name)


def test_the_real_registry_is_used_by_default(tmp_path: Path) -> None:
    with pytest.raises(CaptureError, match="furnished_finder"):
        capture_fixture("nope", PAGE_URL, settings=SourcesSettings(), out_dir=tmp_path, environ={})


# --- File names ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "content_type", "expected"),
    [
        (APIFY_URL, "application/json", "v2-datasets-abc123-items.json"),
        ("https://example.com/search.html?q=1", "text/html", "search.html"),
        ("https://example.com/", "text/html", "index.html"),
        ("https://example.com", "", "index.txt"),
        ("https://example.com/Rooms/Big%20Studio", "text/plain", "rooms-big-studio.txt"),
        (f"https://example.com/{'a' * 80}", "text/html", f"{'a' * 60}.html"),
    ],
)
def test_default_names(url: str, content_type: str, expected: str) -> None:
    assert default_name(url, content_type) == expected


# --- The script ---------------------------------------------------------------------


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("capture_fixture_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_reports_what_it_wrote(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = load_script()
    calls: list[dict[str, Any]] = []

    def fake_capture(adapter: str, url: str, **kwargs: Any) -> CaptureResult:  # noqa: ANN401
        calls.append({"adapter": adapter, "url": url, **kwargs})
        return CaptureResult(tmp_path / "x.html", "text/html", {"email": 2, "name": 1})

    monkeypatch.setattr(script, "capture_fixture", fake_capture)

    result = CliRunner().invoke(
        script.app,
        ["page_source", PAGE_URL, "--redact", "Rosalind", "--redact", "Quill", "--force"],
    )

    assert result.exit_code == 0, result.output
    assert "replaced: 2 email, 1 name" in result.output
    assert "--redact" in result.output
    (call,) = calls
    assert call["redact"] == ["Rosalind", "Quill"]
    assert call["force"] is True
    assert call["name"] is None
    assert call["out_dir"] == Path("tests/fixtures")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CaptureError("unknown adapter 'x'"), "error: unknown adapter"),
        (
            SourceBlockedError("blocked by u: HTTP 403", url="u", reason="HTTP 403"),
            "blocked (HTTP 403)",
        ),
    ],
)
def test_script_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str
) -> None:
    script = load_script()

    def fail(*_args: object, **_kwargs: object) -> CaptureResult:
        raise error

    monkeypatch.setattr(script, "capture_fixture", fail)

    result = CliRunner().invoke(script.app, ["page_source", PAGE_URL])

    assert result.exit_code == 1
    assert expected in result.output
