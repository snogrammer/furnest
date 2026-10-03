"""match_location: areas, ZIPs and neighborhoods, with unknown-data fallbacks."""

from typing import Any

import pytest

from furnest.dedup.address import UsAddressParser
from furnest.domain import Criteria, DistanceUnit, NormalizedListing, haversine
from furnest.matching.location import LocationStatus, match_location

# A placeholder centre; one degree of latitude is ~69.09 mi / ~111.19 km.
CENTRE = (10.0, 20.0)


def listing(**fields: object) -> NormalizedListing:
    return NormalizedListing.model_validate(
        {
            "source": "test",
            "source_id": "1",
            "url": "https://example.com/1",
            "title": "Furnished 1BR",
            **fields,
        }
    )


def area(
    name: str = "Centre", radius: float = 5, unit: str = "mi", **centre: float
) -> dict[str, Any]:
    lat, lng = centre.get("lat", CENTRE[0]), centre.get("lng", CENTRE[1])
    return {"name": name, "lat": lat, "lng": lng, "radius": radius, "unit": unit}


def criteria(**fields: object) -> Criteria:
    return Criteria.model_validate(fields)


def north_of_centre(miles: float) -> dict[str, float]:
    return {"lat": CENTRE[0] + miles / haversine(0, 0, 1, 0), "lng": CENTRE[1]}


def test_no_location_criteria() -> None:
    result = match_location(listing(), Criteria())
    assert result.status is LocationStatus.NOT_CONFIGURED
    assert result.reasons == ("no location criteria",)


def test_inside_radius() -> None:
    result = match_location(listing(**north_of_centre(2)), criteria(areas=[area()]))
    assert result.status is LocationStatus.MATCH
    assert result.matched_by == "area:Centre"
    assert result.nearest_distance == pytest.approx(2)
    assert result.nearest_unit is DistanceUnit.MILES
    assert result.reasons == ("2 mi from Centre (within radius 5 mi)",)


def test_outside_radius() -> None:
    result = match_location(listing(**north_of_centre(7.3)), criteria(areas=[area()]))
    assert result.status is LocationStatus.NO_MATCH
    assert result.matched_by is None
    assert result.reasons == ("7.3 mi from Centre (outside radius 5 mi)",)


def test_on_the_radius_counts_as_inside() -> None:
    lst = listing(**north_of_centre(5))
    radius = haversine(lst.lat or 0, lst.lng or 0, *CENTRE)
    result = match_location(lst, criteria(areas=[area(radius=radius)]))
    assert result.status is LocationStatus.MATCH


def test_kilometre_area() -> None:
    result = match_location(
        listing(**north_of_centre(5)), criteria(areas=[area(radius=10, unit="km")])
    )
    assert result.status is LocationStatus.MATCH
    assert result.nearest_unit is DistanceUnit.KILOMETERS
    assert result.nearest_distance == pytest.approx(8.05, abs=0.01)


def test_nearest_area_is_reported_across_units() -> None:
    far = area("Far", radius=1, lat=CENTRE[0] + 1)
    near = area("Near", radius=1, unit="km")
    for areas in ([far, near], [near, far]):
        result = match_location(listing(**north_of_centre(3)), criteria(areas=areas))
        assert result.status is LocationStatus.NO_MATCH
        assert result.nearest_unit is DistanceUnit.KILOMETERS
        assert result.nearest_distance == pytest.approx(4.83, abs=0.01)


def test_any_of_several_areas_matches() -> None:
    result = match_location(
        listing(**north_of_centre(3)),
        criteria(areas=[area("Far", lat=CENTRE[0] - 1), area("Near")]),
    )
    assert result.status is LocationStatus.MATCH
    assert result.matched_by == "area:Near"
    assert len(result.reasons) == 2


