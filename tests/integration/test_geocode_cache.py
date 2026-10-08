"""SqlGeocodeCache on a migrated database, and CachedGeocoder in front of it."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.geocode_cache import SqlGeocodeCache
from furnest.db.models import GeocodeCacheEntry
from furnest.db.session import session_scope
from furnest.domain.listing import Address
from furnest.enrichment.geocoding import (
    CachedGeocoder,
    GeocodePrecision,
    GeocodeResult,
    query_fields,
)

pytestmark = pytest.mark.integration

AT = datetime(2026, 10, 7, 12, tzinfo=UTC)
QUERY = {"city": "springfield", "country": "us"}
RESULT = GeocodeResult(
    lat=39.8,
    lng=-89.65,
    address=Address(line1="123 N Main St", city="Springfield", region="IL", postal_code="62701"),
    display_name="123, North Main Street, Springfield",
    precision=GeocodePrecision.ADDRESS,
    provider="nominatim",
)


@pytest.fixture
def cache(session_factory: sessionmaker[Session]) -> SqlGeocodeCache:
    return SqlGeocodeCache(session_factory)


def test_a_result_round_trips(cache: SqlGeocodeCache) -> None:
    assert cache.get("a" * 64) is None

    cache.put("a" * 64, provider="nominatim", query=QUERY, result=RESULT, at=AT)

    stored = cache.get("a" * 64)
    assert stored is not None
    assert stored.result == RESULT
    assert stored.created_at == AT


def test_a_miss_round_trips(cache: SqlGeocodeCache, session_factory: sessionmaker[Session]) -> None:
    cache.put("b" * 64, provider="nominatim", query=QUERY, result=None, at=AT)

    stored = cache.get("b" * 64)
    assert stored is not None
    assert stored.result is None
    with session_scope(session_factory) as session:
        row = session.get(GeocodeCacheEntry, "b" * 64)
        assert row is not None
        assert row.found is False
        assert row.query == QUERY


def test_put_replaces_an_earlier_answer(
    cache: SqlGeocodeCache, session_factory: sessionmaker[Session]
) -> None:
    cache.put("c" * 64, provider="nominatim", query=QUERY, result=None, at=AT)
    later = AT + timedelta(days=31)

    cache.put("c" * 64, provider="nominatim", query=QUERY, result=RESULT, at=later)

    stored = cache.get("c" * 64)
    assert stored is not None
    assert stored.result == RESULT
    assert stored.created_at == later
    with session_scope(session_factory) as session:
        assert session.scalar(select(func.count()).select_from(GeocodeCacheEntry)) == 1


class Inner:
    def __init__(self) -> None:
        self.calls = 0

    def geocode(self, address: Address) -> GeocodeResult | None:
        self.calls += 1
        return RESULT


def test_cached_geocoder_persists_across_instances(cache: SqlGeocodeCache) -> None:
    address = Address(line1="123 N Main St", city="Springfield", region="IL")
    inner = Inner()

    def make() -> CachedGeocoder:
        return CachedGeocoder(
            inner, cache, provider="nominatim", miss_ttl=timedelta(days=30), clock=lambda: AT
        )

    assert make().geocode(address) == RESULT
    assert make().geocode(address) == RESULT
    assert inner.calls == 1
    assert query_fields(address)["street"] == "123 n main st"
