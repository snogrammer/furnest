"""SqlAmenityCache on a migrated database, and the LLM extractor against a real
loopback HTTP server standing in for an OpenAI-compatible endpoint."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from furnest.config import LlmSettings
from furnest.db.amenity_cache import SqlAmenityCache
from furnest.db.models import AmenityExtraction
from furnest.db.session import session_scope
from furnest.domain import Amenity, AmenityResult, NormalizedListing
from furnest.matching.amenities import LlmAmenityExtractor, build_amenity_extractor

pytestmark = pytest.mark.integration

RESULT = AmenityResult(
    present=frozenset({Amenity.GYM, Amenity.AC}), absent=frozenset({Amenity.POOL})
)


@pytest.fixture
def cache(session_factory: sessionmaker[Session]) -> SqlAmenityCache:
    return SqlAmenityCache(session_factory)


def test_miss_then_round_trip(
    cache: SqlAmenityCache, session_factory: sessionmaker[Session]
) -> None:
    assert cache.get("a" * 64) is None

    cache.put("a" * 64, "test-model", RESULT)

    assert cache.get("a" * 64) == RESULT
    with session_scope(session_factory) as session:
        row = session.get(AmenityExtraction, "a" * 64)
        assert row is not None
        assert (row.model, row.present, row.absent) == ("test-model", ["ac", "gym"], ["pool"])
        assert row.created_at.tzinfo is not None


def test_duplicate_put_keeps_the_first_row(
    cache: SqlAmenityCache, session_factory: sessionmaker[Session]
) -> None:
    cache.put("b" * 64, "test-model", RESULT)
    cache.put("b" * 64, "test-model", AmenityResult())

    assert cache.get("b" * 64) == RESULT
    with session_scope(session_factory) as session:
        assert session.scalar(select(func.count()).select_from(AmenityExtraction)) == 1


# --- Loopback fake server --------------------------------------------------------


class _FakeLlm(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []  # noqa: RUF012 - shared per test via fixture

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"path": self.path, "body": body})
        content = json.dumps({"present": ["gym"], "absent": []})
        payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def fake_llm() -> Iterator[str]:
    _FakeLlm.requests = []
    server = HTTPServer(("127.0.0.1", 0), _FakeLlm)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_extractor_over_http_with_sql_cache(fake_llm: str, cache: SqlAmenityCache) -> None:
    settings = LlmSettings.model_validate(
        {"provider": "openai_compatible", "base_url": fake_llm, "model": "test-model"}
    )
    extractor = build_amenity_extractor(settings, cache)
    assert isinstance(extractor, LlmAmenityExtractor)
    listing = NormalizedListing.model_validate(
        {
            "source": "test",
            "source_id": "1",
            "url": "https://example.com/1",
            "title": "Furnished 1BR",
            "description": "Dishwasher. Fitness room downstairs.",
        }
    )

    first = extractor.extract(listing)
    second = extractor.extract(listing)

    assert first == second
    assert first.present == {Amenity.DISHWASHER, Amenity.GYM}
    assert len(_FakeLlm.requests) == 1
    assert _FakeLlm.requests[0]["path"] == "/v1/chat/completions"
