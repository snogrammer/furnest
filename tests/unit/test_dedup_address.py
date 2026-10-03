"""UsAddressParser: usaddress tagging into normalized components and a canonical string."""

import pytest

from furnest.dedup.address import UsAddressParser
from furnest.domain import Address

PARSER = UsAddressParser()


def parse(line1: str | None = None, **fields: str) -> str:
    return PARSER.parse(Address(line1=line1, **fields)).canonical


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        (
            Address(line1="123 N Main St Apt 4B, Springfield, IL 62701-1234"),
            "123 n main st #4b, springfield, il 62701, us",
        ),
        (
            Address(
                line1="123 north main street #4b",
                city="Springfield",
                region="Illinois",
                postal_code="62701",
            ),
            "123 n main st #4b, springfield, il 62701, us",
        ),
        (
            Address(
                line1="123  N. Main Street",
                line2="Unit 4B",
                city="springfield",
                region="il",
                postal_code="62701",
            ),
            "123 n main st #4b, springfield, il 62701, us",
        ),
        (
            Address(line1="1600 Elm St NW Suite 200 Springfield IL 62701"),
            "1600 elm st nw #200, springfield, il 62701, us",
        ),
        (
            Address(line1="42 Example Avenue"),
            "42 example ave, us",
        ),
        (
            Address(line1="PO Box 55, Springfield, Illinois 62701"),
            "po box 55, springfield, il 62701, us",
        ),
    ],
)
def test_representative_us_addresses(address: Address, expected: str) -> None:
    parsed = PARSER.parse(address)
    assert parsed.parsed is True
    assert parsed.canonical == expected


def test_components_are_split_out() -> None:
    parsed = PARSER.parse(Address(line1="123 N Main St Apt 4B, Springfield, IL 62701-1234"))
    assert parsed.house_number == "123"
    assert parsed.street == "n main st"
    assert parsed.unit == "#4b"
    assert parsed.city == "springfield"
    assert parsed.region == "IL"
    assert parsed.postal_code == "62701"
    assert parsed.postal_code_ext == "1234"
    assert parsed.country == "US"


def test_unit_designator_variants_share_a_canonical_form() -> None:
    forms = {
        parse("123 Main St Apt 4B", city="Springfield", region="IL"),
        parse("123 Main St #4B", city="Springfield", region="IL"),
        parse("123 Main Street Unit 4b", city="Springfield", region="IL"),
    }
    assert forms == {"123 main st #4b, springfield, il, us"}


def test_explicit_components_win_over_tagged_ones() -> None:
    parsed = PARSER.parse(
        Address(
            line1="123 Main St, Shelbyville, IL 62565",
            city="Springfield",
            postal_code="62701",
            neighborhood="Old Town",
        )
    )
    # Joined text holds both cities, so usaddress may give up; either way the
    # source's own city and ZIP survive.
    assert parsed.city == "springfield"
    assert parsed.postal_code == "62701"
    assert parsed.neighborhood == "old town"


def test_explicit_region_and_zip_override_tags() -> None:
    parsed = PARSER.parse(
        Address(line1="9 Elm St", city="Springfield", region="Illinois", postal_code="62701")
    )
    assert parsed.parsed is True
    assert parsed.region == "IL"
    assert parsed.postal_code == "62701"


@pytest.mark.parametrize(
    "line1",
    [
        "123 Main St, 456 Oak Ave, Springfield IL",  # repeated street labels
        "Main St & Oak Ave, Springfield IL",  # an intersection, not a door
        "asdf",  # nothing address-like
    ],
)
def test_unparseable_input_falls_back(line1: str) -> None:
    parsed = PARSER.parse(Address(line1=line1))
    assert parsed.parsed is False
    assert parsed.street is not None


def test_empty_address_falls_back() -> None:
    parsed = PARSER.parse(Address())
    assert parsed.parsed is False
    assert parsed.canonical == "us"


def test_non_us_country_uses_passthrough() -> None:
    parsed = PARSER.parse(Address(line1="10 Example Road", postal_code="ab1 2cd", country="GB"))
    assert parsed.parsed is False
    assert parsed.canonical == "10 example road, ab1 2cd, gb"


def test_street_address_without_street_or_city_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # usaddress normally calls such input "Ambiguous"; guard the tagger anyway.
    monkeypatch.setattr(
        "furnest.dedup.address.usaddress.tag",
        lambda _text: ({"AddressNumber": "123"}, "Street Address"),
    )
    parsed = PARSER.parse(Address(line1="123"))
    assert parsed.parsed is False
    assert parsed.canonical == "123, us"
