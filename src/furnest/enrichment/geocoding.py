"""Geocoding: an address in, coordinates and the geocoder's canonical address out.

Many listings give an address but no coordinates, or a vague address. Enrichment
(#54 onwards) fills the gap through a :class:`Geocoder`. The interface is small, so
providers can be swapped with ``GEOCODING_PROVIDER``:

* ``none`` (the default) is :class:`NullGeocoder`. It never finds anything and never
  touches the network.
* ``nominatim`` is :class:`NominatimGeocoder`, OpenStreetMap's geocoder, on the public
  instance, a self-hosted one or a hosted compatible API (``GEOCODING_BASE_URL``).

Nominatim's usage policy shapes the client. It sends an identifying User-Agent
(``GEOCODING_USER_AGENT``, required). It allows one request at a time per
``GEOCODING_REQUESTS_PER_MINUTE``, default 60, the public maximum, through a token
bucket shared by every thread. And it caches every answer, since the policy forbids
asking the same thing repeatedly.

That cache is :class:`CachedGeocoder` in front of the provider, with rows in the
``geocode_cache`` table (:class:`furnest.db.geocode_cache.SqlGeocodeCache`). A
result is kept forever, because addresses don't move. A "not found" is kept for
``GEOCODING_MISS_TTL_DAYS``, then asked again, since OpenStreetMap keeps growing.
A failure (timeout, HTTP error, garbage reply) raises :class:`GeocodingError` and is
never cached, so the caller just tries again later.

Every result carries a :class:`GeocodePrecision`. Nominatim falls back to the city
or postcode when it can't find the street, and a consumer placing a listing on a map
should not treat a city centroid as the listing's location.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol, Self

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from furnest.config import GeocodingProvider, GeocodingSettings
from furnest.db.base import utcnow
from furnest.domain.listing import Address
from furnest.logging import get_logger
from furnest.sources.ratelimit import TokenBucket

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

log = get_logger(__name__)


class GeocodePrecision(StrEnum):
    """How exact a result is, from most to least."""

    ADDRESS = "address"
    STREET = "street"
    POSTCODE = "postcode"
    LOCALITY = "locality"
    REGION = "region"


class GeocodeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    # The address as the geocoder knows it, which is usually cleaner than the source's.
    address: Address
    display_name: str
    precision: GeocodePrecision
    provider: str


class GeocodingError(Exception):
    """The geocoder couldn't answer. ``reason`` is short enough to log or store."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Geocoder(Protocol):
    def geocode(self, address: Address) -> GeocodeResult | None:
        """The best match for ``address``, or ``None`` when there is none (or the
        address is too empty to look up). Raises :class:`GeocodingError` on failure."""
        ...


class NullGeocoder:
    """``GEOCODING_PROVIDER=none``: finds nothing."""

    def geocode(self, address: Address) -> GeocodeResult | None:
        return None


# --- Queries --------------------------------------------------------------------

# A trailing unit ("Apt 4B", "Unit 2", "#4b", "Suite 300"). Geocoders know buildings,
# not doors, and many miss a street line with a unit still on it.
_TRAILING_UNIT = re.compile(
    r"[\s,]*(?:#|\b(?:apt|apartment|unit|suite|ste)\b\.?)\s*[\w-]+\s*$", re.IGNORECASE
)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = " ".join(value.split()).lower()
    return collapsed or None


def query_fields(address: Address) -> dict[str, str]:
    """The structured query for ``address``: lower-cased, whitespace-collapsed, the
    street without its unit. Empty when there is nothing to look up (no street, city
    or postcode)."""
    street = _clean(_TRAILING_UNIT.sub("", address.line1)) if address.line1 else None
    fields = {
        "street": street,
        "city": _clean(address.city),
        "state": _clean(address.region),
        "postalcode": _clean(address.postal_code),
    }
    if not (fields["street"] or fields["city"] or fields["postalcode"]):
        return {}
    fields["country"] = address.country.lower()
    return {name: value for name, value in fields.items() if value}


