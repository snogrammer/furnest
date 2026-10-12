"""``GET /photo``: listing photos served from furnest's own origin (#212)."""

from collections.abc import Callable
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.base import utcnow
from furnest.db.listing_store import SqlListingStore
from furnest.dedup.phash import FetchedImage, ImageFetcher, ImageFetchError
from furnest.domain import NormalizedListing
from furnest.web.routes.photos import _fetcher

pytestmark = pytest.mark.integration

PHOTO = "https://img.example.com/a.jpg"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


class FakeFetcher:
    def __init__(self, reply: FetchedImage | ImageFetchError) -> None:
        self.reply = reply
        self.fetched: list[str] = []

    def fetch_image(self, url: str) -> FetchedImage:
        self.fetched.append(url)
        if isinstance(self.reply, ImageFetchError):
            raise self.reply
        return self.reply


@pytest.fixture(autouse=True)
def stored_photo(session_factory: sessionmaker[Session]) -> None:
    listing = NormalizedListing.model_validate(
        {
            "source": "test",
            "source_id": "l1",
            "url": "https://example.com/l1",
            "title": "Furnished place",
            "images": [PHOTO],
        }
    )
    SqlListingStore(session_factory).upsert_batch("test", [listing], seen_at=utcnow())


Install = Callable[[FetchedImage | ImageFetchError], FakeFetcher]


@pytest.fixture
def install(authed_client: TestClient) -> Install:
    def put(reply: FetchedImage | ImageFetchError) -> FakeFetcher:
        fetcher = FakeFetcher(reply)
        authed_client.app.state.photo_fetcher = fetcher  # type: ignore[attr-defined]
        return fetcher

    return put


def test_requires_login(client: TestClient) -> None:
    response = client.get("/photo", params={"src": PHOTO}, follow_redirects=False)

    assert response.status_code in (303, 401)


def test_serves_a_stored_photo_from_this_origin(
    authed_client: TestClient, install: Install
) -> None:
    fetcher = install(FetchedImage(JPEG, "image/jpeg"))

    response = authed_client.get("/photo", params={"src": PHOTO})

    assert response.status_code == 200
    assert response.content == JPEG
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, max-age=86400"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert fetcher.fetched == [PHOTO]


def test_a_url_no_listing_has_is_not_fetched(authed_client: TestClient, install: Install) -> None:
    fetcher = install(FetchedImage(JPEG, "image/jpeg"))

    response = authed_client.get("/photo", params={"src": "http://169.254.169.254/latest"})

    assert response.status_code == 404
    assert fetcher.fetched == []


@pytest.mark.parametrize("content_type", ["image/svg+xml", "image/x-icon"])
def test_non_raster_images_are_not_passed_on(
    authed_client: TestClient, install: Install, content_type: str
) -> None:
    install(FetchedImage(b"<svg onload='alert(1)'/>", content_type))

    response = authed_client.get("/photo", params={"src": PHOTO})

    assert response.status_code == 502
    assert response.content == b""


@pytest.mark.parametrize("reason", ["HTTP 404", "timeout", "robots.txt", "blocked address"])
def test_a_failed_fetch_is_a_bad_gateway(
    authed_client: TestClient, install: Install, reason: str
) -> None:
    install(ImageFetchError(reason, url=PHOTO))

    response = authed_client.get("/photo", params={"src": PHOTO})

    assert response.status_code == 502


def test_off_when_the_proxy_is_disabled(authed_client: TestClient, install: Install) -> None:
    fetcher = install(FetchedImage(JPEG, "image/jpeg"))
    state = authed_client.app.state  # type: ignore[attr-defined]
    images = state.settings.images.model_copy(update={"proxy_enabled": False})
    state.settings = state.settings.model_copy(update={"images": images})

    response = authed_client.get("/photo", params={"src": PHOTO})

    assert response.status_code == 404
    assert fetcher.fetched == []


def test_one_fetcher_is_built_and_shared(client: TestClient) -> None:
    request = SimpleNamespace(app=client.app)

    fetcher = _fetcher(request)  # type: ignore[arg-type]

    assert isinstance(fetcher, ImageFetcher)
    assert _fetcher(request) is fetcher  # type: ignore[arg-type]
    assert client.app.state.photo_fetcher is fetcher  # type: ignore[attr-defined]
