"""Property type aliases from source labels to the PropertyType enum."""

import pytest

from furnest.domain import PropertyType, normalize_property_type


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("apartment", PropertyType.APARTMENT),
        ("Apt", PropertyType.APARTMENT),
        ("flat", PropertyType.APARTMENT),
        ("Townhome", PropertyType.TOWNHOUSE),
        ("town-house", PropertyType.TOWNHOUSE),
        ("single_family", PropertyType.SINGLE_FAMILY),
        ("Single-Family Home", PropertyType.SINGLE_FAMILY),
        ("  HOUSE ", PropertyType.SINGLE_FAMILY),
        ("SFH", PropertyType.SINGLE_FAMILY),
        ("Condominium", PropertyType.CONDO),
        ("co-op", PropertyType.CONDO),
        ("Private Room", PropertyType.ROOM),
        ("shared room", PropertyType.ROOM),
        ("Studio", PropertyType.STUDIO),
        ("efficiency", PropertyType.STUDIO),
        ("other", PropertyType.OTHER),
        ("houseboat", PropertyType.OTHER),
        ("", PropertyType.OTHER),
        (None, PropertyType.OTHER),
        (PropertyType.CONDO, PropertyType.CONDO),
    ],
)
def test_normalize_property_type(raw: str | PropertyType | None, expected: PropertyType) -> None:
    assert normalize_property_type(raw) is expected


def test_every_enum_value_maps_to_itself() -> None:
    for member in PropertyType:
        assert normalize_property_type(member.value) is member