@pytest.mark.parametrize("postal_code", ["62701", "62701-1234", " 62701 "])
def test_zip_match(postal_code: str) -> None:
    result = match_location(
        listing(address={"postal_code": postal_code}), criteria(zip_codes=["62701"])
    )
    assert result.status is LocationStatus.MATCH
    assert result.matched_by == "zip:62701"


def test_criteria_zip_plus4_is_normalized_too() -> None:
    result = match_location(
        listing(address={"postal_code": "62701"}), criteria(zip_codes=["62701-0001"])
    )
    assert result.status is LocationStatus.MATCH


def test_zip_not_in_list() -> None:
    result = match_location(
        listing(address={"postal_code": "62702"}), criteria(zip_codes=["62701"])
    )
    assert result.status is LocationStatus.NO_MATCH
    assert result.reasons == ("ZIP 62702 is not in the list",)


def test_zip_comes_from_the_parser_when_missing() -> None:
    lst = listing(address={"line1": "123 Main St, Springfield, IL 62701"})
    crit = criteria(zip_codes=["62701"])
    assert match_location(lst, crit).status is LocationStatus.UNKNOWN
    assert match_location(lst, crit, parser=UsAddressParser()).status is LocationStatus.MATCH


@pytest.mark.parametrize("hood", ["Old Town", "old town", "  OLD   town ", "Old\tTown"])
def test_neighborhood_is_case_and_whitespace_insensitive(hood: str) -> None:
    result = match_location(
        listing(address={"neighborhood": hood}), criteria(neighborhoods=["Old  Town"])
    )
    assert result.status is LocationStatus.MATCH
    assert result.matched_by == "neighborhood:old town"


def test_neighborhood_not_in_list() -> None:
    result = match_location(
        listing(address={"neighborhood": "Riverside"}), criteria(neighborhoods=["old town"])
    )
    assert result.status is LocationStatus.NO_MATCH
    assert result.reasons == ("neighborhood 'riverside' is not in the list",)


def test_missing_coordinates_fall_back_to_zip() -> None:
    result = match_location(
        listing(address={"postal_code": "62701"}),
        criteria(areas=[area()], zip_codes=["62701"]),
    )
    assert result.status is LocationStatus.MATCH
    assert result.matched_by == "zip:62701"
    assert "location unknown: no coordinates" in result.reasons


def test_missing_coordinates_with_only_areas_is_unknown() -> None:
    result = match_location(listing(), criteria(areas=[area()]))
    assert result.status is LocationStatus.UNKNOWN
    assert result.reasons == ("location unknown: no coordinates",)
    assert result.nearest_distance is None


def test_unknown_when_a_failing_check_leaves_another_unevaluated() -> None:
    result = match_location(
        listing(address={"postal_code": "62702"}),
        criteria(areas=[area()], zip_codes=["62701"]),
    )
    assert result.status is LocationStatus.UNKNOWN


def test_missing_zip_and_neighborhood_are_unknown() -> None:
    result = match_location(listing(), criteria(zip_codes=["62701"], neighborhoods=["old town"]))
    assert result.status is LocationStatus.UNKNOWN
    assert result.reasons == ("location unknown: no ZIP code", "location unknown: no neighborhood")


def test_no_match_when_everything_was_checked() -> None:
    result = match_location(
        listing(
            address={"postal_code": "62702", "neighborhood": "riverside"}, **north_of_centre(50)
        ),
        criteria(areas=[area()], zip_codes=["62701"], neighborhoods=["old town"]),
    )
    assert result.status is LocationStatus.NO_MATCH
    assert len(result.reasons) == 3


def test_all_matches_are_explained_first_one_reported() -> None:
    result = match_location(
        listing(address={"postal_code": "62701", "neighborhood": "old town"}, **north_of_centre(1)),
        criteria(areas=[area()], zip_codes=["62701"], neighborhoods=["old town"]),
    )
    assert result.status is LocationStatus.MATCH
    assert result.matched_by == "area:Centre"
    assert result.reasons[1:] == (
        "ZIP 62701 is in the list",
        "neighborhood 'old town' is in the list",
    )
