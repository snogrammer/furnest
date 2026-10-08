"""Each outbound HTTP client gets the timeout its settings configure.

The clients' own tests check that a timeout is turned into the right error; these
check the configured value actually reaches the client, so no call can fall back to
waiting forever. (The scraper client, LLM, SMTP, IMAP, Apify polls and the CLI probe
are covered next to their other tests.)
"""

import httpx2
import pytest
from pydantic import HttpUrl, SecretStr

from furnest.config import (
    DiscordSettings,
    GeocodingSettings,
    ReverseImageProvider,
    ReverseImageSettings,
    Settings,
)
from furnest.enrichment.geocoding import NominatimGeocoder
from furnest.enrichment.reverse_image import _HttpSearch, build_reverse_image_search
from furnest.notify.discord import discord_client


def test_discord_client_uses_the_request_timeout() -> None:
    settings = Settings(
        discord=DiscordSettings(
            bot_token=SecretStr("bot-token"), channel_id="1", request_timeout_seconds=4
        )
    )

    client = discord_client(settings)

    assert client is not None
    assert client._http.timeout == httpx2.Timeout(4.0)


def test_geocoder_uses_the_geocoding_timeout() -> None:
    geocoder = NominatimGeocoder(
        GeocodingSettings(
            base_url=HttpUrl("https://geocode.example.com"),
            user_agent="furnest-test",
            timeout_seconds=6,
        )
    )

    assert geocoder._http.timeout == httpx2.Timeout(6.0)


@pytest.mark.parametrize(
    "provider",
    [ReverseImageProvider.SERPAPI, ReverseImageProvider.TINEYE, ReverseImageProvider.BING],
)
def test_reverse_image_search_uses_its_timeout(provider: ReverseImageProvider) -> None:
    search = build_reverse_image_search(
        ReverseImageSettings(provider=provider, api_key=SecretStr("key"), timeout_seconds=9)
    )

    assert isinstance(search, _HttpSearch)
    assert search._http.timeout == httpx2.Timeout(9.0)
