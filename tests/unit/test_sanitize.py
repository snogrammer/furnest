"""The fixture sanitizer: emails, phones, names, addresses and coordinates.

Every input below is made up; the point is that each would be personal data in a
real capture and must come out as a placeholder.
"""

import json
from typing import Any

import pytest

from furnest.sources.sanitize import (
    ADDRESS_PLACEHOLDER,
    EMAIL_PLACEHOLDER,
    NAME_PLACEHOLDER,
    PHONE_PLACEHOLDER,
    sanitize,
)

pytestmark = pytest.mark.unit

JSON = "application/json; charset=utf-8"
HTML = "text/html; charset=utf-8"
TEXT = "text/plain"


def clean(text: str, **kwargs: Any) -> str:  # noqa: ANN401
    return sanitize(text, TEXT, **kwargs).text


def clean_json(data: object, **kwargs: Any) -> Any:  # noqa: ANN401
    return json.loads(sanitize(json.dumps(data), JSON, **kwargs).text)


# --- Free text ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    ["jane.doe@gmail.com", "j+rent@mail.co.uk", "LANDLORD_1@Some-Host.io"],
)
def test_emails_are_replaced(email: str) -> None:
    assert clean(f"write to {email}.") == f"write to {EMAIL_PLACEHOLDER}."


@pytest.mark.parametrize(
    "kept", ["me@example.com", "ops@mail.example.org", "a@b.example", "logo@2x.png"]
)
def test_placeholder_domains_and_image_names_are_kept(kept: str) -> None:
    assert clean(kept) == kept


@pytest.mark.parametrize(
    "phone",
    [
        "(303) 555-1234",
        "(303)555-1234",
        "303-555-1234",
        "303.555.1234",
        "303 555 1234",
        "+1 303-555-1234",
        "1-303-555-1234",
    ],
)
def test_phone_numbers_are_replaced(phone: str) -> None:
    assert clean(f"call {phone} now") == f"call {PHONE_PLACEHOLDER} now"


def test_tel_links_keep_their_scheme() -> None:
    page = '<a href="tel:+13035551234">x</a> <a href="tel:303-555-1234">y</a>'

    assert clean(page) == (
        f'<a href="tel:{PHONE_PLACEHOLDER}">x</a> <a href="tel:{PHONE_PLACEHOLDER}">y</a>'
    )


@pytest.mark.parametrize(
    "kept", ["listing 3035551234", "2026-10-05", "id ab-303-555-1234", "$1,250.00/mo"]
)
def test_ids_dates_and_prices_are_not_phones(kept: str) -> None:
    assert clean(kept) == kept


@pytest.mark.parametrize(
    "address",
    [
        "1234 Main St",
        "1234 W Colfax Ave",
        "77 Old Mill Road",
        "5-12 Harbor View Blvd.",
        "1234 Main St, Apt 5B",
        "9 Elm Ct #12",
        "410 E 5th Street",
    ],
)
def test_street_addresses_are_replaced(address: str) -> None:
    assert clean(f"at {address} near") == f"at {ADDRESS_PLACEHOLDER} near"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "/homedetails/70-Rainey-St-2605-Exampleville-XX-00001/1_zpid/",
            "/homedetails/123-Example-St-2605-Exampleville-XX-00001/1_zpid/",
        ),
        (
            "/homedetails/2250-Ridgepoint-Dr-APT-1303-Exampleville-XX-00001/2_zpid/",
            "/homedetails/123-Example-St-APT-1303-Exampleville-XX-00001/2_zpid/",
        ),
        ("/home/410-E-5th-Street?x=1", "/home/123-Example-St?x=1"),
        ("/home/1500-E-Old-Mill-Ln/", "/home/123-Example-St/"),
    ],
)
def test_street_addresses_in_url_slugs_are_replaced(path: str, expected: str) -> None:
    url = f"https://www.example.com{path}"

    assert clean(url) == f"https://www.example.com{expected}"


