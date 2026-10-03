"""Haversine distances in miles and kilometres."""

import math

import pytest

from furnest.domain import EARTH_RADIUS, DistanceUnit, haversine


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
