"""The Craigslist adapter: criteria → search URLs, pages → listings, and a whole search
against a faked Craigslist. Runs offline on tests/fixtures/craigslist."""

import json
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from furnest.config import AdapterOverrides, SourcesSettings
from furnest.domain import (
    Amenity,
    Criteria,
    Currency,
    Money,
    OwnerType,
    PropertyType,
    RentPeriod,
    register_currency,
)
from furnest.matching.amenities import KeywordAmenityExtractor
from furnest.sources import SourceBlockedError, SourceError, discover, resolve_config
from furnest.sources.adapters.craigslist import Craigslist, DetailCache
from furnest.sources.adapters.craigslist.parse import (
    Detail,
    SearchResult,
    categories,
    parse_detail,
    parse_search,
    post_token,
    search_url,
    to_listing,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "craigslist"
VIEW = "https://www.craigslist.org/view/d"
ROBOTS = "User-agent: *\nDisallow: /reply\n"
LD_OPEN = '<script type="application/ld+json" id="ld_searchpage_results">'
# Post tokens from the captured pages.
SEARCH_FIRST = "5gfoKTt9xX8DupSCmmLoEV"
APARTMENT = "kcV6pvvoX8HFuQEJhdG1qA"


def page(name: str) -> str:
    return (FIXTURES / name).read_text()


def usd(amount: int) -> Money:
    return Money.from_major(amount, "USD")


def search_page(*rows: tuple[str, str, str], items: list[dict[str, Any]] | None = None) -> str:
    """A minimal search page: ``rows`` are (token, title, price)."""
    lis = "".join(
        f'<li class="cl-static-search-result" title="{title}">'
        f'<a href="{VIEW}/slug/{token}"><div class="title">{title}</div>'
        f'<div class="details"><div class="price">{price}</div>'
        f'<div class="location"> somewhere </div></div></a></li>'
        for token, title, price in rows
    )
    elements = [
        {"@type": "ListItem", "position": str(i), "item": it} for i, it in enumerate(items or [])
    ]
    ld = {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": elements}
    return (
        f'<script type="application/ld+json" id="ld_searchpage_results" >{json.dumps(ld)}</script>'
        '<ol class="cl-static-search-results">'
        f'<li class="cl-static-hub-links">see also</li>{lis}</ol>'
    )


def posting(
    *chips: str, body: str = "A furnished place.", posted: str = "2026-10-07T10:25:32-0700"
) -> str:
    attrs = "".join(f'<span class="attr important"> {chip} </span>' for chip in chips)
    return (
        f'<div class="attrgroup">{attrs}</div>'
        f'<section id="postingbody"><div class="print-information print-qrcode-container">'
        f"<p>QR Code Link to This Post</p></div>{body}</section>"
        f'<p class="postinginfo">post id: 7000000001</p>'
        f'<time class="date timeago" datetime="{posted}">2026-10-07</time>'
    )


def row(**fields: Any) -> SearchResult:  # noqa: ANN401 - SearchResult fields
    values: dict[str, Any] = {
        "token": "abc123",
        "url": f"{VIEW}/slug/abc123",
        "title": "A furnished place",
        "price": "$2,000",
        "neighborhood": "downtown",
        "category": "apa",
    }
    return SearchResult(**{**values, **fields})


def detail(*chips: str, **fields: Any) -> Detail:  # noqa: ANN401 - Detail fields
    values: dict[str, Any] = {"description": "", "attributes": chips, "data": {}}
    return Detail(**{**values, **fields})


# --- Search URLs --------------------------------------------------------------------


def test_search_url_sends_the_furnished_filter_and_criteria() -> None:
    criteria = Criteria(
        price_min=Money.from_major("1500.50", "USD"),
        price_max=Money.from_major("2999.50", "USD"),
        min_beds=2,
        min_baths=1.5,
    )

    url = urlsplit(search_url("sfbay", "apa", criteria))

    assert (url.scheme, url.netloc, url.path) == (
        "https",
        "www.craigslist.org",
        "/search/area/sfbay",
    )
    assert parse_qs(url.query) == {
        "cat": ["apa"],
        "is_furnished": ["1"],
        "sort": ["date"],
        "min_price": ["1500"],
        "max_price": ["3000"],
        "min_bedrooms": ["2"],
        "min_bathrooms": ["1"],
    }


def test_search_url_leaves_out_what_does_not_apply() -> None:
    register_currency(Currency(code="EUR", exponent=2, symbol="€"))
    criteria = Criteria(price_min=Money.from_major(1000, "EUR"), min_beds=0, min_baths=0.5)

    query = parse_qs(urlsplit(search_url("chicago", "apa", criteria)).query)

    assert set(query) == {"cat", "is_furnished", "sort"}


def test_rooms_are_searched_without_beds_and_baths() -> None:
    query = parse_qs(urlsplit(search_url("sfbay", "roo", Criteria(min_beds=2, min_baths=2))).query)

    assert "min_bedrooms" not in query
    assert "min_bathrooms" not in query


@pytest.mark.parametrize("site", ["", "SF Bay", "sfbay.craigslist.org", "../x"])
def test_search_url_rejects_what_is_not_a_site_name(site: str) -> None:
    with pytest.raises(ValueError, match="not a Craigslist site name"):
        search_url(site, "apa", Criteria())


@pytest.mark.parametrize(
    ("allow", "deny", "expected"),
    [
        ((), (), ("apa", "sub", "roo")),
        ((PropertyType.ROOM,), (), ("roo",)),
        ((PropertyType.APARTMENT,), (), ("apa", "sub")),
        ((PropertyType.APARTMENT, PropertyType.ROOM), (), ("apa", "sub", "roo")),
        ((), (PropertyType.ROOM,), ("apa", "sub")),
    ],
)
def test_categories_follow_property_types(
    allow: tuple[PropertyType, ...], deny: tuple[PropertyType, ...], expected: tuple[str, ...]
) -> None:
    criteria = Criteria(property_types_allow=allow, property_types_deny=deny)

    assert categories(criteria) == expected


# --- Search pages -------------------------------------------------------------------


def test_a_captured_search_page_zips_rows_with_json_ld() -> None:
    rows = parse_search(page("search_apa.html"), category="apa")

    assert len(rows) == 14
    first = rows[0]
    assert first.token == SEARCH_FIRST
    assert first.url == f"{VIEW}/emeryville-all-inclusive-living-near/{SEARCH_FIRST}"
    assert (first.price, first.neighborhood, first.category) == ("$1,550", "berkeley", "apa")
    assert first.data["address"]["addressLocality"] == "Emeryville"
    assert all(r.data for r in rows)


def test_an_empty_search_page_has_no_rows() -> None:
    assert parse_search(search_page(), category="apa") == []


def test_a_page_without_a_results_list_is_an_error() -> None:
    with pytest.raises(ValueError, match="not a Craigslist search page"):
        parse_search("<html><body>Something else</body></html>", category="apa")


def test_json_ld_is_ignored_when_it_does_not_line_up() -> None:
    html = search_page(("aaa", "One", "$1"), ("bbb", "Two", "$2"), items=[{"latitude": 1}])

    rows = parse_search(html, category="sub")

    assert [(r.token, r.data) for r in rows] == [("aaa", {}), ("bbb", {})]


def test_rows_without_a_post_url_or_title_are_dropped_but_keep_their_place() -> None:
    html = search_page(
        ("aaa", "", "$1"), ("bbb", "Two", "$2"), items=[{"latitude": 1}, {"latitude": 2}]
    ).replace(f"{VIEW}/slug/aaa", "https://example.com/elsewhere/")

    rows = parse_search(html, category="apa")

    assert [(r.token, r.data) for r in rows] == [("bbb", {"latitude": 2})]


@pytest.mark.parametrize(
    "script",
    [
        f"{LD_OPEN}{{oops</script>",
        f"{LD_OPEN}[1, 2]</script>",
        f'{LD_OPEN}{{"itemListElement": 3}}</script>',
    ],
)
def test_unreadable_json_ld_leaves_rows_without_data(script: str) -> None:
    html = search_page(("aaa", "One", "$1")).split("</script>", 1)[1]

    rows = parse_search(script + html, category="apa")

    assert [(r.token, r.data) for r in rows] == [("aaa", {})]


def test_json_ld_items_that_are_not_objects_read_as_empty() -> None:
    ld = {"itemListElement": [{"item": "x"}, 5]}
    html = search_page(("aaa", "One", "$1"), ("bbb", "Two", "$2")).split("</script>", 1)[1]
    script = (
        f'<script type="application/ld+json" id="ld_searchpage_results">{json.dumps(ld)}</script>'
    )

    rows = parse_search(script + html, category="apa")

    assert [r.data for r in rows] == [{}, {}]


@pytest.mark.parametrize(
    ("url", "token"),
    [
        (f"{VIEW}/slug/AbC123", "AbC123"),
        ("https://sfbay.craigslist.org/sfc/apa/d/slug/7979564955.html", "7979564955"),
        ("https://craigslist.org/view/d/slug/x1/", "x1"),
        ("http://www.craigslist.org/view/d/slug/abc", None),
        ("https://craigslist.org.example.com/view/d/slug/abc", None),
        ("https://www.craigslist.org/view/d/slug/a-b", None),
        ("", None),
    ],
)
def test_post_token(url: str, token: str | None) -> None:
    assert post_token(url) == token


# --- Posting pages ------------------------------------------------------------------


def test_a_captured_posting_reads_chips_body_json_ld_and_photos() -> None:
    found = parse_detail(page("detail_apartment.html"))

    assert found is not None
    assert found.attributes[:5] == (
        "1br / 1ba",
        "750ft2",
        "available now",
        "application fee details: 45.00",
        "rent period: monthly",
    )
    assert "w/d in unit" in found.attributes
    assert found.description.startswith("Looking for a comfortable place to call home?")
    assert "QR Code Link" not in found.description
    assert "\n\n\n" not in found.description
    assert found.data["address"]["postalCode"] == "95403"
    assert found.posted == date(2026, 9, 24)
    assert found.post_id == "7972213896"
    assert found.email is None
    assert len(found.images) == 14
    assert all(i.endswith("_600x450.jpg") for i in found.images)


def test_an_email_written_in_the_body_is_the_contact() -> None:
    found = parse_detail(posting(body="Write to <b>person@example.com</b> for a tour."))

    assert found is not None
    assert found.email == "person@example.com"


@pytest.mark.parametrize(
    "notice",
    [
        "This posting has been deleted by its author.",
        "This posting has been flagged for removal.",
        "This posting has expired.",
    ],
)
def test_a_taken_down_posting_reads_as_none(notice: str) -> None:
    assert parse_detail(f"<html><body><h2>{notice}</h2></body></html>") is None


def test_a_page_that_is_not_a_posting_is_an_error() -> None:
    with pytest.raises(ValueError, match="no posting body"):
        parse_detail("<html><body>Hello</body></html>")


def test_unparseable_times_and_unclosed_tags_are_tolerated() -> None:
    html = posting(posted="yesterday").replace("</section>", "<p><b>Unclosed</section>")
    html = '<time datetime="">x</time><br/><img src="x">' + html

    found = parse_detail(html)

    assert found is not None
    assert found.posted is None
    assert "Unclosed" in found.description


# --- Mapping ------------------------------------------------------------------------


def test_a_captured_apartment_maps_every_field() -> None:
    result = row(token=APARTMENT, title="Furnished and Unfurnished options")
    found = parse_detail(page("detail_apartment.html"))

    listing = to_listing(result, found)

    assert (listing.source, listing.source_id) == ("craigslist", APARTMENT)
    assert listing.price is not None
    assert (listing.price.amount, listing.price.period) == (usd(2000), RentPeriod.MONTHLY)
    assert (listing.beds, listing.baths, listing.sqft) == (1, 1.0, 750)
    assert listing.property_type is PropertyType.APARTMENT
    assert listing.furnished is True
    assert (listing.address.city, listing.address.region, listing.address.postal_code) == (
        "Santa Rosa",
        "CA",
        "95403",
    )
    assert listing.address.line1 == "123 Example St"
    assert (listing.lat, listing.lng) == (38.48, -122.73)
    assert listing.available_from == date(2026, 9, 24)
    assert listing.contact.platform_only is True
    assert listing.contact.email is None
    assert len(listing.images) == 14
    assert "application fee details: 45.00" not in listing.amenities
    assert {"w/d in unit", "carport", "no smoking", "air conditioning"} <= listing.amenities
    assert listing.raw["post_id"] == "7972213896"
    assert listing.raw["labelled"] == {"application fee details": "45.00", "rent period": "monthly"}


def test_captured_chips_read_as_amenities() -> None:
    listing = to_listing(row(), parse_detail(page("detail_room.html")))

    result = KeywordAmenityExtractor().extract(listing)

    assert {
        Amenity.CATS_ALLOWED,
        Amenity.DOGS_ALLOWED,
        Amenity.SHARED_LAUNDRY,
        Amenity.SMOKE_FREE,
        Amenity.AC,
    } <= result.present
    assert Amenity.IN_UNIT_LAUNDRY not in result.present


def test_a_private_room_is_a_room_whatever_the_housing_type() -> None:
    listing = to_listing(row(category="sub"), parse_detail(page("detail_room.html")))

    assert listing.property_type is PropertyType.ROOM
    assert listing.address.line1 is None


def test_the_rooms_category_is_a_room() -> None:
    assert to_listing(row(category="roo"), None).property_type is PropertyType.ROOM


def test_a_search_row_alone_maps_from_its_json_ld() -> None:
    data = {
        "@type": "Apartment",
        "numberOfBedrooms": 0,
        "numberOfBathroomsTotal": 1,
        "latitude": 37.7,
        "longitude": -122.4,
        "address": {"addressLocality": "Oakland", "addressRegion": "CA", "streetAddress": ""},
    }

    listing = to_listing(row(data=data), None)

    assert listing.property_type is PropertyType.STUDIO
    assert (listing.beds, listing.baths, listing.lat, listing.lng) == (0, 1.0, 37.7, -122.4)
    assert (listing.address.city, listing.address.line1) == ("Oakland", None)
    assert listing.description == ""
    assert listing.amenities == frozenset()
    assert listing.contact.platform_only is True
    assert "posting" not in listing.raw


def test_half_a_coordinate_is_no_coordinate() -> None:
    listing = to_listing(row(data={"latitude": 37.7}), None)

    assert (listing.lat, listing.lng) == (None, None)


def test_a_weekly_rent_period_keeps_the_weekly_price() -> None:
    listing = to_listing(row(price="$700"), detail("rent period: weekly"))

    assert listing.price is not None
    assert (listing.price.amount, listing.price.period) == (usd(700), RentPeriod.WEEKLY)


def test_no_price_shown_is_no_price() -> None:
    assert to_listing(row(price=""), None).price is None


@pytest.mark.parametrize(
    ("chips", "expected"),
    [
        (("house",), PropertyType.SINGLE_FAMILY),
        (("cottage/cabin",), PropertyType.SINGLE_FAMILY),
        (("townhouse",), PropertyType.TOWNHOUSE),
        (("condo",), PropertyType.CONDO),
        (("in-law",), PropertyType.APARTMENT),
        (("0br / 1ba", "apartment"), PropertyType.STUDIO),
        (("manufactured",), PropertyType.OTHER),
        ((), PropertyType.OTHER),
    ],
)
def test_housing_chips_set_the_property_type(
    chips: tuple[str, ...], expected: PropertyType
) -> None:
    assert to_listing(row(), detail(*chips)).property_type is expected


def test_chips_beat_json_ld_and_odd_wording_is_reworded() -> None:
    found = detail(
        "2br / 1.5ba",
        "1,200ft2",
        "laundry in bldg",
        "w/d hookups",
        data={"numberOfBedrooms": "5", "@type": "House"},
    )

    listing = to_listing(row(), found)

    assert (listing.beds, listing.baths, listing.sqft) == (2, 1.5, 1200)
    assert listing.amenities == {"laundry in building", "laundry hookups"}
    assert listing.property_type is PropertyType.OTHER


@pytest.mark.parametrize(
    ("chip", "posted", "expected"),
    [
        ("available oct 15", date(2026, 10, 7), date(2026, 10, 15)),
        ("available sept 1", date(2026, 10, 7), date(2026, 9, 1)),
        ("available jan 5", date(2026, 12, 20), date(2027, 1, 5)),
        ("available now", date(2026, 10, 7), date(2026, 10, 7)),
        ("available feb 30", date(2026, 10, 7), None),
        ("available oct 15", None, None),
        ("available soon", date(2026, 10, 7), None),
        ("available smarch 5", date(2026, 10, 7), None),
    ],
)
def test_available_dates_take_their_year_from_the_posting(
    chip: str, posted: date | None, expected: date | None
) -> None:
    listing = to_listing(row(), detail(chip, posted=posted))

    assert listing.available_from == expected


def test_an_email_makes_the_contact_direct() -> None:
    listing = to_listing(row(), detail(email="person@example.com"))

    assert listing.contact.email == "person@example.com"
    assert listing.contact.platform_only is False


# --- The adapter --------------------------------------------------------------------


def settings(**overrides: Any) -> SourcesSettings:  # noqa: ANN401
    entry = {"search_locations": ("sfbay",), **overrides}
    return SourcesSettings(
        requests_per_minute=600, adapters={"craigslist": AdapterOverrides(**entry)}
    )


class Site:
    """Fake Craigslist: robots.txt, search pages by category, postings by token."""

    def __init__(
        self,
        searches: dict[str, str] | None = None,
        postings: dict[str, httpx2.Response | str] | None = None,
        robots: str = ROBOTS,
    ) -> None:
        self.searches = searches if searches is not None else {"apa": page("search_apa.html")}
        self.postings = postings or {}
        self.robots = robots
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/robots.txt":
            return httpx2.Response(200, text=self.robots)
        if path.startswith("/search/area/"):
            category = request.url.params["cat"]
            return html_response(self.searches.get(category, search_page()))
        token = path.rpartition("/")[2]
        reply = self.postings.get(token, posting("1br / 1ba", "apartment"))
        return reply if isinstance(reply, httpx2.Response) else html_response(reply)

    def paths(self, prefix: str) -> list[str]:
        return [r.url.path for r in self.requests if r.url.path.startswith(prefix)]


def html_response(body: str, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


def adapter(
    site: Site, source_settings: SourcesSettings | None = None, cache: DetailCache | None = None
) -> Craigslist:
    config = resolve_config(Craigslist, source_settings or settings())
    return Craigslist(
        config,
        transport=httpx2.MockTransport(site),
        cache=cache if cache is not None else DetailCache(),
        sleep=lambda _: None,
    )


def test_search_fetches_each_category_then_each_posting() -> None:
    site = Site()

    listings = adapter(site).search(Criteria(property_types_deny=(PropertyType.ROOM,)))

    assert len(listings) == 14
    assert [r.url.params["cat"] for r in site.requests if r.url.path.startswith("/search/")] == [
        "apa",
        "sub",
    ]
    assert len(site.paths("/view/")) == 14
    assert all(listing.description == "A furnished place." for listing in listings)
    assert all(listing.furnished for listing in listings)


def test_a_second_run_reads_postings_from_the_cache() -> None:
    site, cache = Site(), DetailCache()
    criteria = Criteria(property_types_allow=(PropertyType.APARTMENT,))

    first = adapter(site, cache=cache).search(criteria)
    fetched = len(site.paths("/view/"))
    second = adapter(site, cache=cache).search(criteria)

    assert fetched == 14
    assert len(site.paths("/view/")) == fetched
    assert [(x.source_id, x.description) for x in second] == [
        (x.source_id, x.description) for x in first
    ]


def test_rows_are_taken_in_turn_across_pages_and_capped() -> None:
    apartments = search_page(("a1", "A1", "$1"), ("a2", "A2", "$1"), ("a3", "A3", "$1"))
    sublets = search_page(("s1", "S1", "$1"), ("a1", "A1 again", "$1"), ("s3", "S3", "$1"))
    site = Site(searches={"apa": apartments, "sub": sublets})

    listings = adapter(site, settings(max_results=4)).search(
        Criteria(property_types_deny=(PropertyType.ROOM,))
    )

    assert [listing.source_id for listing in listings] == ["a1", "s1", "a2", "a3"]


def test_several_sites_are_each_searched() -> None:
    site = Site(searches={"apa": search_page(("a1", "A1", "$1"))})

    adapter(site, settings(search_locations=("sfbay", "chicago"))).search(
        Criteria(property_types_allow=(PropertyType.ROOM,))
    )

    assert site.paths("/search/") == ["/search/area/sfbay", "/search/area/chicago"]


def test_a_taken_down_posting_drops_its_row() -> None:
    site = Site(
        searches={"apa": search_page(("gone", "G", "$1"), ("dead", "D", "$1"), ("ok", "O", "$1"))},
        postings={
            "gone": html_response("Not found", status=404),
            "dead": "<h2>This posting has been deleted by its author.</h2>",
        },
    )

    listings = adapter(site).search(Criteria(property_types_deny=(PropertyType.ROOM,)))

    assert [listing.source_id for listing in listings] == ["ok"]


def test_a_posting_that_fails_keeps_its_row_and_is_retried_next_run() -> None:
    cache = DetailCache()
    site = Site(
        searches={"apa": search_page(("bad", "B", "$1"), ("odd", "O", "$1"))},
        postings={"bad": html_response("oops", status=400), "odd": "<p>not a posting</p>"},
    )
    criteria = Criteria(property_types_deny=(PropertyType.ROOM,))

    listings = adapter(site, cache=cache).search(criteria)
    adapter(site, cache=cache).search(criteria)

    assert [(listing.source_id, listing.description) for listing in listings] == [
        ("bad", ""),
        ("odd", ""),
    ]
    assert site.paths("/view/").count("/view/d/slug/bad") == 2
    assert len(cache) == 0


def test_postings_past_the_time_budget_wait_for_a_later_run() -> None:
    # 60s x 0.8 at 6 a minute is 4 requests: 2 search pages, then 2 postings.
    site = Site(searches={"apa": search_page(*((f"t{i}", f"T{i}", "$1") for i in range(4)))})
    tight = settings(requests_per_minute=6, run_timeout_seconds=60)

    listings = adapter(site, tight).search(Criteria(property_types_deny=(PropertyType.ROOM,)))

    assert [listing.description for listing in listings] == ["A furnished place."] * 2 + [""] * 2
    assert len(site.paths("/view/")) == 2


@pytest.mark.parametrize(
    "reply",
    [
        html_response("Your request has been blocked.", status=403),
        html_response("<html>Your request has been blocked.</html>"),
    ],
)
def test_a_blocked_search_fails_the_run(reply: httpx2.Response) -> None:
    def blocked(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/robots.txt":
            return httpx2.Response(200, text=ROBOTS)
        return reply

    config = resolve_config(Craigslist, settings())
    with pytest.raises(SourceBlockedError):
        Craigslist(config, transport=httpx2.MockTransport(blocked), cache=DetailCache()).search(
            Criteria()
        )


def test_a_blocked_posting_fails_the_run() -> None:
    site = Site(
        searches={"apa": search_page(("t1", "T1", "$1"))},
        postings={"t1": html_response('<div class="h-captcha"></div>')},
    )

    with pytest.raises(SourceBlockedError):
        adapter(site).search(Criteria(property_types_deny=(PropertyType.ROOM,)))


def test_an_unreadable_search_page_fails_the_run() -> None:
    site = Site(searches={"apa": "<html>a new layout</html>"})

    with pytest.raises(SourceError, match="not a Craigslist search page"):
        adapter(site).search(Criteria())


def test_robots_txt_is_obeyed() -> None:
    site = Site(robots="User-agent: *\nDisallow: /search\n")

    with pytest.raises(SourceBlockedError, match=r"robots\.txt"):
        adapter(site).search(Criteria())

    assert site.paths("/search/") == []


def test_no_search_locations_fails_with_a_hint() -> None:
    site = Site()

    with pytest.raises(SourceError, match="no search locations for craigslist"):
        adapter(site, settings(search_locations=None)).search(Criteria())

    assert site.requests == []


def test_search_locations_must_be_site_names() -> None:
    with pytest.raises(SourceError, match="'San Francisco, CA'"):
        adapter(Site(), settings(search_locations=("sfbay", "San Francisco, CA"))).search(
            Criteria()
        )


def test_rows_that_fail_to_map_are_skipped() -> None:
    site = Site(searches={"apa": search_page(("t1", "T1", "$1"), ("t2", "T2", "$1"))})
    site.postings = {"t1": posting("rent period: monthly")}
    criteria = Criteria(property_types_deny=(PropertyType.ROOM,))
    found = adapter(site)
    # A posting whose JSON-LD puts the place off the map can't become a listing.
    found._cache.put("t2", detail(data={"latitude": 500, "longitude": 1}))

    assert [listing.source_id for listing in found.search(criteria)] == ["t1"]


def test_the_cache_is_bounded_and_expires() -> None:
    now = [0.0]
    cache = DetailCache(max_entries=2, ttl_seconds=10, clock=lambda: now[0])
    cache.put("a", detail())
    cache.put("b", detail())
    assert cache.get("a") is not None  # a is now the most recent
    cache.put("c", detail())

    assert (cache.get("a") is not None, cache.get("b"), cache.get("c") is not None) == (
        True,
        None,
        True,
    )
    now[0] = 11
    assert cache.get("a") is None
    assert len(cache) == 1


def test_discovered_and_off_by_default_with_no_key_needed() -> None:
    registry = discover()
    assert registry.get("craigslist") is Craigslist

    by_name = {source.config.name: source for source in registry.configured(SourcesSettings())}
    assert by_name["craigslist"].config.enabled is False
    assert by_name["craigslist"].config.disabled_reason is None

    on = {source.config.name: source for source in registry.configured(settings(enabled=True))}
    assert on["craigslist"].config.enabled is True


# --- Owner type ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chips", "description", "expected"),
    [
        ((), "", OwnerType.UNKNOWN),
        (("application fee details: 45.00",), "", OwnerType.PROPERTY_MANAGER),
        (("broker fee: one month",), "", OwnerType.AGENT),
        # A chip beats the body; without one the body decides.
        (("broker fee: one month",), "No brokers, I am the owner.", OwnerType.AGENT),
        ((), "No brokers, I am the owner.", OwnerType.INDIVIDUAL),
        ((), "Contact our leasing office today.", OwnerType.PROPERTY_MANAGER),
        (("w/d in unit", "cats are OK - purrr"), "A quiet place.", OwnerType.UNKNOWN),
    ],
)
def test_owner_type_from_chips_and_body(
    chips: tuple[str, ...], description: str, expected: OwnerType
) -> None:
    listing = to_listing(row(), detail(*chips, description=description))
    assert listing.owner_type is expected


def test_a_search_row_alone_has_an_unknown_owner_type() -> None:
    assert to_listing(row(), None).owner_type is OwnerType.UNKNOWN


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("detail_apartment.html", OwnerType.PROPERTY_MANAGER),  # application fee chip
        ("detail_complex.html", OwnerType.PROPERTY_MANAGER),  # "On-Site Management"
    ],
)
def test_the_captured_postings_owner_types(name: str, expected: OwnerType) -> None:
    listing = to_listing(row(), parse_detail(page(name)))
    assert listing.owner_type is expected