def test_slugs_that_are_not_addresses_are_kept() -> None:
    url = "https://www.example.com/b/2-Bedroom-Apartments/77-units/"

    assert clean(url) == url


def test_redacted_terms_are_replaced_whole_word_any_case() -> None:
    text = "Ask for ROSALIND or rosalind.  Rosalinda is someone else."

    assert clean(text, redact=["Rosalind"]) == (
        f"Ask for {NAME_PLACEHOLDER} or {NAME_PLACEHOLDER}.  Rosalinda is someone else."
    )


def test_short_terms_are_ignored() -> None:
    assert clean("Al is at the AL office", redact=["Al", " "]) == "Al is at the AL office"


def test_the_longest_name_wins() -> None:
    assert clean("Jane Smith and Smith", redact=["Smith", "Jane Smith"]) == (
        f"{NAME_PLACEHOLDER} and {NAME_PLACEHOLDER}"
    )


def test_counts_by_category() -> None:
    result = sanitize("a@b.io c@d.io 303-555-1234 1 Main St Bob", TEXT, redact=["Bob"])

    assert result.counts == {"address": 1, "email": 2, "name": 1, "phone": 1}
    assert result.total == 5


def test_placeholders_are_not_counted_again() -> None:
    text = f"{EMAIL_PLACEHOLDER} {PHONE_PLACEHOLDER} {ADDRESS_PLACEHOLDER} {NAME_PLACEHOLDER}"

    result = sanitize(text, TEXT, redact=[NAME_PLACEHOLDER])

    assert result.text == text
    assert result.counts == {}


def test_unknown_content_types_get_the_text_pass() -> None:
    assert sanitize("x@y.io", "").text == EMAIL_PLACEHOLDER


# --- JSON ---------------------------------------------------------------------------

LISTING: dict[str, Any] = {
    "id": "4821_1",
    "title": "Sunny studio",
    "amenities": [{"name": "Pool"}, {"name": "Washer"}],
    "city": {"name": "Exampleville"},
    "landlord": {
        "name": "Rosalind Quill",
        "firstName": "Rosalind",
        "phoneNumber": "(303) 555-1234",
        "contactEmail": "rq@mail.io",
        "emailVerified": True,
        "phoneCount": 2,
    },
    "reviews": [{"reviewerName": "Theo Brandt", "text": "Theo Brandt says: great"}],
    "host": {"profile": {"name": "Profile name, kept: not directly under host"}},
    "phone": 3035551234,
    "address": {"streetAddress": "1234 Main St", "addressLocality": "Exampleville"},
    "fullAddress": "1234 Main St, Exampleville, XX",
    "lat": 39.739236,
    "longitude": "-104.990251",
    "geo": {"lat": 39.74, "lon": "not a number", "lng": None},
    "description": "Contact Rosalind Quill at rq@mail.io.",
}


def test_json_keys_say_what_to_replace() -> None:
    out = clean_json(LISTING)

    assert out["id"] == "4821_1"
    assert out["amenities"] == [{"name": "Pool"}, {"name": "Washer"}]
    assert out["city"] == {"name": "Exampleville"}
    assert out["landlord"] == {
        "name": NAME_PLACEHOLDER,
        "firstName": NAME_PLACEHOLDER,
        "phoneNumber": PHONE_PLACEHOLDER,
        "contactEmail": EMAIL_PLACEHOLDER,
        "emailVerified": True,
        "phoneCount": 2,
    }
    assert out["reviews"] == [
        {"reviewerName": NAME_PLACEHOLDER, "text": f"{NAME_PLACEHOLDER} says: great"}
    ]
    assert out["host"] == LISTING["host"]
    assert out["phone"] == PHONE_PLACEHOLDER
    assert out["address"] == {
        "streetAddress": ADDRESS_PLACEHOLDER,
        "addressLocality": "Exampleville",
    }
    assert out["fullAddress"] == ADDRESS_PLACEHOLDER
    assert out["description"] == f"Contact {NAME_PLACEHOLDER} at {EMAIL_PLACEHOLDER}."


