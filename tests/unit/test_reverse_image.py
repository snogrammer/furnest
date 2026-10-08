"""Reverse image providers against faked HTTP; no network, no keys."""

from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest
from pydantic import SecretStr

from furnest.config import ReverseImageProvider, ReverseImageSettings
from furnest.enrichment.reverse_image import (
    BingVisualSearch,
    NullReverseImageSearch,
    ReverseImageError,
    ReverseImageMatch,
    SerpApiLensSearch,
    TinEyeSearch,
    build_reverse_image_search,
)

PHOTO = "https://img.example.com/a.jpg"


def settings(provider: ReverseImageProvider, **fields: Any) -> ReverseImageSettings:  # noqa: ANN401
    return ReverseImageSettings(provider=provider, api_key=SecretStr("secret-key"), **fields)


def transport(
    body: object | None = None, status: int = 200, seen: list[httpx2.Request] | None = None
) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return httpx2.Response(status, json=body)

    return httpx2.MockTransport(handle)


def test_unconfigured_is_a_no_op() -> None:
    none = ReverseImageSettings(provider=ReverseImageProvider.NONE)
    keyless = ReverseImageSettings(provider=ReverseImageProvider.SERPAPI)
    for configured in (none, keyless):
        search = build_reverse_image_search(configured)
        assert isinstance(search, NullReverseImageSearch)
        assert search.search(PHOTO) == []


def test_builder_selects_each_provider() -> None:
    for provider, kind in (
        (ReverseImageProvider.SERPAPI, SerpApiLensSearch),
        (ReverseImageProvider.TINEYE, TinEyeSearch),
        (ReverseImageProvider.BING, BingVisualSearch),
    ):
        built = build_reverse_image_search(settings(provider), transport=transport())
        assert isinstance(built, kind)


def test_serpapi_lens() -> None:
    seen: list[httpx2.Request] = []
    body = {
        "visual_matches": [
            {"title": " Cozy flat ", "link": "https://a.example.com/1"},
            {"title": "no link"},
            {"title": "bad", "link": "not a url"},
            {"title": "Second", "link": "https://b.example.com/2"},
        ]
    }
    search = SerpApiLensSearch(
        settings(ReverseImageProvider.SERPAPI, max_results=2), transport=transport(body, seen=seen)
    )
    matches = search.search(PHOTO)
    assert [(str(m.page_url), m.title, m.provider) for m in matches] == [
        ("https://a.example.com/1", "Cozy flat", "serpapi"),
        ("https://b.example.com/2", "Second", "serpapi"),
    ]
    query = parse_qs(seen[0].url.query.decode())
    assert query["engine"] == ["google_lens"]
    assert query["url"] == [PHOTO]
    assert query["api_key"] == ["secret-key"]


def test_serpapi_no_results_is_empty_but_other_errors_raise() -> None:
    empty = {"error": "Google Lens hasn't returned any results for this query."}
    search = SerpApiLensSearch(settings(ReverseImageProvider.SERPAPI), transport=transport(empty))
    assert search.search(PHOTO) == []
    with pytest.raises(ReverseImageError, match="provider error"):
        SerpApiLensSearch(
            settings(ReverseImageProvider.SERPAPI), transport=transport({"error": "Invalid key"})
        ).search(PHOTO)


def test_tineye() -> None:
    seen: list[httpx2.Request] = []
    body = {
        "results": {
            "matches": [
                {"backlinks": [{"backlink": "https://a.example.com/x"}, {"url": "ignored"}]},
                {"backlinks": [{"backlink": "https://b.example.com/y"}]},
            ]
        }
    }
    matches = TinEyeSearch(
        settings(ReverseImageProvider.TINEYE), transport=transport(body, seen=seen)
    ).search(PHOTO)
    assert [str(m.page_url) for m in matches] == [
        "https://a.example.com/x",
        "https://b.example.com/y",
    ]
    assert {m.provider for m in matches} == {"tineye"}
    assert parse_qs(seen[0].url.query.decode())["image_url"] == [PHOTO]


def test_bing_visual_search() -> None:
    seen: list[httpx2.Request] = []
    body = {
        "tags": [
            {
                "actions": [
                    {"actionType": "Entity", "data": {"value": []}},
                    {
                        "actionType": "PagesIncluding",
                        "data": {
                            "value": [
                                {"name": "Listing", "hostPageUrl": "https://c.example.com/3"},
                                {"name": "no url"},
                            ]
                        },
                    },
                ]
            }
        ]
    }
    matches = BingVisualSearch(
        settings(ReverseImageProvider.BING), transport=transport(body, seen=seen)
    ).search(PHOTO)
    assert [(str(m.page_url), m.title) for m in matches] == [("https://c.example.com/3", "Listing")]
    request = seen[0]
    assert request.method == "POST"
    assert request.headers["Ocp-Apim-Subscription-Key"] == "secret-key"
    assert PHOTO.encode() in request.read()


@pytest.mark.parametrize(
    "provider",
    [ReverseImageProvider.SERPAPI, ReverseImageProvider.TINEYE, ReverseImageProvider.BING],
)
def test_failures_raise_reverse_image_error(provider: ReverseImageProvider) -> None:
    def search(handler: httpx2.MockTransport) -> list[ReverseImageMatch]:
        return build_reverse_image_search(settings(provider), transport=handler).search(PHOTO)

    with pytest.raises(ReverseImageError, match="HTTP 500"):
        search(transport({}, 500))
    with pytest.raises(ReverseImageError, match="malformed"):
        search(transport(["x"]))

    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    with pytest.raises(ReverseImageError, match="timeout"):
        search(httpx2.MockTransport(boom))
