"""Great-circle distance between two points, in miles or kilometres.

Location matching (#13) uses it for the radius around each criteria area, and
dedup (P7) uses it for geo proximity. Haversine on a spherical Earth is accurate
to about 0.5%, which is plenty for search radii measured in miles.
"""

from __future__ import annotations

import math
from enum import StrEnum


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