def test_coordinates_are_rounded_keeping_their_type() -> None:
    out = clean_json(LISTING)

    assert out["lat"] == 39.74
    assert out["longitude"] == "-104.99"
    assert out["geo"] == {"lat": 39.74, "lon": "not a number", "lng": None}


def test_coordinates_outside_json_are_rounded() -> None:
    html = (
        '<div id="map" data-latitude="38.478259" data-longitude="-122.725574"'
        ' data-accuracy="10"></div>\n'
        '<script>init({\'location\': {"lat":37.8085,"lon":-122.2668,"radius":3}});</script>'
    )

    first = sanitize(html, HTML)

    assert 'data-latitude="38.48" data-longitude="-122.73" data-accuracy="10"' in first.text
    assert '"lat":37.81,"lon":-122.27,"radius":3' in first.text
    assert first.counts == {"coordinate": 4}
    assert sanitize(first.text, HTML).counts == {}


def test_json_learns_names_for_free_text_elsewhere() -> None:
    data = [{"description": "Ask Rosalind Quill"}, {"owner": {"name": "Rosalind Quill"}}]

    assert clean_json(data)[0]["description"] == f"Ask {NAME_PLACEHOLDER}"


def test_schema_org_person_names_are_replaced() -> None:
    data = {"@type": "Person", "name": "Rosalind Quill", "telephone": "303-555-1234"}

    assert clean_json(data) == {
        "@type": "Person",
        "name": NAME_PLACEHOLDER,
        "telephone": PHONE_PLACEHOLDER,
    }


def test_empty_values_are_left_alone() -> None:
    assert clean_json({"email": "", "landlordName": ""}) == {"email": "", "landlordName": ""}


def test_json_output_is_stable() -> None:
    first = sanitize(json.dumps(LISTING), JSON)
    second = sanitize(first.text, JSON)

    assert first.counts == {"address": 2, "coordinate": 2, "email": 2, "name": 5, "phone": 2}
    assert second.text == first.text
    assert second.counts == {}
    assert first.text.endswith("}\n")


def test_invalid_json_gets_the_text_pass() -> None:
    assert sanitize('{"email": "x@y.io"', JSON).text == f'{{"email": "{EMAIL_PLACEHOLDER}"'


# --- HTML ---------------------------------------------------------------------------


def test_html_embedded_json_is_walked_and_names_reach_the_page() -> None:
    ld = {"@type": "Person", "name": "Rosalind Quill", "url": "</script><b>"}
    # Pages escape "</" inside script JSON, as the sanitizer does when writing it back.
    ld_json = json.dumps(ld).replace("</", "<\\/")
    page = (
        "<html><p>Hosted by Rosalind Quill, 303-555-1234</p>"
        f'<script type="application/ld+json">{ld_json}</script>'
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props": {"owner": {"name": "Theo Brandt", "email": "t@b.io"}}}</script>'
        "<script>var x = 1;</script></html>"
    )

    result = sanitize(page, HTML)

    assert "Rosalind" not in result.text
    assert "Theo" not in result.text
    assert "t@b.io" not in result.text
    assert f"Hosted by {NAME_PLACEHOLDER}, {PHONE_PLACEHOLDER}" in result.text
    assert '"url": "<\\/script><b>"' in result.text
    assert "<script>var x = 1;</script>" in result.text
    assert sanitize(result.text, HTML).text == result.text


def test_html_with_broken_embedded_json_still_gets_the_text_pass() -> None:
    page = '<script type="application/json">{"email": "x@y.io",</script>'

    assert sanitize(page, HTML).text == (
        f'<script type="application/json">{{"email": "{EMAIL_PLACEHOLDER}",</script>'
    )
