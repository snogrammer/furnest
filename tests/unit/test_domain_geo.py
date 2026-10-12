"""Haversine distances in miles and kilometres, and points inside outlines."""

import math

import pytest

from furnest.domain import EARTH_RADIUS, DistanceUnit, haversine
from furnest.domain.geo import bbox, point_in_polygon, ring_area


def test_zero_distance() -> None:
    assert haversine(10.0, 20.0, 10.0, 20.0) == 0.0


def test_one_degree_of_longitude_at_the_equator() -> None:
    assert haversine(0, 0, 0, 1) == pytest.approx(69.09, abs=0.01)
    assert haversine(0, 0, 0, 1, DistanceUnit.KILOMETERS) == pytest.approx(111.19, abs=0.01)


def test_is_symmetric() -> None:
    assert haversine(12.5, -45.0, -3.25, 60.0) == pytest.approx(haversine(-3.25, 60.0, 12.5, -45.0))


def test_crosses_the_antimeridian_the_short_way() -> None:
    assert haversine(0, 179.5, 0, -179.5) == pytest.approx(haversine(0, 0, 0, 1))


def test_antipodes_are_half_the_circumference_without_nan() -> None:
    distance = haversine(0, 0, 0, 180, DistanceUnit.KILOMETERS)
    assert not math.isnan(distance)
    assert distance == pytest.approx(math.pi * EARTH_RADIUS[DistanceUnit.KILOMETERS])


def test_units_agree() -> None:
    miles = haversine(40, -100, 41, -101, DistanceUnit.MILES)
    km = haversine(40, -100, 41, -101, DistanceUnit.KILOMETERS)
    assert km / miles == pytest.approx(1.609, abs=0.001)


def test_distance_unit_values_are_stable() -> None:
    # Stored criteria versions hold these strings.
    assert [u.value for u in DistanceUnit] == ["mi", "km"]


# --- Outlines: point in polygon (#214) ----------------------------------------

SQUARE = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0), (0.0, 0.0)]
# A "C" open to the east: the notch (x 2..4, y 1..3) is outside.
C_SHAPE = [
    (0.0, 0.0),
    (4.0, 0.0),
    (4.0, 1.0),
    (2.0, 1.0),
    (2.0, 3.0),
    (4.0, 3.0),
    (4.0, 4.0),
    (0.0, 4.0),
    (0.0, 0.0),
]


@pytest.mark.parametrize(
    ("point", "inside"),
    [
        ((2.0, 2.0), True),
        ((5.0, 2.0), False),
        ((-1.0, 2.0), False),
        ((2.0, 0.0), True),  # on an edge
        ((4.0, 2.5), True),  # on a vertical edge
        ((4.0, 4.0), True),  # on a vertex
        ((0.0, 0.0), True),  # on the closing vertex
    ],
)
def test_point_in_square(point: tuple[float, float], inside: bool) -> None:
    assert point_in_polygon(*point, SQUARE) is inside


@pytest.mark.parametrize(
    ("point", "inside"),
    [((1.0, 2.0), True), ((3.0, 2.0), False), ((3.0, 0.5), True), ((3.0, 3.5), True)],
)
def test_point_in_concave_polygon(point: tuple[float, float], inside: bool) -> None:
    assert point_in_polygon(*point, C_SHAPE) is inside


def test_ray_through_a_vertex_is_counted_once() -> None:
    diamond = [(0.0, -2.0), (2.0, 0.0), (0.0, 2.0), (-2.0, 0.0), (0.0, -2.0)]
    # The ray from (-1, 0) runs along y = 0 through the vertex at (2, 0).
    assert point_in_polygon(-1.0, 0.0, diamond)
    assert not point_in_polygon(-3.0, 0.0, diamond)


def test_bbox_and_area() -> None:
    assert bbox(C_SHAPE) == (0.0, 0.0, 4.0, 4.0)
    assert ring_area(SQUARE) == 16.0
    assert ring_area(C_SHAPE) == 12.0
    assert ring_area([(0.0, 0.0), (1.0, 1.0), (2.0, 2.0), (0.0, 0.0)]) == 0.0
