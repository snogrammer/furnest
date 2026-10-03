"""ParsedAddress, ZIP/region normalization and the passthrough parser."""

import pytest

from furnest.domain import Address, ParsedAddress, PassthroughAddressParser, normalize_zip
from furnest.domain.address import (
    clean,
    normalize_directional,
    normalize_region,
    normalize_suffix,
    normalize_unit,
    split_zip,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("62701", "62701"),
        (" 62701 ", "62701"),
        ("62701-1234", "62701"),
        ("627011234", "62701"),
        ("62701 1234", "62701"),
        ("", None),
        ("   ", None),
        (None, None),
        ("6270", "6270"),
    ],
)
def test_normalize_zip_us(value: str | None, expected: str | None) -> None:
    assert normalize_zip(value) == expected


def test_split_zip_keeps_the_plus4() -> None:
    assert split_zip("62701-1234") == ("62701", "1234")
    assert split_zip("62701") == ("62701", None)


def test_non_us_postal_codes_are_only_cleaned() -> None:
    assert normalize_zip(" ab1 2cd ", "GB") == "AB1 2CD"
    assert split_zip("12345-6789", "DE") == ("12345-6789", None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Illinois", "IL"), ("il", "IL"), ("New  York", "NY"), ("District of Columbia", "DC")],
)
def test_normalize_region_us(value: str, expected: str) -> None:
    assert normalize_region(value) == expected


def test_normalize_region_non_us_and_empty() -> None:
    assert normalize_region("ontario", "CA") == "ONTARIO"
    assert normalize_region("  ") is None
    assert normalize_region(None) is None


def test_component_normalizers() -> None:
    assert clean("  Main.  Street, ") == "main street"
    assert clean(" . ") is None
    assert normalize_directional("North") == "n"
    assert normalize_directional("NW") == "nw"
    assert normalize_directional(None) is None
    assert normalize_suffix("Avenue") == "ave"
    assert normalize_suffix("St.") == "st"
    assert normalize_suffix("Bend") == "bend"
    assert normalize_suffix("") is None


@pytest.mark.parametrize(
    ("designator", "identifier", "expected"),
    [
        ("Apt", "4B", "#4b"),
        ("Unit", "4b", "#4b"),
        (None, "# 4B", "#4b"),
        ("Suite", "200", "#200"),
        ("Basement", None, "basement"),
        (None, None, None),
    ],
)
def test_normalize_unit(designator: str | None, identifier: str | None, expected: str) -> None:
    assert normalize_unit(designator, identifier) == expected


def test_canonical_street_address() -> None:
    parsed = ParsedAddress(
        house_number="123",
        street="n main st",
        unit="#4b",
        city="springfield",
        region="IL",
        postal_code="62701",
        parsed=True,
    )
    assert parsed.canonical == "123 n main st #4b, springfield, il 62701, us"


def test_canonical_po_box_and_sparse() -> None:
    assert ParsedAddress(po_box="55", region="IL").canonical == "po box 55, il, us"
    assert ParsedAddress().canonical == "us"


def test_passthrough_cleans_components() -> None:
    parsed = PassthroughAddressParser().parse(
        Address(
            line1="  10 Example  Road ",
            line2="Flat 2",
            city="Exampleton",
            region="Example County",
            postal_code="ab1 2cd",
            country="gb",
            neighborhood=" Old  Town ",
        )
    )
    assert parsed.parsed is False
    assert parsed.street == "10 example road"
    assert parsed.unit == "flat 2"
    assert parsed.region == "EXAMPLE COUNTY"
    assert parsed.postal_code == "AB1 2CD"
    assert parsed.neighborhood == "old town"
    assert parsed.canonical == "10 example road flat 2, exampleton, example county ab1 2cd, gb"


def test_passthrough_is_stable_across_spelling_noise() -> None:
    parser = PassthroughAddressParser()
    a = parser.parse(Address(line1="10 Example Rd.", city="Springfield", postal_code="62701"))
    b = parser.parse(Address(line1="10  example rd", city="SPRINGFIELD", postal_code="62701-0001"))
    assert a.canonical == b.canonical
