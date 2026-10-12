"""Great-circle distance between two points, and points inside drawn outlines.

Location matching (#13) uses :func:`haversine` for the radius around each criteria
area, and dedup (P7) uses it for geo proximity. Haversine on a spherical Earth is
accurate to about 0.5%, which is plenty for search radii measured in miles.

Outlines drawn on the dashboard map (#214) are polygons: one closed ring of
``(lng, lat)`` positions, GeoJSON order. :func:`point_in_polygon` treats the degrees
as flat x/y. That is wrong near the poles and across the antimeridian, but an
outline around a few towns is small enough for the error not to matter.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from enum import StrEnum
from itertools import pairwise

# A closed ring of (lng, lat) positions: the first position repeats as the last.
Ring = Sequence[tuple[float, float]]


class DistanceUnit(StrEnum):
    MILES = "mi"
    KILOMETERS = "km"


# IUGG mean Earth radius.
EARTH_RADIUS: dict[DistanceUnit, float] = {
    DistanceUnit.MILES: 3958.7613,
    DistanceUnit.KILOMETERS: 6371.0088,
}


def haversine(
    lat1: float,
    lng1: float,
    lat2: float,
    lng2: float,
    unit: DistanceUnit = DistanceUnit.MILES,
) -> float:
    """Distance between two (lat, lng) points given in degrees."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    # Rounding can push `a` a hair outside [0, 1] near antipodes; asin would then fail.
    a = min(1.0, max(0.0, a))
    return 2 * EARTH_RADIUS[unit] * math.asin(math.sqrt(a))


def bbox(ring: Ring) -> tuple[float, float, float, float]:
    """``(min_lng, min_lat, max_lng, max_lat)`` of a ring."""
    lngs = [lng for lng, _ in ring]
    lats = [lat for _, lat in ring]
    return min(lngs), min(lats), max(lngs), max(lats)


def ring_area(ring: Ring) -> float:
    """Unsigned area in square degrees (shoelace); zero for a ring with no inside."""
    twice = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in pairwise(ring))
    return abs(twice) / 2


def point_in_polygon(lng: float, lat: float, ring: Ring) -> bool:
    """Whether a point is inside a ring or on its boundary.

    Ray casting: a ray from the point crosses the boundary an odd number of times
    exactly when the point is inside. Points on an edge or a vertex count as inside,
    checked first because the crossing count is ambiguous there.
    """
    min_lng, min_lat, max_lng, max_lat = bbox(ring)
    if not (min_lng <= lng <= max_lng and min_lat <= lat <= max_lat):
        return False
    inside = False
    for (x1, y1), (x2, y2) in pairwise(ring):
        cross = (x2 - x1) * (lat - y1) - (y2 - y1) * (lng - x1)
        if (
            math.isclose(cross, 0.0, abs_tol=1e-12)
            and min(x1, x2) <= lng <= max(x1, x2)
            and min(y1, y2) <= lat <= max(y1, y2)
        ):
            return True
        if (y1 > lat) != (y2 > lat) and lng < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside
