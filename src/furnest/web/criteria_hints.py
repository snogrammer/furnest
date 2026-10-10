"""Location helpers beside the criteria editor's ZIP and neighborhood lists (#180).

For each area that names a looked-up city the form offers its ZIP codes and
neighborhood suggestions, and shows how many stored listings each location criterion
catches. Everything is computed from the form as it stands (typed, not yet saved), so
the numbers and chips follow the edit.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from furnest.db.location_stats import (
    AreaCircle,
    LocationCounts,
    count_matches,
    stored_neighborhoods,
)
from furnest.domain import DistanceUnit, Place
from furnest.web.criteria_form import FormState, merge_lines, place_of, suggested_neighborhoods

MAX_SUGGESTIONS = 30


@dataclass(frozen=True)
class AreaHint:
    index: int
    place: Place
    new_zips: int
    neighborhoods: list[str]


@dataclass(frozen=True)
class LocationHints:
    areas: list[AreaHint]
    # (name, listings in its radius) for each area with a usable circle.
    area_counts: list[tuple[str, int]]
    counts: LocationCounts | None


def _circle(row: dict[str, str]) -> AreaCircle | None:
    try:
        return AreaCircle(
            float(row["lat"]),
            float(row["lng"]),
            float(row["radius"]),
            DistanceUnit(row.get("unit") or DistanceUnit.MILES),
        )
    except (KeyError, ValueError):
        return None


def build_hints(session_factory: sessionmaker[Session], state: FormState) -> LocationHints:
    zips = [z.strip() for z in state.value("zip_codes").replace(",", "\n").splitlines()]
    hoods = [n.strip() for n in state.value("neighborhoods").replace(",", "\n").splitlines()]
    zips, hoods = [z for z in zips if z], [n for n in hoods if n]

    circles = [_circle(row) for row in state.areas]
    valid = [c for c in circles if c is not None]
    counts = count_matches(session_factory, valid, zips, hoods) if state.areas else None
    per_area = iter(counts.areas) if counts else iter(())

    areas = []
    area_counts = []
    for i, row in enumerate(state.areas):
        if circles[i] is not None:
            area_counts.append((row.get("name") or f"area {i + 1}", next(per_area)))
        place = place_of(row)
        if place is None:
            continue
        stored = stored_neighborhoods(session_factory, place)
        areas.append(
            AreaHint(
                index=i,
                place=place,
                new_zips=merge_lines(state.value("zip_codes"), place.zip_codes)[1],
                neighborhoods=suggested_neighborhoods(place, stored, state.value("neighborhoods"))[
                    :MAX_SUGGESTIONS
                ],
            )
        )
    return LocationHints(areas, area_counts, counts)
