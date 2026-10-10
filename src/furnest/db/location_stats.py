"""Cheap reads over stored listings for the criteria editor's location helpers (#180).

Neighborhood suggestions come from the ``address.neighborhood`` values stored on
listings in a city, and the match counts say how many active listings each location
criterion would catch today. Both read the listing's JSON ``data`` column, so no new
column or index is needed: area counts narrow by the existing ``ix_listings_lat_lng``
bounding box before measuring distance, and the ZIP and neighborhood counts scan the
active listings once. That is fine at the scale of a personal search; if the table
grows large, an indexed ``postal_code`` column is the first thing to add.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.models import Listing
from furnest.db.session import session_scope
from furnest.domain import EARTH_RADIUS, DistanceUnit, haversine
from furnest.domain.places import US_STATES, Place

_ADDRESS = Listing.data["address"]
_CITY = func.lower(_ADDRESS["city"].as_string())
_REGION = func.lower(_ADDRESS["region"].as_string())
_ZIP5 = func.substr(_ADDRESS["postal_code"].as_string(), 1, 5)
_NEIGHBORHOOD = _ADDRESS["neighborhood"].as_string()
_STATE_NAMES = {code: name for name, code in reversed(US_STATES.items())}


@dataclass(frozen=True, slots=True)
class AreaCircle:
    lat: float
    lng: float
    radius: float
    unit: DistanceUnit = DistanceUnit.MILES


@dataclass(frozen=True, slots=True)
class LocationCounts:
    """Active stored listings each kind of criterion matches (a listing can count in several)."""

    areas: tuple[int, ...]
    zip_codes: int
    neighborhoods: int


def _in_city(place: Place) -> ColumnElement[bool]:
    """Listings in the city: by ZIP code, or by city name and a state that agrees."""
    region = {place.state.lower(), _STATE_NAMES.get(place.state, place.state).lower()}
    return or_(
        _ZIP5.in_(place.zip_codes),
        and_(_CITY.in_([place.city.lower()]), _REGION.in_(region)),
    )


def stored_neighborhoods(
    session_factory: sessionmaker[Session], place: Place, limit: int = 30
) -> list[str]:
    """Distinct neighborhoods on active listings in the city, most common first."""
    count = func.count()
    query = (
        select(_NEIGHBORHOOD, count)
        .where(Listing.removed_at.is_(None), _NEIGHBORHOOD.is_not(None), _NEIGHBORHOOD != "")
        .where(_in_city(place))
        .group_by(func.lower(_NEIGHBORHOOD))
        .order_by(count.desc(), func.lower(_NEIGHBORHOOD))
        .limit(limit)
    )
    with session_scope(session_factory) as session:
        return [name for name, _ in session.execute(query) if name]


def _fold(value: str) -> str:
    return " ".join(value.split()).casefold()


def count_matches(
    session_factory: sessionmaker[Session],
    areas: Sequence[AreaCircle],
    zip_codes: Collection[str],
    neighborhoods: Collection[str],
) -> LocationCounts:
    """How many active listings each area, the ZIP list and the neighborhood list match."""
    with session_scope(session_factory) as session:
        return LocationCounts(
            areas=tuple(_count_area(session, area) for area in areas),
            zip_codes=_count_where(session, _ZIP5.in_([z[:5] for z in zip_codes]))
            if zip_codes
            else 0,
            neighborhoods=_count_neighborhoods(session, neighborhoods),
        )


def _count_where(session: Session, clause: ColumnElement[bool]) -> int:
    query = select(func.count()).select_from(Listing)
    return session.scalar(query.where(Listing.removed_at.is_(None), clause)) or 0


def _count_neighborhoods(session: Session, neighborhoods: Collection[str]) -> int:
    wanted = {_fold(n) for n in neighborhoods}
    if not wanted:
        return 0
    names = session.scalars(
        select(_NEIGHBORHOOD).where(Listing.removed_at.is_(None), _NEIGHBORHOOD.is_not(None))
    )
    return sum(1 for name in names if name and _fold(name) in wanted)


def _count_area(session: Session, area: AreaCircle) -> int:
    earth = EARTH_RADIUS[area.unit]
    d_lat = math.degrees(area.radius / earth)
    cos_lat = max(math.cos(math.radians(area.lat)), 1e-6)
    d_lng = min(180.0, math.degrees(area.radius / (earth * cos_lat)))
    query = select(Listing.lat, Listing.lng).where(
        Listing.removed_at.is_(None),
        Listing.lat.between(area.lat - d_lat, area.lat + d_lat),
        Listing.lng.between(area.lng - d_lng, area.lng + d_lng),
    )
    return sum(
        1
        for lat, lng in session.execute(query)
        if lat is not None
        and lng is not None
        and haversine(area.lat, area.lng, lat, lng, area.unit) <= area.radius
    )
