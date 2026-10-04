"""LlmAmenityExtractor: request shape, merging, fallback, caching and the factory.

The endpoint is faked in-process with ``httpx2.MockTransport``, so no socket opens.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from furnest.config import LlmProvider, LlmSettings, LogFormat, LoggingSettings, LogLevel
from furnest.domain import Amenity, AmenityResult, NormalizedListing
from furnest.logging import configure_logging
from furnest.matching.amenities import (
    InMemoryAmenityCache,
    KeywordAmenityExtractor,
    LlmAmenityExtractor,
    OpenAICompatibleClient,
    build_amenity_extractor,
)
from furnest.matching.amenities.llm import RESPONSE_SCHEMA, content_hash, merge

pytestmark = pytest.mark.unit

A = Amenity
BASE_URL = "https://llm.example.com/v1"
MODEL = "test-model"
Handler = Callable[[httpx2.Request], httpx2.Response]


def listing(
    description: str = "", *, title: str = "Furnished 1BR", **fields: object
) -> NormalizedListing:
    return NormalizedListing.model_validate(
        {
            "source": "test",
            "source_id": "1",
            "url": "https://example.com/1",
            "title": title,
            "description": description,
            **fields,
        }
    )


def chat_reply(content: str, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, json={"choices": [{"message": {"content": content}}]})


def verdicts(present: list[str] = [], absent: list[str] = []) -> str:  # noqa: B006 - read-only
    return json.dumps({"present": present, "absent": absent})


class Recorder:
    """A MockTransport handler that returns canned responses and records requests."""

    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def body(self, index: int = 0) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.requests[index].content)
        return loaded


def make_extractor(
    handler: Handler,
    *,
    cache: InMemoryAmenityCache | None = None,
    api_key: str | None = None,
) -> LlmAmenityExtractor:
    client = OpenAICompatibleClient(
        BASE_URL, MODEL, api_key=api_key, timeout=5, transport=httpx2.MockTransport(handler)
    )
    return LlmAmenityExtractor(client, cache if cache is not None else InMemoryAmenityCache())


# --- Request shape --------------------------------------------------------------


def test_request_targets_chat_completions_with_strict_schema() -> None:
    recorder = Recorder(chat_reply(verdicts()))
    make_extractor(recorder).extract(
        listing("Quiet unit.", title="Cozy loft", amenities=frozenset({"Bike room"}))
    )

    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/chat/completions"
    assert "authorization" not in request.headers
    body = recorder.body()
    assert body["model"] == MODEL
    assert body["temperature"] == 0
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "amenities", "strict": True, "schema": RESPONSE_SCHEMA},
    }
    system, user = body["messages"]
    assert system["role"] == "system"
    assert all(tag.value in system["content"] for tag in Amenity)
    assert user["role"] == "user"
    assert "Cozy loft" in user["content"]
    assert "bike room" in user["content"]
    assert "Quiet unit." in user["content"]


def test_api_key_is_sent_as_bearer_token() -> None:
    recorder = Recorder(chat_reply(verdicts()))
    make_extractor(recorder, api_key="sk-test").extract(listing())

    assert recorder.requests[0].headers["authorization"] == "Bearer sk-test"


def test_long_descriptions_are_truncated() -> None:
    recorder = Recorder(chat_reply(verdicts()))
    make_extractor(recorder).extract(listing("x" * 20_000))

    assert len(recorder.body()["messages"][1]["content"]) < 9000


def test_timeout_is_applied_to_requests() -> None:
    seen: list[object] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.extensions["timeout"])
        return chat_reply(verdicts())

    make_extractor(handler).extract(listing())

    assert seen == [{"connect": 5, "read": 5, "write": 5, "pool": 5}]


# --- Merging ----------------------------------------------------------------------


def test_llm_fills_tags_the_keywords_missed() -> None:
    recorder = Recorder(chat_reply(verdicts(present=["gym", "dogs_allowed"], absent=["pool"])))
    result = make_extractor(recorder).extract(
        listing("Dishwasher. Bring your golden retriever; fitness room downstairs.")
    )

    assert result.present == {A.DISHWASHER, A.GYM, A.DOGS_ALLOWED, A.PETS_ALLOWED}
    assert result.absent == {A.POOL}


def test_keyword_verdicts_win_conflicts() -> None:
    recorder = Recorder(chat_reply(verdicts(present=["pets_allowed"], absent=["dishwasher"])))
    result = make_extractor(recorder).extract(listing("No pets. Dishwasher included."))

    assert result.status(A.PETS_ALLOWED) is False
    assert result.status(A.DISHWASHER) is True


def test_unrecognized_labels_come_from_the_keyword_pass() -> None:
    recorder = Recorder(chat_reply(verdicts()))
    result = make_extractor(recorder).extract(listing(amenities=frozenset({"Rooftop garden"})))

    assert result.unrecognized == {"rooftop garden"}


def test_merge_reconciles_pets_after_filling() -> None:
    base = AmenityResult(absent=frozenset({A.PETS_ALLOWED, A.CATS_ALLOWED, A.DOGS_ALLOWED}))
    merged = merge(base, AmenityResult(present=frozenset({A.CATS_ALLOWED, A.AC})))

    assert merged.status(A.CATS_ALLOWED) is False
    assert merged.status(A.AC) is True

    merged = merge(AmenityResult(), AmenityResult(present=frozenset({A.CATS_ALLOWED})))
    assert merged.present == {A.CATS_ALLOWED, A.PETS_ALLOWED}


# --- Fallback ---------------------------------------------------------------------

KEYWORD_ONLY = KeywordAmenityExtractor().extract(listing("Central A/C, no smoking."))


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(chat_reply(verdicts(), status=500), id="server-error"),
        pytest.param(httpx2.ReadTimeout("slow"), id="timeout"),
        pytest.param(httpx2.ConnectError("refused"), id="connect-error"),
        pytest.param(httpx2.Response(200, text="not json"), id="non-json-body"),
        pytest.param(httpx2.Response(200, json={"choices": []}), id="no-choices"),
        pytest.param(chat_reply("Sure! The listing has AC."), id="non-json-content"),
        pytest.param(chat_reply(verdicts(present=["hot_tub"])), id="unknown-tag"),
        pytest.param(chat_reply(verdicts(present=["ac"], absent=["ac"])), id="overlap"),
        pytest.param(chat_reply('{"present": [], "absent": [], "notes": "x"}'), id="extra-key"),
        pytest.param(chat_reply('{"present": []}'), id="missing-key"),
    ],
)
def test_any_failure_falls_back_to_keyword_result(response: httpx2.Response | Exception) -> None:
    cache = InMemoryAmenityCache()
    recorder = Recorder(response, chat_reply(verdicts(present=["gym"])))
    extractor = make_extractor(recorder, cache=cache)
    item = listing("Central A/C, no smoking.")

    assert extractor.extract(item) == KEYWORD_ONLY
    assert cache.get(content_hash(item, MODEL)) is None
    # Failures aren't cached: the next run asks again.
    assert extractor.extract(item).status(A.GYM) is True
    assert len(recorder.requests) == 2


def test_failure_is_logged_without_listing_text(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, format=LogFormat.JSON))
    phone = "call 555-0100 today"
    make_extractor(Recorder(httpx2.ConnectError("refused"))).extract(listing(phone))

    err = capsys.readouterr().err
    (line,) = [json.loads(raw) for raw in err.splitlines() if "llm_amenity" in raw]
    assert line["event"] == "llm_amenity_extraction_failed"
    assert (line["source"], line["source_id"], line["error"]) == ("test", "1", "ConnectError")
    assert phone not in err


def test_code_fenced_json_is_accepted() -> None:
    fenced = f"```json\n{verdicts(present=['gym'])}\n```"
    result = make_extractor(Recorder(chat_reply(fenced))).extract(listing())

    assert result.present == {A.GYM}


def test_bare_fence_is_rejected() -> None:
    result = make_extractor(Recorder(chat_reply("```"))).extract(listing())

    assert result == AmenityResult()


# --- Cache ------------------------------------------------------------------------


def test_cache_hit_skips_the_http_call() -> None:
    cache = InMemoryAmenityCache()
    recorder = Recorder(chat_reply(verdicts(present=["gym"])))
    extractor = make_extractor(recorder, cache=cache)

    first = extractor.extract(listing("Fitness room."))
    second = extractor.extract(listing("Fitness room."))

    assert first == second
    assert len(recorder.requests) == 1
    assert cache.get(content_hash(listing("Fitness room."), MODEL)) == AmenityResult(
        present=frozenset({A.GYM})
    )


def test_content_hash_covers_every_prompt_input() -> None:
    base = content_hash(listing("Desc", amenities=frozenset({"a", "b"})), MODEL)

    assert base == content_hash(listing("Desc", amenities=frozenset({"b", "a"})), MODEL)
    assert len(base) == 64
    variants = {
        content_hash(listing("Desc", amenities=frozenset({"a", "b"})), "other-model"),
        content_hash(listing("Other", amenities=frozenset({"a", "b"})), MODEL),
        content_hash(listing("Desc", title="New title", amenities=frozenset({"a", "b"})), MODEL),
        content_hash(listing("Desc", amenities=frozenset({"a"})), MODEL),
    }
    assert base not in variants
    assert len(variants) == 4


def test_source_metadata_does_not_change_the_hash() -> None:
    assert content_hash(listing("Desc"), MODEL) == content_hash(
        listing("Desc", source_id="2", url="https://example.com/2"), MODEL
    )


# --- Factory ----------------------------------------------------------------------


def test_factory_returns_keyword_extractor_when_disabled() -> None:
    extractor = build_amenity_extractor(LlmSettings(), InMemoryAmenityCache())

    assert isinstance(extractor, KeywordAmenityExtractor)


def test_factory_ignores_unimplemented_anthropic_provider() -> None:
    settings = LlmSettings(provider=LlmProvider.ANTHROPIC)

    assert isinstance(
        build_amenity_extractor(settings, InMemoryAmenityCache()), KeywordAmenityExtractor
    )


def test_factory_builds_llm_extractor_when_configured() -> None:
    settings = LlmSettings.model_validate(
        {
            "provider": "openai_compatible",
            "base_url": BASE_URL,
            "model": MODEL,
            "api_key": SecretStr("sk-test"),
            "timeout_seconds": 7,
        }
    )
    extractor = build_amenity_extractor(settings, InMemoryAmenityCache())

    assert isinstance(extractor, LlmAmenityExtractor)
    client = extractor._client
    assert client.model == MODEL
    assert str(client._http.base_url) == f"{BASE_URL}/"
    assert client._http.headers["authorization"] == "Bearer sk-test"
    assert client._http.timeout.read == 7
    client.close()
    assert client._http.is_closed
