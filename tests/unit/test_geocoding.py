"""Geocoding: Nominatim requests and parsing, rate limiting, caching and wiring.

Nominatim is faked with ``httpx2.MockTransport``; time is a fake clock.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest
from pydantic import HttpUrl, SecretStr

from furnest.config import GeocodingProvider, GeocodingSettings
from furnest.domain.listing import Address
from furnest.enrichment.geocoding import (
    CachedGeocoder,
    GeocodePrecision,
    GeocodeResult,
    GeocodingError,
    InMemoryGeocodeCache,
    NominatimGeocoder,
    NullGeocoder,
    build_geocoder,
    cache_key,
    query_fields,
)

ADDRESS = Address(
    line1="123 North Main Street Apt 4B", city="Springfield", region="IL", postal_code="62701"
)

HOUSE: dict[str, Any] = {
    "lat": "39.7990175",
    "lon": "-89.6439575",
    "display_name": "123, North Main Street, Springfield, Sangamon County, Illinois, 62701, US",
    "addresstype": "building",
    "address": {
        "house_number": "123",
        "road": "North Main Street",
        "neighbourhood": "Downtown",
        "city": "Springfield",
        "state": "Illinois",
        "ISO3166-2-lvl4": "US-IL",
        "postcode": "62701",
        "country_code": "us",
    },
}

TOWN: dict[str, Any] = {
    "lat": "39.8",
    "lon": "-89.65",
    "display_name": "Springfield, Illinois, US",
    "addresstype": "city",
    "address": {"city": "Springfield", "state": "Illinois", "country_code": "us"},
}


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Nominatim:
    """Records requests and gives one reply (or raises one exception) for all."""

    def __init__(self, reply: httpx2.Response | Exception) -> None:
        self.reply = reply
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def params(self, index: int = 0) -> dict[str, str]:
        query = parse_qs(self.requests[index].url.query.decode())
        return {name: values[0] for name, values in query.items()}


def settings(**overrides: object) -> GeocodingSettings:
    values: dict[str, object] = {
        "provider": GeocodingProvider.NOMINATIM,
        "base_url": HttpUrl("https://geocoder.example.com/"),
        "user_agent": "furnest-test (admin@example.com)",
        **overrides,
    }
    return GeocodingSettings.model_validate(values)


Make = Callable[..., tuple[NominatimGeocoder, Nominatim]]


@pytest.fixture
def t() -> FakeTime:
    return FakeTime()


@pytest.fixture
def make(t: FakeTime) -> Make:
    def build(
        reply: httpx2.Response | Exception, **overrides: object
    ) -> tuple[NominatimGeocoder, Nominatim]:
        server = Nominatim(reply)
        geocoder = NominatimGeocoder(
            settings(**overrides),
            transport=httpx2.MockTransport(server),
            clock=t.clock,
            sleep=t.sleep,
        )
        return geocoder, server

    return build


# --- Queries --------------------------------------------------------------------


def test_query_fields_normalize_and_drop_the_unit() -> None:
    assert query_fields(ADDRESS) == {
        "street": "123 north main street",
        "city": "springfield",
        "state": "il",
        "postalcode": "62701",
        "country": "us",
    }


@pytest.mark.parametrize(
    ("line1", "street"),
    [
        ("12 Elm St #4b", "12 elm st"),
        ("12 Elm St, Unit 2", "12 elm st"),
        ("12 Elm St Suite 300", "12 elm st"),
        ("12  Elm   St", "12 elm st"),
        ("12 Unity Ave", "12 unity ave"),
    ],
)
def test_query_fields_strip_trailing_units_only(line1: str, street: str) -> None:
    assert query_fields(Address(line1=line1, city="X"))["street"] == street


def test_query_fields_are_empty_without_street_city_or_postcode() -> None:
    assert query_fields(Address(region="IL")) == {}


def test_cache_key_ignores_case_and_whitespace() -> None:
    other = Address(
        line1="123  north MAIN street", city=" springfield", region="il", postal_code="62701"
    )

    assert cache_key("nominatim", query_fields(ADDRESS)) == cache_key(
        "nominatim", query_fields(other)
    )
    assert cache_key("nominatim", query_fields(ADDRESS)) != cache_key(
        "other", query_fields(ADDRESS)
    )


# --- Nominatim requests ---------------------------------------------------------


def test_sends_a_structured_search_with_the_configured_user_agent(make: Make) -> None:
    geocoder, server = make(httpx2.Response(200, json=[HOUSE]))

    geocoder.geocode(ADDRESS)

    request = server.requests[0]
    assert str(request.url).startswith("https://geocoder.example.com/search?")
    assert request.headers["user-agent"] == "furnest-test (admin@example.com)"
    assert server.params() == {
        "street": "123 north main street",
        "city": "springfield",
        "state": "il",
        "postalcode": "62701",
        "countrycodes": "us",
        "format": "jsonv2",
        "addressdetails": "1",
        "limit": "1",
    }


def test_sends_the_api_key_only_when_configured(make: Make) -> None:
    geocoder, server = make(httpx2.Response(200, json=[]), api_key=SecretStr("k-123"))

    geocoder.geocode(ADDRESS)

    assert server.params()["key"] == "k-123"


def test_base_url_path_is_kept(make: Make) -> None:
    geocoder, server = make(
        httpx2.Response(200, json=[]), base_url=HttpUrl("https://geocoder.example.com/osm")
    )

    geocoder.geocode(ADDRESS)

    assert server.requests[0].url.path == "/osm/search"


def test_empty_address_makes_no_request(make: Make) -> None:
    geocoder, server = make(httpx2.Response(200, json=[HOUSE]))

    assert geocoder.geocode(Address()) is None
    assert server.requests == []


def test_requires_base_url_and_user_agent() -> None:
    with pytest.raises(ValueError, match="GEOCODING_USER_AGENT"):
        NominatimGeocoder(GeocodingSettings(base_url=HttpUrl("https://geocoder.example.com")))


# --- Nominatim responses --------------------------------------------------------


def test_house_level_result(make: Make) -> None:
    geocoder, _ = make(httpx2.Response(200, json=[HOUSE]))

    result = geocoder.geocode(ADDRESS)

    assert result == GeocodeResult(
        lat=39.7990175,
        lng=-89.6439575,
        address=Address(
            line1="123 North Main Street",
            city="Springfield",
            region="IL",
            postal_code="62701",
            country="US",
            neighborhood="Downtown",
        ),
        display_name=HOUSE["display_name"],
        precision=GeocodePrecision.ADDRESS,
        provider="nominatim",
    )


def test_city_level_result_is_marked_as_locality(make: Make) -> None:
    geocoder, _ = make(httpx2.Response(200, json=[TOWN]))

    result = geocoder.geocode(ADDRESS)

    assert result is not None
    assert result.precision is GeocodePrecision.LOCALITY
    assert result.address.line1 is None
    # No ISO subdivision code: the state name is kept as given.
    assert result.address.region == "Illinois"


@pytest.mark.parametrize(
    ("addresstype", "precision"),
    [
        ("road", GeocodePrecision.STREET),
        ("postcode", GeocodePrecision.POSTCODE),
        ("village", GeocodePrecision.LOCALITY),
        ("state", GeocodePrecision.REGION),
    ],
)
def test_precision_follows_the_address_type(
    make: Make, addresstype: str, precision: GeocodePrecision
) -> None:
    geocoder, _ = make(httpx2.Response(200, json=[{**TOWN, "addresstype": addresstype}]))

    result = geocoder.geocode(ADDRESS)

    assert result is not None
    assert result.precision is precision


def test_no_match_is_none(make: Make) -> None:
    geocoder, _ = make(httpx2.Response(200, json=[]))

    assert geocoder.geocode(ADDRESS) is None


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (httpx2.Response(429), "HTTP 429"),
        (httpx2.Response(503), "HTTP 503"),
        (httpx2.ReadTimeout("slow"), "timeout"),
        (httpx2.ConnectError("refused"), "transport error: ConnectError"),
        (httpx2.Response(200, text="<html>"), "malformed response"),
        (httpx2.Response(200, json={"error": "x"}), "malformed response"),
        (httpx2.Response(200, json=["x"]), "malformed response"),
        (httpx2.Response(200, json=[{"lat": "x", "lon": "1"}]), "malformed response"),
        (httpx2.Response(200, json=[{"lat": "91", "lon": "1"}]), "malformed response"),
        (httpx2.Response(200, json=[{"display_name": "no coordinates"}]), "malformed response"),
    ],
)
def test_failures_raise_geocoding_error(
    make: Make, reply: httpx2.Response | Exception, reason: str
) -> None:
    geocoder, _ = make(reply)

    with pytest.raises(GeocodingError) as excinfo:
        geocoder.geocode(ADDRESS)

    assert excinfo.value.reason == reason


def test_requests_are_spaced_by_the_rate_limit(make: Make, t: FakeTime) -> None:
    geocoder, server = make(httpx2.Response(200, json=[]))

    for _ in range(3):
        geocoder.geocode(ADDRESS)

    assert len(server.requests) == 3
    assert t.slept == pytest.approx([1.0, 1.0])


def test_closes_its_client(make: Make) -> None:
    geocoder, _ = make(httpx2.Response(200, json=[]))

    with geocoder:
        pass

    with pytest.raises(RuntimeError):
        geocoder.geocode(ADDRESS)


# --- Cache ----------------------------------------------------------------------

RESULT = GeocodeResult(
    lat=39.8,
    lng=-89.6,
    address=Address(city="Springfield"),
    display_name="Springfield",
    precision=GeocodePrecision.LOCALITY,
    provider="fake",
)


class FakeGeocoder:
    def __init__(self, answer: GeocodeResult | Exception | None) -> None:
        self.answer = answer
        self.calls = 0

    def geocode(self, address: Address) -> GeocodeResult | None:
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 7, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def cached(inner: FakeGeocoder, clock: Clock) -> CachedGeocoder:
    return CachedGeocoder(
        inner, InMemoryGeocodeCache(), provider="fake", miss_ttl=timedelta(days=30), clock=clock
    )


def test_a_result_is_cached_forever() -> None:
    inner, clock = FakeGeocoder(RESULT), Clock()
    geocoder = cached(inner, clock)

    assert geocoder.geocode(ADDRESS) == RESULT
    clock.now += timedelta(days=3650)
    assert geocoder.geocode(ADDRESS) == RESULT
    assert inner.calls == 1


def test_a_miss_is_cached_until_it_expires() -> None:
    inner, clock = FakeGeocoder(None), Clock()
    geocoder = cached(inner, clock)

    assert geocoder.geocode(ADDRESS) is None
    clock.now += timedelta(days=29)
    assert geocoder.geocode(ADDRESS) is None
    assert inner.calls == 1

    clock.now += timedelta(days=1)
    inner.answer = RESULT
    assert geocoder.geocode(ADDRESS) == RESULT
    assert inner.calls == 2


def test_equivalent_addresses_share_a_cache_entry() -> None:
    inner = FakeGeocoder(RESULT)
    geocoder = cached(inner, Clock())

    geocoder.geocode(ADDRESS)
    geocoder.geocode(ADDRESS.model_copy(update={"city": "SPRINGFIELD "}))

    assert inner.calls == 1


def test_errors_are_not_cached() -> None:
    inner = FakeGeocoder(GeocodingError("HTTP 503"))
    geocoder = cached(inner, Clock())

    with pytest.raises(GeocodingError):
        geocoder.geocode(ADDRESS)
    inner.answer = RESULT

    assert geocoder.geocode(ADDRESS) == RESULT
    assert inner.calls == 2


def test_empty_address_skips_cache_and_provider() -> None:
    inner = FakeGeocoder(RESULT)

    assert cached(inner, Clock()).geocode(Address()) is None
    assert inner.calls == 0


# --- Wiring ---------------------------------------------------------------------


def test_null_geocoder_finds_nothing() -> None:
    assert NullGeocoder().geocode(ADDRESS) is None


def test_build_geocoder_none_is_null() -> None:
    geocoder = build_geocoder(GeocodingSettings(), session_factory=None)  # type: ignore[arg-type]

    assert isinstance(geocoder, NullGeocoder)


def test_build_geocoder_nominatim_is_cached() -> None:
    geocoder = build_geocoder(settings(), session_factory=None)  # type: ignore[arg-type]

    assert isinstance(geocoder, CachedGeocoder)
