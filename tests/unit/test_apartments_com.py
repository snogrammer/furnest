"""The Apartments.com adapter: criteria → actor input, actor items → listings, and a
whole search against a faked Apify. Runs offline on tests/fixtures/apartments_com."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from furnest.config import AdapterOverrides, SourcesSettings
from furnest.domain import Amenity, Criteria, Money, OwnerType, Price, PropertyType
from furnest.matching.amenities import KeywordAmenityExtractor
from furnest.sources import SourceError, discover, resolve_config
from furnest.sources.adapters.apartments_com import ApartmentsCom
from furnest.sources.adapters.apartments_com.parse import (
    NotFurnishedError,
    build_input,
    parse_item,
)
from furnest.sources.apify import APIFY_API

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "apartments_com"
TODAY = date(2026, 10, 10)
TOKEN = "apify-test-token"  # noqa: S105 - a fake


def fixture(name: str) -> Any:  # noqa: ANN401 - arbitrary JSON
    return json.loads((FIXTURES / name).read_text())


def items() -> list[dict[str, Any]]:
    """Live rows (#194): a furnished building with units, a furnished building with
    floor plans only, and an unfurnished building."""
    return fixture("dataset_live.json")  # type: ignore[no-any-return]


def usd(amount: int) -> Money:
    return Money.from_major(amount, "USD")


def units(**fields: Any) -> list[Any]:  # noqa: ANN401 - NormalizedListings
    return parse_item({**items()[0], **fields}, today=TODAY)


def building(**fields: Any) -> Any:  # noqa: ANN401 - a NormalizedListing
    [listing] = parse_item({**items()[1], **fields}, today=TODAY)
    return listing


def unit(**fields: Any) -> Any:  # noqa: ANN401 - a NormalizedListing
    """The first unit of the first building, with ``fields`` set on the unit."""
    item = items()[0]
    item["rentals"] = [{**item["rentals"][0], **fields}]
    [listing] = parse_item(item, today=TODAY)
    return listing


# --- parse_item ----------------------------------------------------------------------


def test_each_available_unit_of_a_furnished_building_is_a_listing() -> None:
    studio, one_bed, three_bed = units()

    assert [studio.source_id, one_bed.source_id, three_bed.source_id] == [
        "apt0001-apt0001u1",
        "apt0001-apt0001u2",
        "apt0001-apt0001u3",
    ]
    assert {str(listing.url) for listing in (studio, one_bed, three_bed)} == {
        "https://www.apartments.com/example-heights-denver-co/apt0001/"
    }
    assert studio.title == "Example Heights, unit 001"
    assert studio.price == Price(amount=usd(1687))
    assert (studio.beds, studio.baths, studio.sqft) == (0, 1.0, 543)
    assert studio.property_type is PropertyType.STUDIO
    assert (three_bed.beds, three_bed.baths, three_bed.sqft) == (3, 2.0, 1340)
    assert three_bed.property_type is PropertyType.APARTMENT
    # "Now" units carry the date they came free; a later one its own date.
    assert (studio.available_from, three_bed.available_from) == (TODAY, date(2026, 10, 15))
    assert [str(image) for image in studio.images] == [
        "https://images1.apartments.com/i2/example-image-03/105/image.jpg"
    ]


def test_a_unit_shares_its_buildings_details() -> None:
    listing = units()[0]

    assert listing.description.startswith("Welcome to Example Heights.")
    address = listing.address
    assert (address.line1, address.city, address.region, address.postal_code) == (
        "123 Example St",
        "Denver",
        "CO",
        "00001",
    )
    assert address.country == "US"
    assert (listing.lat, listing.lng) == (39.74, -104.99)
    assert listing.furnished is True
    assert listing.min_lease_months == 3  # "3 - 16 Month Leases"
    assert listing.contact.phone == "(555) 010-0101"
    assert listing.contact.name == "Example Residential"


def test_a_building_without_units_is_one_listing_at_its_rent_range() -> None:
    listing = building()

    assert listing.source_id == "apt0002"
    assert listing.title == "Example Commons"
    assert listing.price == Price(amount=usd(1498), amount_max=usd(2964))
    # "Studio - 2 bd": the rent isn't tied to one plan.
    assert (listing.beds, listing.baths, listing.sqft) == (None, None, None)
    assert listing.property_type is PropertyType.APARTMENT
    assert listing.available_from is None
    assert listing.min_lease_months == 6
    assert len(listing.images) == 3
    assert all("/117/" in str(image) for image in listing.images)


def test_furnished_comes_from_the_amenities_or_the_property_information() -> None:
    # The two signals seen live: an interior amenity, and a fee-list line.
    assert "furnished" in units()[0].amenities
    assert building().furnished is True


def test_an_unfurnished_building_is_rejected() -> None:
    with pytest.raises(NotFurnishedError):
        parse_item(items()[2], today=TODAY)


def test_unfurnished_is_not_furnished() -> None:
    item = items()[2]
    item["description"] = "Unfurnished units only."

    with pytest.raises(NotFurnishedError):
        parse_item(item, today=TODAY)


def test_amenities_become_words_the_extractor_reads() -> None:
    listing = units()[0]

    assert {"air conditioning", "dishwasher", "pets allowed"} <= listing.amenities
    found = KeywordAmenityExtractor().extract(listing)
    for tag in (Amenity.AC, Amenity.DISHWASHER, Amenity.PETS_ALLOWED):
        assert tag in found.present, tag


@pytest.mark.parametrize(
    ("header", "allowed"),
    [
        ("Dogs Allowed", True),
        ("Other Pets Allowed", True),
        ("Birds Allowed", False),
    ],
)
def test_pet_policies(header: str, allowed: bool) -> None:
    fees = [
        {"title": "Details", "policies": [{"header": "x", "values": [{"key": "Furnished"}]}]},
        {"title": "Pet Policies", "policies": [{"header": header, "values": []}]},
    ]

    assert ("pets allowed" in building(fees=fees).amenities) is allowed


def test_bulky_and_personal_blocks_stay_out_of_raw() -> None:
    listing = units()[0]

    for key in ("contact", "reviews", "schools", "transportation", "models", "rentals"):
        assert key not in listing.raw
    assert "Jane Example" not in json.dumps(listing.raw)
    assert "Example Residential" not in json.dumps(listing.raw)
    assert listing.raw["unit"]["totalPrice"] == 1728
    assert "image" not in listing.raw["unit"]
    assert "unit" not in building().raw


def test_an_item_without_an_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="no id"):
        units(id="")


def test_an_item_with_a_foreign_url_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"no apartments\.com URL"):
        units(url="https://elsewhere.example.com/x")


def test_units_without_a_key_are_dropped() -> None:
    item = items()[0]
    item["rentals"] = [{"beds": 1}, "nope"]

    [listing] = parse_item(item, today=TODAY)

    assert listing.source_id == "apt0001"  # the building, as with no units


@pytest.mark.parametrize(
    ("beds", "expected"),
    [("2 bd", 2), ("Studio - 2 bd", None), ("Studio", 0), (None, None)],
)
def test_a_buildings_beds_count_only_when_single(beds: object, expected: int | None) -> None:
    assert building(beds=beds).beds == expected


def test_a_studio_only_building_is_a_studio() -> None:
    assert building(beds="Studio").property_type is PropertyType.STUDIO


@pytest.mark.parametrize(
    ("rent", "expected"),
    [
        ({"min": 1999.5}, Price(amount=Money.from_major("1999.5", "USD"))),
        ({"min": 1500, "max": 1500}, Price(amount=usd(1500))),
        ({"min": 0, "max": 2000}, None),
        ({"min": "lots"}, None),
        (None, None),
        ({"min": True}, None),
    ],
)
def test_building_prices(rent: object, expected: Price | None) -> None:
    assert building(baseRent=rent).price == expected


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"availableDate": "2027-01-22T00:00:00-05:00"}, date(2027, 1, 22)),
        ({"availableDate": "2025-03-01T00:00:00-05:00"}, TODAY),
        ({"availableDate": None, "availability": "11/01/26"}, date(2026, 11, 1)),
        ({"availableDate": None, "availability": "Now"}, TODAY),
        ({"availableDate": None, "availability": "Soon"}, None),
    ],
)
def test_unit_availability(fields: dict[str, Any], expected: date | None) -> None:
    assert unit(**fields).available_from == expected


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("3 - 16 Month Leases", 3),
        ("12 - 24 mo", 12),
        ("Call Office for Lease Terms", None),
    ],
)
def test_lease_months_come_from_the_lease_options(key: str, expected: int | None) -> None:
    fees = [
        {
            "title": "Details",
            "policies": [
                {"header": "Lease Options", "values": [{"key": key}]},
                {"header": "Property Information", "values": [{"key": "Furnished"}]},
            ],
        }
    ]

    assert building(fees=fees).min_lease_months == expected


def test_odd_values_are_ignored_not_fatal() -> None:
    listing = unit(basePrice="lots", beds="two", baths=None, squareFeet=0, image=3)

    assert listing.price is None
    assert (listing.beds, listing.baths, listing.sqft) == (None, None, None)
    assert len(listing.images) == 2  # the building's floor plans instead

    odd = building(
        coordinates={"latitude": 40.0},
        location="nowhere",
        contact="none",
        propertyName="",
        models=[{"imageLarge": "nope"}, 3],
    )
    assert (odd.lat, odd.lng) == (None, None)
    assert odd.title == "Rental"
    assert odd.images == ()
    assert odd.contact.platform_only is True


def test_no_phone_is_platform_only() -> None:
    listing = building(contact={"name": "Example Residential"})

    assert listing.contact.platform_only is True
    assert (listing.contact.name, listing.contact.phone) == (None, None)


# --- Owner type ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"description": "Furnished one bedroom."}, OwnerType.UNKNOWN),
        ({"description": "Furnished. Professionally managed."}, OwnerType.PROPERTY_MANAGER),
        (
            {"description": "Furnished. Property management company on site."},
            OwnerType.PROPERTY_MANAGER,
        ),
        ({"description": "Furnished, by a private owner."}, OwnerType.INDIVIDUAL),
        (
            {"description": "Furnished. Professionally managed by a private owner."},
            OwnerType.INDIVIDUAL,
        ),
        (
            {"description": "Furnished.", "contact": {"name": "Example Management Group"}},
            OwnerType.PROPERTY_MANAGER,
        ),
    ],
)
def test_owner_type_hint(fields: dict[str, Any], expected: OwnerType) -> None:
    assert building(**fields).owner_type is expected


# --- build_input ---------------------------------------------------------------------


def test_input_has_one_url_per_location_with_the_site_filters() -> None:
    criteria = Criteria(
        price_min=usd(1000), price_max=usd(3000), min_beds=2, move_in=date(2026, 12, 1)
    )

    run_input = build_input(criteria, locations=("Exampleville, XX", "00001"), max_results=40)

    assert run_input == {
        "startUrls": [
            "https://www.apartments.com/exampleville-xx/min-2-bedrooms-1000-to-3000/",
            "https://www.apartments.com/00001/min-2-bedrooms-1000-to-3000/",
        ],
        "maxItems": 40,
    }


@pytest.mark.parametrize(
    ("criteria", "path"),
    [
        (Criteria(), ""),
        (Criteria(min_beds=0), ""),
        (Criteria(price_max=usd(2000)), "under-2000/"),
        (Criteria(price_min=usd(900)), "over-900/"),
        (Criteria(min_beds=1), "min-1-bedrooms/"),
    ],
)
def test_input_leaves_out_what_criteria_leave_open(criteria: Criteria, path: str) -> None:
    run_input = build_input(criteria, locations=("A City, ZZ",), max_results=10)

    assert run_input["startUrls"] == [f"https://www.apartments.com/a-city-zz/{path}"]


def test_blank_locations_are_dropped() -> None:
    assert build_input(Criteria(), locations=(" , ",), max_results=5)["startUrls"] == []


# --- The adapter ---------------------------------------------------------------------


def settings(**overrides: Any) -> SourcesSettings:  # noqa: ANN401
    entry = {"api_key": SecretStr(TOKEN), "search_locations": ("Exampleville, XX",), **overrides}
    return SourcesSettings(
        requests_per_minute=600, adapters={"apartments_com": AdapterOverrides(**entry)}
    )


class Apify:
    """Fake Apify: the run ends at once as ``run`` says, and its dataset holds ``dataset``."""

    def __init__(self, dataset: list[Any], run: str = "apify_run_succeeded.json") -> None:
        self.dataset = dataset
        self.run = run
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/runs"):
            return httpx2.Response(201, json=fixture(self.run))
        if path.endswith("/items"):
            return httpx2.Response(200, json=self.dataset)
        return httpx2.Response(404)


def adapter(apify: Apify, source_settings: SourcesSettings | None = None) -> ApartmentsCom:
    config = resolve_config(ApartmentsCom, source_settings or settings())
    return ApartmentsCom(
        config, transport=httpx2.MockTransport(apify), sleep=lambda _: None, today=lambda: TODAY
    )


def test_search_returns_furnished_listings_and_skips_the_rest() -> None:
    apify = Apify([*items(), {"id": ""}])

    listings = adapter(apify).search(Criteria(min_beds=1))

    assert [listing.source_id for listing in listings] == [
        "apt0001-apt0001u1",
        "apt0001-apt0001u2",
        "apt0001-apt0001u3",
        "apt0002",
    ]
    start = apify.requests[0]
    assert str(start.url).startswith(f"{APIFY_API}/acts/epctex~apartments-scraper-api/runs")
    body = json.loads(start.content)
    assert body["startUrls"] == ["https://www.apartments.com/exampleville-xx/min-1-bedrooms/"]
    assert body["maxItems"] == 25
    assert not any(request.url.path == "/robots.txt" for request in apify.requests)


def test_an_empty_dataset_is_no_listings() -> None:
    assert adapter(Apify(fixture("dataset_empty.json"))).search(Criteria()) == []


def test_a_demo_mode_run_fails_with_the_reason() -> None:
    # What the actor returns on Apify's Free plan after its 5 runs a month.
    with pytest.raises(SourceError, match="demo mode"):
        adapter(Apify(fixture("dataset_demo.json"))).search(Criteria())


def test_a_failed_run_fails_the_search() -> None:
    with pytest.raises(SourceError, match="ended FAILED"):
        adapter(Apify(items(), run="apify_run_failed.json")).search(Criteria())


def test_no_search_locations_fails_with_a_hint() -> None:
    apify = Apify([])

    with pytest.raises(SourceError, match="no search locations"):
        adapter(apify, settings(search_locations=None)).search(Criteria())

    assert apify.requests == []


def test_no_api_key_fails_with_a_hint() -> None:
    config = resolve_config(ApartmentsCom, settings(api_key=None))

    with pytest.raises(SourceError, match="no Apify API token"):
        ApartmentsCom(config, transport=httpx2.MockTransport(Apify([]))).search(Criteria())


def test_discovered_and_off_until_it_has_a_key() -> None:
    registry = discover()
    assert registry.get("apartments_com") is ApartmentsCom
    assert ApartmentsCom.default_enabled is False

    by_name = {source.config.name: source for source in registry.configured(SourcesSettings())}
    assert by_name["apartments_com"].config.enabled is False
    assert (
        by_name["apartments_com"].config.disabled_reason
        == "needs the Apify API token: add it under Credentials on the Sources page"
    )

    keyed = {source.config.name: source for source in registry.configured(settings(enabled=True))}
    assert keyed["apartments_com"].config.enabled is True
