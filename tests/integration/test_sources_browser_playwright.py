"""SourceBrowser against real headless Chromium. Skipped unless the ``browser`` extra
and a Chromium build (``playwright install chromium``) are installed.

pytest-socket only guards this process, not Chromium, so every request the page
makes is answered by a route: nothing reaches the network.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from furnest.sources import AdapterConfig, BrowserUnavailableError, SourceBrowser

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.integration

PAGE = """<!doctype html>
<html><head><title>Listings</title></head>
<body>
  <img src="https://cdn.example.com/photo.jpg">
  <div id="out"></div>
  <script>
    document.getElementById("out").textContent = "rendered by JS: " + navigator.userAgent;
  </script>
</body></html>"""


def _config() -> AdapterConfig:
    return AdapterConfig(
        name="fake",
        enabled=True,
        enabled_pinned=False,
        interval_minutes=60,
        jitter_seconds=0,
        requests_per_minute=600.0,
        user_agent="furnest-test/1",
        request_timeout_seconds=10,
        respect_robots=False,
    )


@pytest.fixture
def browser() -> Iterator[SourceBrowser]:
    try:
        with SourceBrowser(_config()) as browser:
            yield browser
    except BrowserUnavailableError as exc:
        pytest.skip(f"Chromium unavailable: {exc}")


def test_context_runs_js_blocks_images_and_sets_user_agent(browser: SourceBrowser) -> None:
    seen: list[tuple[str, str]] = []
    failed: list[str] = []

    def serve(route: Any) -> None:  # noqa: ANN401
        request = route.request
        seen.append((request.resource_type, request.url))
        if request.resource_type == "document":
            route.fulfill(status=200, content_type="text/html", body=PAGE)
        else:
            # Fall through to the browser layer's blocker, registered before us.
            route.fallback()

    with browser.new_context() as context:
        context.route("**/*", serve)
        page = context.new_page()
        page.on("requestfailed", lambda request: failed.append(request.url))
        page.goto("https://listings.example.com/search", wait_until="load")
        text = page.text_content("#out")

    assert text == "rendered by JS: furnest-test/1"
    assert ("image", "https://cdn.example.com/photo.jpg") in seen
    assert failed == ["https://cdn.example.com/photo.jpg"]
