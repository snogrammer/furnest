"""LlmAmenityExtractor: an optional LLM pass layered on the keyword extractor.

The keyword extractor always runs first and its answer stands. The LLM only fills in
tags the keyword pass left unknown, which is where it helps most: phrasing no regex
anticipated ("bring your golden retriever", "sweat it out in the building's fitness
room"). Where both have an opinion the deterministic one wins. It already ranks the
adapter's structured labels above prose and "absent" above "present", and a model
should not overturn either.

The model is reached through any OpenAI-compatible ``/chat/completions`` endpoint, so
a hosted API and a local Ollama look the same. Its reply must match a strict JSON
schema of two lists of :class:`~furnest.domain.Amenity` values. Anything else (a
transport error, a timeout, a non-2xx status, malformed JSON, an unknown tag, a tag
both present and absent) is logged and the keyword result is returned alone. An LLM
outage therefore degrades matching quality but never stops it.

Successful verdicts are cached by :func:`content_hash`. The hash covers the model,
the prompt version and every listing field the prompt shows, so re-scraping an
unchanged listing costs nothing and any change in input or prompt misses. Failures
are never cached, so a transient error is retried on the next run.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

import httpx2
from pydantic import BaseModel, ConfigDict, Field, model_validator

from furnest.config import LlmProvider, LlmSettings
from furnest.domain import Amenity, AmenityExtractor, AmenityResult, NormalizedListing
from furnest.logging import get_logger
from furnest.matching.amenities.keyword import KeywordAmenityExtractor, reconcile_pets

log = get_logger(__name__)

# Bump whenever the prompt or schema changes, so cached verdicts are not reused.
PROMPT_VERSION = "1"
# Long descriptions are mostly boilerplate past this point; cap the cost per call.
_MAX_DESCRIPTION_CHARS = 8000


class AmenityCache(Protocol):
    """Raw LLM verdicts keyed by :func:`content_hash`."""

    def get(self, key: str) -> AmenityResult | None: ...

    def put(self, key: str, model: str, result: AmenityResult) -> None: ...


class InMemoryAmenityCache:
    """Dict-backed :class:`AmenityCache`, for tests and as a reference implementation."""

    def __init__(self) -> None:
        self._values: dict[str, AmenityResult] = {}

    def get(self, key: str) -> AmenityResult | None:
        return self._values.get(key)

    def put(self, key: str, model: str, result: AmenityResult) -> None:
        self._values[key] = result


def content_hash(listing: NormalizedListing, model: str) -> str:
    """SHA-256 over everything that can change the LLM's answer."""
    payload = json.dumps(
        [PROMPT_VERSION, model, listing.title, listing.description, sorted(listing.amenities)],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# --- Prompt and response schema -----------------------------------------------

_TAGS = [tag.value for tag in Amenity]

_SYSTEM_PROMPT = f"""\
You extract amenities from a rental listing. Use only these tags: {", ".join(_TAGS)}.
Put a tag in "present" only if the listing says the unit or building has it.
Put a tag in "absent" only if the listing explicitly rules it out (e.g. "no pets"
rules out pets_allowed, "no A/C" rules out ac).
Leave out every tag the listing does not mention. Never guess.
Reply with JSON only: {{"present": [...], "absent": [...]}}."""

_TAG_LIST_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "enum": _TAGS},
    "uniqueItems": True,
}
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"present": _TAG_LIST_SCHEMA, "absent": _TAG_LIST_SCHEMA},
    "required": ["present", "absent"],
    "additionalProperties": False,
}


def _user_prompt(listing: NormalizedListing) -> str:
    labels = ", ".join(sorted(listing.amenities)) or "(none)"
    description = listing.description[:_MAX_DESCRIPTION_CHARS]
    return f"Title: {listing.title}\nAmenity labels: {labels}\nDescription:\n{description}"


