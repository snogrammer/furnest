"""US place lookup: forgiving queries, typeahead order, and unambiguous resolution.

Tests use Springfield, IL (the hygiene placeholder area) as the known place.
"""

import pytest

from furnest.domain import haversine
from furnest.domain.places import find_places, parse_query, places_within, resolve_place

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "query", ["springfield il", "Springfield, IL", "SPRINGFIELD illinois", "  springfield ,  il "]
)
def test_city_and_state_resolve(query: str) -> None:
    place = resolve_place(query)
    assert place is not None
    assert place.label == "Springfield, IL"
    assert place.zip_codes
    # The centre is the mean of the city's ZIP centroids: near the town centre.
    assert haversine(place.lat, place.lng, 39.8, -89.65) < 10


@pytest.mark.parametrize(
    ("query", "parsed"),
    [
        ("austin tx", ("austin", "TX")),
        ("Austin, Texas", ("austin", "TX")),
        ("st. louis mo", ("st louis", "MO")),
        ("Saint Louis, Missouri", ("st louis", "MO")),
        ("charleston west virginia", ("charleston", "WV")),
        ("seattle washington", ("seattle", "WA")),
        ("washington dc", ("washington", "DC")),
        ("new york", ("new york", None)),
        ("winston-salem", ("winston salem", None)),
    ],
)
def test_parse_query(query: str, parsed: tuple[str, str | None]) -> None:
    assert parse_query(query) == parsed


def test_city_without_a_state_is_ambiguous_when_several_share_it() -> None:
    matches = find_places("springfield")
    states = {place.state for place in matches}
    assert len(states) > 1
    assert "IL" in states
    assert resolve_place("springfield") is None


def test_prefixes_suggest_places_and_respect_the_limit() -> None:
    matches = find_places("springf", limit=3)
    assert len(matches) == 3
    assert all(place.city.startswith("Springf") for place in matches)
    assert resolve_place("springf") is None


def test_exact_names_come_before_prefixes() -> None:
    labels = [place.label for place in find_places("springfield il")]
    assert labels[0] == "Springfield, IL"


@pytest.mark.parametrize("query", ["", "s", "   ", "Nowhereville QQ", "zzzz"])
def test_nothing_matches(query: str) -> None:
    assert find_places(query) == []
    assert resolve_place(query) is None


def test_neighborhood_names_are_other_names_for_the_citys_zips() -> None:
    from furnest.domain.places import neighborhood_names

    place = resolve_place("Brooklyn, NY")
    assert place is not None
    names = neighborhood_names(place)
    assert "Brooklyn Heights" in names
    assert "Brooklyn" not in names
    assert list(names) == sorted(names)


def test_places_within_an_outline() -> None:
    box = [(-89.8, 39.7), (-89.5, 39.7), (-89.5, 39.9), (-89.8, 39.9), (-89.8, 39.7)]

    places, dropped = places_within(box, limit=50)

    assert "Springfield, IL" in [p.label for p in places]
    assert dropped == 0
    assert all(-89.8 <= p.lng <= -89.5 and 39.7 <= p.lat <= 39.9 for p in places)


def test_places_within_keeps_the_biggest() -> None:
    box = [(-91.0, 37.0), (-87.5, 37.0), (-87.5, 42.0), (-91.0, 42.0), (-91.0, 37.0)]

    places, dropped = places_within(box, limit=3)

    assert len(places) == 3
    assert dropped > 100
    sizes = [len(p.zip_codes) for p in places]
    assert sizes == sorted(sizes, reverse=True)
