"""SqlGeocodeCache: geocoder answers over the ``geocode_cache`` table.

Implements :class:`furnest.enrichment.geocoding.GeocodeCache`. Unlike the amenity
cache a row can be replaced: a "not found" that has expired is asked again, and the
new answer overwrites it. Two workers asking at once both write; the last one wins,
and both wrote the same kind of answer.
"""

from datetime import datetime

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.models import GeocodeCacheEntry
from furnest.db.session import session_scope
from furnest.domain.listing import Address
from furnest.enrichment.geocoding import CachedGeocode, GeocodePrecision, GeocodeResult


class SqlGeocodeCache:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self, key: str) -> CachedGeocode | None:
        with session_scope(self._session_factory) as session:
            row = session.get(GeocodeCacheEntry, key)
            if row is None:
                return None
            return CachedGeocode(result=_result(row), created_at=row.created_at)

    def put(
        self,
        key: str,
        *,
        provider: str,
        query: dict[str, str],
        result: GeocodeResult | None,
        at: datetime,
    ) -> None:
        values = {
            "provider": provider,
            "query": query,
            "found": result is not None,
            "lat": result.lat if result else None,
            "lng": result.lng if result else None,
            "address": result.address.model_dump(mode="json") if result else None,
            "display_name": result.display_name if result else None,
            "precision": result.precision.value if result else None,
            "created_at": at,
        }
        statement = insert(GeocodeCacheEntry).values(key=key, **values)
        statement = statement.on_conflict_do_update(index_elements=["key"], set_=values)
        with session_scope(self._session_factory) as session:
            session.execute(statement)


def _result(row: GeocodeCacheEntry) -> GeocodeResult | None:
    if not row.found or row.lat is None or row.lng is None or row.precision is None:
        return None
    return GeocodeResult(
        lat=row.lat,
        lng=row.lng,
        address=Address.model_validate(row.address or {}),
        display_name=row.display_name or "",
        precision=GeocodePrecision(row.precision),
        provider=row.provider,
    )
