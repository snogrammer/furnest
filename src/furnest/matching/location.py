"""Location matching: is a listing inside the searcher's areas, outlines, ZIPs or neighborhoods?

A listing qualifies when it matches **any** configured location criterion: within
the radius of an area, inside an outline drawn on the map (#214), in a listed ZIP
code, or in a listed neighborhood. Each
criterion is evaluated only when the listing has the data for it. Missing
coordinates fall back to ZIP and neighborhood, and the result says so.

The result has four outcomes. ``MATCH`` means something matched.
``NO_MATCH`` means every configured criterion was checked and none matched.
``UNKNOWN`` means nothing matched but at least one criterion could not be checked
for missing data, so the listing might still be in range. ``NOT_CONFIGURED`` means
the criteria set no location constraints. The hard filters (#16) decide how to
treat ``UNKNOWN``. Every result carries human-readable reasons for the match
breakdown.

Pure: no I/O and no DB. The address parser is passed in, so matching works with
any locale's parser, or none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from furnest.domain import AddressParser, Criteria, DistanceUnit, NormalizedListing
from furnest.domain.address import normalize_zip
from furnest.domain.geo import EARTH_RADIUS, haversine, point_in_polygon

_SPACE = re.compile(r"\s+")


class LocationStatus(StrEnum):
    MATCH = "match"
    NO_MATCH = "no_match"
    UNKNOWN = "unknown"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True, slots=True)
class LocationResult:
    status: LocationStatus
    reasons: tuple[str, ...]
    # What matched first: "area:<name>", "zip:<zip>" or "neighborhood:<name>".
    matched_by: str | None = None
    # Distance to the nearest area's centre, in that area's unit.
    nearest_distance: float | None = None
    nearest_unit: DistanceUnit | None = None


def _fold(value: str) -> str:
    return _SPACE.sub(" ", value).strip().casefold()


def _fmt(value: float) -> str:
    return f"{value:.1f}".removesuffix(".0")


def match_location(
    listing: NormalizedListing,
    criteria: Criteria,
    *,
    parser: AddressParser | None = None,
) -> LocationResult:
    if not (criteria.areas or criteria.outlines or criteria.zip_codes or criteria.neighborhoods):
        return LocationResult(LocationStatus.NOT_CONFIGURED, ("no location criteria",))

    parsed = parser.parse(listing.address) if parser else None
    country = listing.address.country
    reasons: list[str] = []
    matches: list[str] = []
    unknown = False
    nearest: tuple[float, DistanceUnit] | None = None
    nearest_km = 0.0

    if criteria.areas or criteria.outlines:
        if listing.lat is None or listing.lng is None:
            unknown = True
            reasons.append("location unknown: no coordinates")
        else:
            for outline in criteria.outlines:
                inside = point_in_polygon(listing.lng, listing.lat, outline.polygon)
                reasons.append(f"{'inside' if inside else 'outside'} {outline.name}")
                if inside:
                    matches.append(f"outline:{outline.name}")
            for area in criteria.areas:
                distance = haversine(listing.lat, listing.lng, area.lat, area.lng, area.unit)
                # Areas may mix units, so rank them by their distance in kilometres.
                in_km = distance * EARTH_RADIUS[DistanceUnit.KILOMETERS] / EARTH_RADIUS[area.unit]
                if nearest is None or in_km < nearest_km:
                    nearest, nearest_km = (distance, area.unit), in_km
                inside = distance <= area.radius
                reasons.append(
                    f"{_fmt(distance)} {area.unit} from {area.name} "
                    f"({'within' if inside else 'outside'} radius {_fmt(area.radius)} {area.unit})"
                )
                if inside:
                    matches.append(f"area:{area.name}")

    if criteria.zip_codes:
        raw_zip = listing.address.postal_code or (parsed.postal_code if parsed else None)
        listing_zip = normalize_zip(raw_zip, country)
        if listing_zip is None:
            unknown = True
            reasons.append("location unknown: no ZIP code")
        else:
            wanted = {normalize_zip(z, country) for z in criteria.zip_codes}
            if listing_zip in wanted:
                matches.append(f"zip:{listing_zip}")
                reasons.append(f"ZIP {listing_zip} is in the list")
            else:
                reasons.append(f"ZIP {listing_zip} is not in the list")

    if criteria.neighborhoods:
        raw_hood = listing.address.neighborhood or (parsed.neighborhood if parsed else None)
        if raw_hood is None or not raw_hood.strip():
            unknown = True
            reasons.append("location unknown: no neighborhood")
        else:
            hood = _fold(raw_hood)
            if hood in {_fold(n) for n in criteria.neighborhoods}:
                matches.append(f"neighborhood:{hood}")
                reasons.append(f"neighborhood {hood!r} is in the list")
            else:
                reasons.append(f"neighborhood {hood!r} is not in the list")

    if matches:
        status = LocationStatus.MATCH
    elif unknown:
        status = LocationStatus.UNKNOWN
    else:
        status = LocationStatus.NO_MATCH
    return LocationResult(
        status=status,
        reasons=tuple(reasons),
        matched_by=matches[0] if matches else None,
        nearest_distance=nearest[0] if nearest else None,
        nearest_unit=nearest[1] if nearest else None,
    )
