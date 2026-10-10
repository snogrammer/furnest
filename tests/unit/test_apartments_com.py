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
    return fixture("dataset_items.json")  # type: ignore[no-any-return]


def usd(amount: int) -> Money:
    return Money.from_major(amount, "USD")


def one(**fields: Any) -> Any:  # noqa: ANN401 - a NormalizedListing
    return parse_item({**items()[0], **fields}, today=TODAY)


# --- parse_item ----------------------------------------------------------------------


def test_a_building_maps_every_documented_field() -> None:
    listing = parse_item(items()[0], today=TODAY)

    assert (listing.source, listing.source_id) == ("apartments_com", "abc1234")
    assert str(listing.url) == "https://www.apartments.com/example-towers-exampleville-xx/abc1234/"
    assert listing.title == "Example Towers"
    assert listing.description.startswith("Welcome to Example Towers. Professionally managed")
    assert "\n" not in listing.description
    assert listing.price == Price(amount=usd(2800))
    address = listing.address
    assert (address.line1, address.city, address.region, address.postal_code) == (
        "100 Example St",
        "Exampleville",
        "XX",
        "00001",
    )
    assert (address.neighborhood, address.country) == ("Midtown", "US")
    assert (listing.lat, listing.lng) == (40.0, -100.0)
    # Ranges: the actor doesn't say which floor plan the rent is for.
    assert (listing.beds, listing.baths, listing.sqft) == (None, 1.0, None)
    assert listing.property_type is PropertyType.APARTMENT
    assert listing.furnished is True
    assert listing.min_lease_months == 12
    assert len(listing.images) == 2
    assert listing.contact.phone == "(555) 010-0100"
    assert listing.contact.platform_only is False


def test_amenities_become_words_the_extractor_reads() -> None:
    listing = parse_item(items()[0], today=TODAY)

    assert {"air conditioning", "dishwasher", "fitness center", "pets allowed"} <= (
        listing.amenities
    )
    found = KeywordAmenityExtractor().extract(listing)
    for tag in (Amenity.AC, Amenity.DISHWASHER, Amenity.PETS_ALLOWED):
        assert tag in found.present, tag


def test_bulky_and_personal_blocks_stay_out_of_raw() -> None:
    listing = parse_item(items()[0], today=TODAY)

    for key in ("contact", "reviews", "schools", "transportation", "photos", "score"):
        assert key not in listing.raw
    assert "Jane Example" not in json.dumps(listing.raw)
    assert listing.raw["lastUpdated"] == "2 weeks ago"


def test_a_studio_with_a_name_but_no_phone_is_platform_only() -> None:
    listing = parse_item(items()[1], today=TODAY)

    assert (listing.beds, listing.baths, listing.sqft) == (0, 1.0, 450)
    assert listing.property_type is PropertyType.STUDIO
    assert listing.price == Price(amount=usd(1500))
    assert listing.contact.platform_only is True
    assert (listing.contact.name, listing.contact.phone) == (None, None)
    assert listing.min_lease_months is None


def test_an_item_that_never_says_furnished_is_rejected() -> None:
    with pytest.raises(NotFurnishedError):
        parse_item(items()[2], today=TODAY)


def test_an_item_without_an_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="no id"):
        parse_item(items()[3], today=TODAY)


def test_an_item_with_a_foreign_url_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"no apartments\.com URL"):
        one(url="https://elsewhere.example.com/x")


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"beds": "2 bd"}, 2),
        ({"beds": "Studio - 2 bd"}, None),
        ({"beds": "Studio"}, 0),
        ({"beds": None}, None),
    ],
)
def test_beds(fields: dict[str, Any], expected: int | None) -> None:
    assert one(**fields).beds == expected


@pytest.mark.parametrize(
    ("rent", "expected"),
    [
        ({"min": 1999.5}, Price(amount=Money.from_major("1999.5", "USD"))),
        ({"min": 0}, None),
        ({"min": "lots"}, None),
        (None, None),
        ({"min": True}, None),
    ],
)
def test_prices(rent: object, expected: Price | None) -> None:
    assert one(rent=rent).price == expected


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("Lease Options", 12),
        ("Lease Terms", 12),
        ("Other Fees", None),
    ],
)
def test_lease_months_come_from_the_lease_options(key: str, expected: int | None) -> None:
    fees = [{"title": "Details", "policies": [{"header": key, "values": [{"key": "12 - 24 mo"}]}]}]

    assert one(fees=fees).min_lease_months == expected


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"availableFrom": "2026-12-01T00:00:00"}, date(2026, 12, 1)),
        ({"availability": "Available Now"}, TODAY),
        ({"availability": "Soon"}, None),
        ({}, None),
    ],
)
def test_availability_when_the_actor_gives_it(
    fields: dict[str, Any], expected: date | None
) -> None:
    assert one(**fields).available_from == expected


def test_odd_values_are_ignored_not_fatal() -> None:
    listing = one(
        coordinates={"latitude": 40.0},
        location="nowhere",
        photos=["nope", 3, "https://images1.apartments.com/i2/1/image.jpg"],
        contact="none",
        propertyName="",
        baths="lots",
    )

    assert (listing.lat, listing.lng, listing.baths) == (None, None, None)
    assert listing.title == "Rental"
    assert len(listing.images) == 1
    assert listing.contact.platform_only is True


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
    assert one(**fields).owner_type is expected


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
    apify = Apify(items())

    listings = adapter(apify).search(Criteria(min_beds=1))

    assert [listing.source_id for listing in listings] == ["abc1234", "def5678"]
    start = apify.requests[0]
    assert str(start.url).startswith(f"{APIFY_API}/acts/epctex~apartments-scraper-api/runs")
    body = json.loads(start.content)
    assert body["startUrls"] == ["https://www.apartments.com/exampleville-xx/min-1-bedrooms/"]
    assert body["maxItems"] == 100
    assert not any(request.url.path == "/robots.txt" for request in apify.requests)


def test_an_empty_dataset_is_no_listings() -> None:
    assert adapter(Apify(fixture("dataset_empty.json"))).search(Criteria()) == []


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