class _Verdicts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    present: list[Amenity]
    absent: list[Amenity]

    @model_validator(mode="after")
    def _disjoint(self) -> _Verdicts:
        if overlap := set(self.present) & set(self.absent):
            msg = f"tags both present and absent: {sorted(overlap)}"
            raise ValueError(msg)
        return self


class _Message(BaseModel):
    content: str


class _Choice(BaseModel):
    message: _Message


class _ChatResponse(BaseModel):
    choices: list[_Choice] = Field(min_length=1)


def _strip_code_fence(content: str) -> str:
    # Some local models wrap JSON in ```json fences even when asked not to.
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.removesuffix("```").strip()
    return text


# --- Client --------------------------------------------------------------------


class OpenAICompatibleClient:
    """Minimal ``/chat/completions`` client returning validated amenity verdicts.

    Raises :class:`httpx2.HTTPError` for transport, timeout and status failures and
    :class:`ValueError` (including pydantic's ``ValidationError``) for a reply that
    does not match the schema.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self.model = model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx2.Client(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def extract(self, listing: NormalizedListing) -> AmenityResult:
        response = self._http.post(
            "chat/completions",
            json={
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _user_prompt(listing)},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "amenities", "strict": True, "schema": RESPONSE_SCHEMA},
                },
            },
        )
        response.raise_for_status()
        content = _ChatResponse.model_validate_json(response.content).choices[0].message.content
        verdicts = _Verdicts.model_validate_json(_strip_code_fence(content))
        return AmenityResult(present=frozenset(verdicts.present), absent=frozenset(verdicts.absent))


# --- Extractor -------------------------------------------------------------------


def merge(base: AmenityResult, extra: AmenityResult) -> AmenityResult:
    """``base`` verdicts stand; ``extra`` only fills tags ``base`` left unknown."""
    verdicts = {tag: False for tag in extra.absent} | {tag: True for tag in extra.present}
    verdicts |= {tag: False for tag in base.absent} | {tag: True for tag in base.present}
    reconcile_pets(verdicts)
    return AmenityResult(
        present=frozenset(tag for tag, present in verdicts.items() if present),
        absent=frozenset(tag for tag, present in verdicts.items() if not present),
        unrecognized=base.unrecognized,
    )


class LlmAmenityExtractor:
    def __init__(
        self,
        client: OpenAICompatibleClient,
        cache: AmenityCache,
        fallback: AmenityExtractor | None = None,
    ) -> None:
        self._client = client
        self._cache = cache
        self._fallback = fallback or KeywordAmenityExtractor()

    def extract(self, listing: NormalizedListing) -> AmenityResult:
        base = self._fallback.extract(listing)
        key = content_hash(listing, self._client.model)
        llm = self._cache.get(key)
        if llm is None:
            try:
                llm = self._client.extract(listing)
            except (httpx2.HTTPError, ValueError) as exc:
                # No listing text in the log: descriptions can carry contact details.
                log.warning(
                    "llm_amenity_extraction_failed",
                    source=listing.source,
                    source_id=listing.source_id,
                    error=type(exc).__name__,
                )
                return base
            self._cache.put(key, self._client.model, llm)
        return merge(base, llm)


def build_amenity_extractor(settings: LlmSettings, cache: AmenityCache) -> AmenityExtractor:
    """The LLM extractor when configured, else the keyword extractor."""
    if settings.provider is LlmProvider.ANTHROPIC:
        log.warning("llm_provider_not_implemented", provider=settings.provider.value)
    if not settings.openai_compatible_enabled:
        return KeywordAmenityExtractor()
    assert settings.base_url is not None  # noqa: S101 - guaranteed by LlmSettings' validator
    assert settings.model is not None  # noqa: S101
    client = OpenAICompatibleClient(
        str(settings.base_url),
        settings.model,
        api_key=settings.api_key.get_secret_value() if settings.api_key else None,
        timeout=settings.timeout_seconds,
    )
    return LlmAmenityExtractor(client, cache)
