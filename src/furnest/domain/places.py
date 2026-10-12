"""US places by name: "Austin, TX" to a centre point, offline (#179).

The criteria editor lets someone add an area by typing a city and state instead of
coordinates. The data is the ``zipcodes`` package (MIT): every US ZIP code with its
city, state and centroid, bundled in the wheel, so a lookup needs no network and no
geocoder. A city is the set of its ZIP codes, and its centre is the mean of their
centroids. Delivery-area (``STANDARD``) ZIPs are used when a city has any, because a
PO box or single-building ZIP sits at one address; military and retired ZIPs are left out.

Queries are forgiving: case, a missing comma, a full state name, "St." or "Saint".
:func:`find_places` serves the typeahead (exact names first, then prefixes, bigger
cities first) and :func:`resolve_place` answers only when one place fits, which is what
saving a form needs. Anything else should depend on this module, not on ``zipcodes``,
so the dataset can be swapped.

The index is built on first use (about 30k places) and kept for the process.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from functools import cache

import zipcodes

from furnest.domain.geo import Ring, bbox, point_in_polygon

# Postal codes for the states, DC and the territories in the dataset.
US_STATES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "american samoa": "AS",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "federated states of micronesia": "FM",
    "florida": "FL",
    "georgia": "GA",
    "guam": "GU",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "marshall islands": "MH",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "northern mariana islands": "MP",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "palau": "PW",
    "pennsylvania": "PA",
    "puerto rico": "PR",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virgin islands": "VI",
    "virginia": "VA",
    "washington": "WA",
    "washington dc": "DC",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}
_STATE_CODES = frozenset(US_STATES.values())
# The longest state name is four words ("federated states of micronesia").
_MAX_STATE_WORDS = max(len(name.split()) for name in US_STATES)

# Armed Forces "states" (AA, AE, AP) are mail routing, not places.
_SKIPPED_TYPES = frozenset({"MILITARY"})
_PREFERRED_TYPE = "STANDARD"

_PUNCTUATION = re.compile("[.,'\u2019]")
_SEPARATORS = re.compile(r"[\s\-]+")
_ABBREVIATIONS = {"saint": "st", "sainte": "ste", "fort": "ft", "mount": "mt"}


@dataclass(frozen=True)
class Place:
    """A US city or town: its name, state and centre, and the ZIP codes it spans."""

    city: str
    state: str
    lat: float
    lng: float
    zip_codes: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{self.city}, {self.state}"


def _words(text: str) -> list[str]:
    """Lowercase words with punctuation and hyphens dropped: the comparable form."""
    return [w for w in _SEPARATORS.split(_PUNCTUATION.sub(" ", text.lower())) if w]


def _key(words: list[str]) -> str:
    """The lookup key of a city name: "St. Louis", "saint louis" and "St Louis" agree."""
    return " ".join(_ABBREVIATIONS.get(w, w) for w in words)


def _display(city: str) -> str:
    """The dataset's name, with each hyphenated part capitalised ("Winston-Salem")."""
    return "-".join(part[:1].upper() + part[1:] for part in city.split("-"))


def parse_query(text: str) -> tuple[str, str | None]:
    """Split a query into a city key and a state code, if it ends with a state.

    "austin tx", "Austin, TX" and "Austin, Texas" all give ``("austin", "TX")``. A
    query that is only a state name ("new york") is read as a city, since a city with
    that name is the likelier meaning.
    """
    words = _words(text)
    for size in range(min(_MAX_STATE_WORDS, len(words) - 1), 0, -1):
        tail = " ".join(words[-size:])
        code = US_STATES.get(tail) or (tail.upper() if tail.upper() in _STATE_CODES else None)
        if code is not None:
            return _key(words[:-size]), code
    return _key(words), None


@dataclass(frozen=True)
class _Indexed:
    key: str
    place: Place


@cache
def _index() -> tuple[_Indexed, ...]:
    """Every place, biggest (most ZIP codes) first so prefix matches favour them."""
    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    # zipcodes 3 wraps its typed extension in an unannotated function.
    for row in zipcodes.list_all():  # type: ignore[no-untyped-call]
        if not row.get("active") or row.get("zip_code_type") in _SKIPPED_TYPES:
            continue
        groups[(str(row["city"]), str(row["state"]))].append(row)

    places = []
    for (city, state), rows in groups.items():
        centred = [r for r in rows if r.get("zip_code_type") == _PREFERRED_TYPE] or rows
        lat = sum(float(str(r["lat"])) for r in centred) / len(centred)
        lng = sum(float(str(r["long"])) for r in centred) / len(centred)
        zips = tuple(sorted(str(r["zip_code"]) for r in rows))
        place = Place(_display(city), state, round(lat, 4), round(lng, 4), zips)
        places.append(_Indexed(_key(_words(city)), place))
    places.sort(key=lambda p: (-len(p.place.zip_codes), p.place.label))
    return tuple(places)


def find_places(query: str, limit: int = 10) -> list[Place]:
    """Places matching a typed query: exact city names first, then name prefixes."""
    city, state = parse_query(query)
    if len(city) < 2:
        return []
    candidates = [p for p in _index() if state is None or p.place.state == state]
    exact = [p.place for p in candidates if p.key == city]
    prefix = [p.place for p in candidates if p.key != city and p.key.startswith(city)]
    return (exact + prefix)[:limit]


def resolve_place(query: str) -> Place | None:
    """The one place a query names, or None when it names none or several."""
    city, state = parse_query(query)
    if not city:
        return None
    exact = [p.place for p in _index() if p.key == city and state in (None, p.place.state)]
    return exact[0] if len(exact) == 1 else None


def neighborhood_names(place: Place) -> tuple[str, ...]:
    """Other place names the dataset gives the city's ZIP codes: districts, boroughs, suburbs.

    Best effort: these are the postal "acceptable" names (Brooklyn Heights for a
    Brooklyn ZIP), sorted and without the city's own name. Some are neighbouring towns
    or abbreviations, which is why the editor only suggests them.
    """
    own = _key(_words(place.city))
    names: dict[str, str] = {}
    for code in place.zip_codes:
        for row in zipcodes.matching(code):  # type: ignore[no-untyped-call]
            for name in row.get("acceptable_cities") or []:
                key = _key(_words(str(name)))
                if key and key != own:
                    names.setdefault(key, _display(str(name)))
    return tuple(sorted(names.values()))


def places_within(ring: Ring, *, limit: int) -> tuple[list[Place], int]:
    """The places whose centre lies inside a ring, biggest first, and how many didn't fit.

    A linear scan with a bounding-box prefilter: about 30,000 places, fast enough
    for the once-per-run lookup it serves.
    """
    min_lng, min_lat, max_lng, max_lat = bbox(ring)
    inside = [
        p.place
        for p in _index()
        if min_lng <= p.place.lng <= max_lng
        and min_lat <= p.place.lat <= max_lat
        and point_in_polygon(p.place.lng, p.place.lat, ring)
    ]
    return inside[:limit], max(0, len(inside) - limit)
