"""The Zumper adapter: criteria → actor input, actor items → listings, and a whole
search against a faked Apify. Runs offline on tests/fixtures/zumper (a trimmed live run)."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from furnest.config import AdapterOverrides, SourcesSettings
from furnest.domain import Criteria, Money, OwnerType, Price, PropertyType
from furnest.sources import SourceError, discover, resolve_config
from furnest.sources.adapters.stubs import HotPads
from furnest.sources.adapters.zumper import Zumper
from furnest.sources.adapters.zumper.parse import build_input, parse_item
from furnest.sources.apify import APIFY_API

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "zumper"
TODAY = date(2026, 10, 10)
TOKEN = "apify-test-token"  # noqa: S105 - a fake


def fixture(name: str) -> Any:  # noqa: ANN401 - arbitrary JSON
    return json.loads((FIXTURES / name).read_text())


def items() -> list[dict[str, Any]]:
    return fixture("dataset_live.json")  # type: ignore[no-any-return]


def record(index: int) -> dict[str, Any]:
    return items()[index]["entity"]["data"]  # type: ignore[no-any-return]


# --- parse_item ----------------------------------------------------------------------


def test_a_building_unit_maps_every_field() -> None:
    listing = parse_item(items()[1], today=TODAY)[0]

    assert (listing.source, listing.source_id) == ("zumper", "900000003")
    assert str(listing.url) == (
        "https://www.zumper.com/apartment-buildings/800000002/example-lofts-denver-co"
    )
    assert listing.title == "Example Lofts: S02"
    assert listing.description == (
        "Example Lofts offers studios to two bedrooms. Furnished units available."
    )
    assert listing.price == Price(amount=Money.from_major(1660, "USD"))
    address = listing.address
    assert (address.line1, address.city, address.region, address.postal_code) == (
        "123 Example St #3",
        "Denver",
        "CO",
        "00001",
    )
    assert (listing.lat, listing.lng) == (39.7, -104.9)
    assert (listing.beds, listing.baths, listing.sqft) == (0, 1.0, 526)
    assert listing.property_type is PropertyType.STUDIO
    assert listing.furnished is True
    assert listing.available_from == date(2026, 10, 10)
    assert listing.min_lease_months == 6
    assert [str(i) for i in listing.images] == ["https://img.zumpercdn.com/100000007/1280x960"]
    assert listing.contact.phone == "555-0100"
    assert listing.owner_type is OwnerType.PROPERTY_MANAGER


def test_only_the_units_marked_furnished_are_kept() -> None:
    # Two units carry the "Furnished" tag; the third doesn't, though the building's
    # description mentions furnished units.
    assert [listing.source_id for listing in parse_item(items()[1], today=TODAY)] == [
        "900000003",
        "900000004",
    ]


def test_a_title_saying_furnished_is_enough() -> None:
    first, second = parse_item(items()[0], today=TODAY)

    assert record(0)["floorplan_listings"][0]["amenity_tags"] is None
    assert first.title == "Example Stays at 123 Example St: Example Stays Furnished Apartment 1"
    assert (first.price, second.price) == (
        Price(amount=Money.from_major(1830, "USD")),
        Price(amount=Money.from_major(2404, "USD")),
    )
    assert first.min_lease_months == 1  # the building's 30-day minimum
    assert first.description.startswith("Example Stays is a fully furnished")
    assert first.property_type is PropertyType.APARTMENT


def test_a_unit_without_photos_shows_the_building() -> None:
    building = record(1)
    units = [{**u, "media": []} for u in building["floorplan_listings"]]

    listing = parse_item({**building, "floorplan_listings": units}, today=TODAY)[0]

    assert [str(i) for i in listing.images] == [
        "https://img.zumpercdn.com/100000010/1280x960",
        "https://img.zumpercdn.com/100000011/1280x960",
    ]


def test_an_unfurnished_building_has_no_listings() -> None:
    assert parse_item(items()[2], today=TODAY) == []


def test_a_single_listing() -> None:
    (listing,) = parse_item(items()[3], today=TODAY)

    assert listing.source_id == "900000008"
    assert str(listing.url) == "https://www.zumper.com/listings/900000008p/1-bedroom-denver-co"
    assert listing.title == "Large, Furnished Master Bedroom in Shared House"
    assert listing.address.city == "Denver"
    assert (listing.lat, listing.lng) == (39.7, -104.9)
    assert listing.property_type is PropertyType.OTHER  # Zumper's id 13 is unconfirmed
    assert listing.available_from == date(2026, 11, 1)
    assert listing.min_lease_months == 6
    assert len(listing.images) == 3
    assert listing.owner_type is OwnerType.PROPERTY_MANAGER  # "Example Rentals"


def test_people_and_bulky_blocks_stay_out_of_raw() -> None:
    for item in items():
        for listing in parse_item(item, today=TODAY):
            dumped = json.dumps(listing.raw)
            for leaked in ("Jane Example", "Example Property Management", "555-01", "@example"):
                assert leaked not in dumped
            for key in ("media", "provider_url", "listing_agents", "phone"):
                assert key not in listing.raw
    assert parse_item(items()[1], today=TODAY)[0].raw["building_id"] == 800000002


def test_odd_values_are_ignored_not_fatal() -> None:
    unit = {
        **record(3),
        "price": 0,
        "min_price": None,
        "currency_code": 7,
        "date_available": "soon",
        "min_lease_days": 0,
        "media": "none",
        "listing_location": None,
        "listing_agents": None,
        "phone": None,
    }

    (listing,) = parse_item({"activeListings": [unit]}, today=TODAY)

    assert listing.price is None
    assert (listing.available_from, listing.min_lease_months, listing.images) == (None, None, ())
    assert (listing.lat, listing.lng) == (None, None)
    assert listing.contact.platform_only is True
    assert listing.owner_type is OwnerType.UNKNOWN


@pytest.mark.parametrize(("code", "expected"), [("usd", "USD"), ("XYZ", None), (None, "USD")])
def test_currency_codes(code: object, expected: str | None) -> None:
    (listing,) = parse_item({**record(3), "currency_code": code}, today=TODAY)
    assert (listing.price and listing.price.amount.currency) == expected


def test_units_without_an_id_are_skipped() -> None:
    building = record(1)
    units = [{**u, "listing_id": None} for u in building["floorplan_listings"]]

    assert parse_item({**building, "floorplan_listings": units}, today=TODAY) == []


def test_an_item_without_an_id_or_units_is_rejected() -> None:
    with pytest.raises(ValueError, match="no listing_id or units"):
        parse_item({"entity": {"data": {"building_id": 1}}}, today=TODAY)


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        ("now", TODAY),
        ("2026/12/01", date(2026, 12, 1)),
        ("2026-12-01T00:00:00Z", date(2026, 12, 1)),
    ],
)
def test_availability_strings(when: str, expected: date) -> None:
    (listing,) = parse_item({**record(3), "date_available": when}, today=TODAY)
    assert listing.available_from == expected


# --- build_input ---------------------------------------------------------------------


def test_input_has_one_furnished_url_per_location() -> None:
    run_input = build_input(
        Criteria(min_beds=2), locations=("A City, ZZ", " , ", "Other, YY"), max_results=7
    )

    assert run_input == {
        "startUrls": [
            "https://www.zumper.com/apartments-for-rent/a-city-zz/furnished",
            "https://www.zumper.com/apartments-for-rent/other-yy/furnished",
        ],
        "maxItems": 7,
    }


# --- The adapter ---------------------------------------------------------------------


def settings(**overrides: Any) -> SourcesSettings:  # noqa: ANN401
    entry = {"api_key": SecretStr(TOKEN), "search_locations": ("Exampleville, XX",), **overrides}
    return SourcesSettings(requests_per_minute=600, adapters={"zumper": AdapterOverrides(**entry)})


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


def adapter(apify: Apify, source_settings: SourcesSettings | None = None) -> Zumper:
    config = resolve_config(Zumper, source_settings or settings())
    return Zumper(
        config, transport=httpx2.MockTransport(apify), sleep=lambda _: None, today=lambda: TODAY
    )


def test_search_returns_furnished_listings_and_skips_the_rest() -> None:
    apify = Apify(items())

    listings = adapter(apify).search(Criteria())

    assert [listing.source_id for listing in listings] == [
        "900000001",
        "900000002",
        "900000003",
        "900000004",
        "900000008",
    ]
    start = apify.requests[0]
    assert str(start.url).startswith(f"{APIFY_API}/acts/memo23~zumper-cheerio/runs")
    body = json.loads(start.content)
    assert body["startUrls"] == [
        "https://www.zumper.com/apartments-for-rent/exampleville-xx/furnished"
    ]
    assert body["maxItems"] == 25
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
    config = resolve_config(Zumper, settings(api_key=None))

    with pytest.raises(SourceError, match="no Apify API token"):
        Zumper(config, transport=httpx2.MockTransport(Apify([]))).search(Criteria())


def test_discovered_and_off_until_it_has_a_key() -> None:
    registry = discover()
    assert registry.get("zumper") is Zumper
    assert registry.get("hotpads") is HotPads
    assert Zumper.default_enabled is False

    by_name = {source.config.name: source for source in registry.configured(SourcesSettings())}
    assert by_name["zumper"].config.enabled is False
    assert (
        by_name["zumper"].config.disabled_reason
        == "needs the Apify API token: add it under Credentials on the Sources page"
    )

    keyed = {source.config.name: source for source in registry.configured(settings(enabled=True))}
    assert keyed["zumper"].config.enabled is True
