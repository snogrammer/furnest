"""Reverse image enrichment: photos searched, pages stored on the property."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import ReverseImageSettings
from furnest.db.listing_store import SqlListingStore
from furnest.db.models import EnrichmentResult
from furnest.db.models.enrichment import REVERSE_IMAGE
from furnest.db.property_store import SqlPropertyStore
from furnest.db.session import session_scope
from furnest.domain import NormalizedListing
from furnest.enrichment.reverse_image import (
    NullReverseImageSearch,
    ReverseImageEnrichment,
    ReverseImageError,
    ReverseImageMatch,
    ReverseImageSearch,
)

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
PHOTOS = [f"https://img.example.com/{n}.jpg" for n in range(5)]


def match(url: str, title: str = "") -> ReverseImageMatch:
    return ReverseImageMatch(page_url=HttpUrl(url), title=title, provider="fake")


class FakeSearch:
    def __init__(self, answers: dict[str, list[ReverseImageMatch] | ReverseImageError]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def search(self, image_url: str) -> list[ReverseImageMatch]:
        self.asked.append(image_url)
        answer = self.answers.get(image_url, [])
        if isinstance(answer, ReverseImageError):
            raise answer
        return answer


@pytest.fixture
def property_id(session_factory: sessionmaker[Session]) -> int:
    own = NormalizedListing.model_validate(
        {
            "source": "test",
            "source_id": "own",
            "url": "https://test.example.com/own",
            "title": "Furnished 1BR",
            "address": {"line1": "1 Main St", "city": "Springfield", "region": "IL"},
            "images": PHOTOS,
        }
    )
    (listing_id,) = SqlListingStore(session_factory).upsert_batch("test", [own], seen_at=T0).new_ids
    return SqlPropertyStore(session_factory).ensure_for_listing(listing_id, at=T0).id


def run(
    session_factory: sessionmaker[Session],
    search: ReverseImageSearch,
    property_id: int,
    **fields: Any,  # noqa: ANN401
) -> int:
    settings = ReverseImageSettings(**fields)
    return ReverseImageEnrichment(session_factory, search, settings).run(property_id)


def stored(session_factory: sessionmaker[Session]) -> list[EnrichmentResult]:
    with session_scope(session_factory) as session:
        rows = list(session.scalars(select(EnrichmentResult).order_by(EnrichmentResult.url)))
        session.expunge_all()
        return rows


def test_stores_pages_for_the_first_photos_and_skips_own_host(
    session_factory: sessionmaker[Session], property_id: int
) -> None:
    search = FakeSearch(
        {
            PHOTOS[0]: [
                match("https://other.example.com/a", "Copy"),
                match("https://www.test.example.com/own-copy"),
            ],
            PHOTOS[1]: [match("https://other.example.com/a"), match("https://third.example.org/b")],
        }
    )
    assert run(session_factory, search, property_id, max_photos=2) == 2
    assert search.asked == PHOTOS[:2]
    rows = stored(session_factory)
    assert [(r.kind, r.url, r.title, r.source, r.data) for r in rows] == [
        (REVERSE_IMAGE, "https://other.example.com/a", "Copy", "fake", {"photo": PHOTOS[0]}),
        (REVERSE_IMAGE, "https://third.example.org/b", "", "fake", {"photo": PHOTOS[1]}),
    ]


def test_a_rerun_updates_in_place_and_drops_what_is_gone(
    session_factory: sessionmaker[Session], property_id: int
) -> None:
    pages = [match("https://a.example.com/1"), match("https://b.example.com/2")]
    first = FakeSearch({PHOTOS[0]: pages})
    run(session_factory, first, property_id, max_photos=1)
    ids = {r.url: r.id for r in stored(session_factory)}
    second = FakeSearch({PHOTOS[0]: [match("https://b.example.com/2", "Now titled")]})
    run(session_factory, second, property_id, max_photos=1)
    rows = stored(session_factory)
    assert [(r.url, r.title) for r in rows] == [("https://b.example.com/2", "Now titled")]
    assert rows[0].id == ids["https://b.example.com/2"]


def test_all_searches_failing_keeps_earlier_results(
    session_factory: sessionmaker[Session], property_id: int
) -> None:
    earlier = FakeSearch({PHOTOS[0]: [match("https://a.example.com/1")]})
    run(session_factory, earlier, property_id, max_photos=1)
    failing = FakeSearch({PHOTOS[0]: ReverseImageError("timeout")})
    assert run(session_factory, failing, property_id, max_photos=1) == 0
    assert [r.url for r in stored(session_factory)] == ["https://a.example.com/1"]


def test_one_failing_photo_does_not_lose_the_others(
    session_factory: sessionmaker[Session], property_id: int
) -> None:
    search = FakeSearch(
        {PHOTOS[0]: ReverseImageError("HTTP 500"), PHOTOS[1]: [match("https://a.example.com/1")]}
    )
    assert run(session_factory, search, property_id, max_photos=2) == 1


def test_unconfigured_search_stores_nothing(
    session_factory: sessionmaker[Session], property_id: int
) -> None:
    assert run(session_factory, NullReverseImageSearch(), property_id) == 0
    assert stored(session_factory) == []
