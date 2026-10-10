"""The Zumper adapter: criteria → actor input, actor items → listings, and a whole
search against a faked Apify. Runs offline on tests/fixtures/zumper."""

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
from furnest.sources.adapters.zumper.parse import NotFurnishedError, build_input, parse_item
from furnest.sources.apify import APIFY_API

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "zumper"
TODAY = date(2026, 10, 10)
TOKEN = "apify-test-token"  # noqa: S105 - a fake


def fixture(name: str) -> Any:  # noqa: ANN401 - arbitrary JSON
    return json.loads((FIXTURES / name).read_text())


def items() -> list[dict[str, Any]]:
    return fixture("dataset_items.json")  # type: ignore[no-any-return]


# --- parse_item ----------------------------------------------------------------------


def test_a_listing_maps_every_documented_field() -> None:
    listing = parse_item(items()[0], today=TODAY)

    assert (listing.source, listing.source_id) == ("zumper", "900000001")
    assert (
        str(listing.url) == "https://www.zumper.com/apartments/900000001/example-st-exampleville-xx"
    )
    assert listing.title == "Furnished one-bedroom near Example Park"
    assert (
        listing.description
        == "Fully furnished one-bedroom unit with all utilities included. No pets."
    )
    assert listing.price == Price(amount=Money.from_major(2280, "USD"))
    address = listing.address
    assert (address.line1, address.city, address.region, address.postal_code) == (
        "100 Example St #1",
        "Exampleville",
        "XX",
        "00001",
    )
    assert (listing.lat, listing.lng) == (40.0, -100.0)
    assert (listing.beds, listing.baths, listing.sqft) == (1, 1.5, 585)
    assert listing.property_type is PropertyType.APARTMENT
    assert listing.furnished is True
    assert listing.available_from == date(2026, 11, 1)
    assert listing.min_lease_months == 12
    assert [str(i) for i in listing.images] == [
        "https://img.zumpercdn.com/100000001/1280x960",
        "https://img.zumpercdn.com/100000002/1280x960",
    ]
    assert listing.contact.phone == "555-0100"
    assert listing.owner_type is OwnerType.PROPERTY_MANAGER


def test_people_and_bulky_blocks_stay_out_of_raw() -> None:
    raw = parse_item(items()[0], today=TODAY).raw
    dumped = json.dumps(raw)

    for leaked in ("Example Management", "555-0100", "leasing@example.com", "provider_url"):
        assert leaked not in dumped
    assert raw["feed_name"] == "example"
    assert "media" not in raw


def test_the_activelistings_variant_with_unknowns() -> None:
    listing = parse_item(items()[1], today=TODAY)

    assert listing.source_id == "900000002"
    assert str(listing.url) == "https://www.zumper.com/apartments/900000002"
    assert listing.property_type is PropertyType.STUDIO
    assert listing.baths == 1.0
    assert (listing.lat, listing.lng) == (None, None)
    assert (listing.available_from, listing.min_lease_months, listing.images) == (None, None, ())
    assert listing.contact.platform_only is True
    assert listing.owner_type is OwnerType.AGENT


def test_odd_values_are_ignored_not_fatal() -> None:
    listing = parse_item(items()[4], today=TODAY)

    assert listing.price is None  # 0 is not a rent
    assert listing.available_from == date(2026, 11, 1)  # a unix time
    assert listing.min_lease_months is None
    assert listing.owner_type is OwnerType.UNKNOWN


def test_unfurnished_is_skipped() -> None:
    with pytest.raises(NotFurnishedError):
        parse_item(items()[2], today=TODAY)


def test_an_item_without_an_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="no listing_id"):
        parse_item(items()[3], today=TODAY)


@pytest.mark.parametrize("when", ["now", "Available Now", "2026-12-01T00:00:00Z"])
def test_availability_strings(when: str) -> None:
    item = {**items()[2], "title": "Furnished", "description": "", "available_on": when}

    expected = date(2026, 12, 1) if when.startswith("2026") else TODAY
    assert parse_item(item, today=TODAY).available_from == expected


# --- build_input ---------------------------------------------------------------------


def test_input_has_one_url_per_location() -> None:
    run_input = build_input(
        Criteria(min_beds=2), locations=("A City, ZZ", " , ", "Other, YY"), max_results=7
    )

    assert run_input == {
        "startUrls": [
            "https://www.zumper.com/apartments-for-rent/a-city-zz",
            "https://www.zumper.com/apartments-for-rent/other-yy",
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

    assert [listing.source_id for listing in listings] == ["900000001", "900000002", "900000005"]
    start = apify.requests[0]
    assert str(start.url).startswith(f"{APIFY_API}/acts/memo23~zumper-cheerio/runs")
    body = json.loads(start.content)
    assert body["startUrls"] == ["https://www.zumper.com/apartments-for-rent/exampleville-xx"]
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