def cache_key(provider: str, fields: dict[str, str]) -> str:
    """SHA-256 over the provider and the query, so equal queries share a row."""
    payload = json.dumps([provider, sorted(fields.items())], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


# --- Nominatim ------------------------------------------------------------------

_LOCALITY_TYPES = frozenset(
    {
        "city",
        "town",
        "village",
        "hamlet",
        "municipality",
        "borough",
        "suburb",
        "quarter",
        "neighbourhood",
        "city_district",
        "locality",
    }
)


def _precision(item: dict[str, Any], details: dict[str, Any]) -> GeocodePrecision:
    if details.get("house_number"):
        return GeocodePrecision.ADDRESS
    kind = item.get("addresstype") or item.get("type")
    if kind in {"road", "street"}:
        return GeocodePrecision.STREET
    if kind == "postcode":
        return GeocodePrecision.POSTCODE
    if kind in _LOCALITY_TYPES:
        return GeocodePrecision.LOCALITY
    return GeocodePrecision.REGION


def _first(details: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _address(details: dict[str, Any]) -> Address:
    street = " ".join(
        part for part in (_first(details, "house_number"), _first(details, "road")) if part
    )
    # "US-TX" → "TX"; outside the US the subdivision code may be missing.
    iso = _first(details, "ISO3166-2-lvl4")
    region = iso.split("-", 1)[1] if iso and "-" in iso else _first(details, "state")
    country = _first(details, "country_code")
    return Address(
        line1=street or None,
        city=_first(details, "city", "town", "village", "hamlet", "municipality"),
        region=region,
        postal_code=_first(details, "postcode"),
        country=country.upper() if country and len(country) == 2 else "US",
        neighborhood=_first(details, "neighbourhood", "suburb", "quarter"),
    )


class NominatimGeocoder:
    """Nominatim's structured ``/search``. Tests pass an ``httpx2.MockTransport`` and
    a fake clock and sleep."""

    provider = GeocodingProvider.NOMINATIM.value

    def __init__(
        self,
        settings: GeocodingSettings,
        *,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if settings.base_url is None or not settings.user_agent:
            msg = "Nominatim needs GEOCODING_BASE_URL and GEOCODING_USER_AGENT"
            raise ValueError(msg)
        self._search_url = str(settings.base_url).rstrip("/") + "/search"
        self._api_key = settings.api_key
        self._http = httpx2.Client(
            transport=transport,
            timeout=httpx2.Timeout(settings.timeout_seconds),
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
        )
        self._limiter = TokenBucket(settings.requests_per_minute, clock=clock, sleep=sleep)

    def geocode(self, address: Address) -> GeocodeResult | None:
        fields = query_fields(address)
        if not fields:
            return None
        params = {
            **{name: value for name, value in fields.items() if name != "country"},
            "countrycodes": fields["country"],
            "format": "jsonv2",
            "addressdetails": "1",
            "limit": "1",
        }
        if self._api_key is not None:
            params["key"] = self._api_key.get_secret_value()
        return self._parse(self._get(params))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- Internals ----------------------------------------------------------------

    def _get(self, params: dict[str, str]) -> object:
        self._limiter.acquire()
        try:
            response = self._http.get(self._search_url, params=params)
        except httpx2.TimeoutException as exc:
            raise GeocodingError("timeout") from exc
        except httpx2.HTTPError as exc:
            raise GeocodingError(f"transport error: {type(exc).__name__}") from exc
        if response.is_error:
            raise GeocodingError(f"HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise GeocodingError("malformed response") from exc

    def _parse(self, body: object) -> GeocodeResult | None:
        if not isinstance(body, list):
            raise GeocodingError("malformed response")
        if not body:
            return None
        item = body[0]
        if not isinstance(item, dict):
            raise GeocodingError("malformed response")
        details = item.get("address")
        details = details if isinstance(details, dict) else {}
        try:
            return GeocodeResult(
                lat=float(item["lat"]),
                lng=float(item["lon"]),
                address=_address(details),
                display_name=str(item.get("display_name") or ""),
                precision=_precision(item, details),
                provider=self.provider,
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise GeocodingError("malformed response") from exc


# --- Cache ----------------------------------------------------------------------


@dataclass(frozen=True)
class CachedGeocode:
    """A stored answer: the result, or ``None`` for "not found"."""

    result: GeocodeResult | None
    created_at: datetime


class GeocodeCache(Protocol):
    def get(self, key: str) -> CachedGeocode | None: ...

    def put(
        self,
        key: str,
        *,
        provider: str,
        query: dict[str, str],
        result: GeocodeResult | None,
        at: datetime,
    ) -> None:
        """Store an answer, replacing any earlier one for ``key``."""
        ...


class InMemoryGeocodeCache:
    """Dict-backed :class:`GeocodeCache`, for tests and as a reference implementation."""

    def __init__(self) -> None:
        self._values: dict[str, CachedGeocode] = {}

    def get(self, key: str) -> CachedGeocode | None:
        return self._values.get(key)

    def put(
        self,
        key: str,
        *,
        provider: str,
        query: dict[str, str],
        result: GeocodeResult | None,
        at: datetime,
    ) -> None:
        self._values[key] = CachedGeocode(result=result, created_at=at)


class CachedGeocoder:
    """Answers from ``cache`` when it can, otherwise asks ``inner`` and stores the
    answer. Errors from ``inner`` propagate and are not stored."""

    def __init__(
        self,
        inner: Geocoder,
        cache: GeocodeCache,
        *,
        provider: str,
        miss_ttl: timedelta,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._provider = provider
        self._miss_ttl = miss_ttl
        self._clock = clock

    def geocode(self, address: Address) -> GeocodeResult | None:
        fields = query_fields(address)
        if not fields:
            return None
        key = cache_key(self._provider, fields)
        cached = self._cache.get(key)
        now = self._clock()
        if cached is not None and (
            cached.result is not None or now - cached.created_at < self._miss_ttl
        ):
            return cached.result
        result = self._inner.geocode(address)
        self._cache.put(key, provider=self._provider, query=fields, result=result, at=now)
        log.info("geocoded", provider=self._provider, found=result is not None)
        return result


# --- Wiring ---------------------------------------------------------------------


def build_geocoder(settings: GeocodingSettings, session_factory: sessionmaker[Session]) -> Geocoder:
    """The geocoder ``GEOCODING_PROVIDER`` selects, behind the database cache."""
    if settings.provider is GeocodingProvider.NONE:
        return NullGeocoder()
    # Imported here: the store module imports this one for its types.
    from furnest.db.geocode_cache import SqlGeocodeCache

    return CachedGeocoder(
        NominatimGeocoder(settings),
        SqlGeocodeCache(session_factory),
        provider=GeocodingProvider.NOMINATIM.value,
        miss_ttl=timedelta(days=settings.miss_ttl_days),
    )
