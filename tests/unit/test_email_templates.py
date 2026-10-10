"""Inquiry template rendering: the default template, the context and the sandbox."""

from dataclasses import replace
from datetime import date

import pytest
from pydantic import HttpUrl

from furnest.domain import Address, Contact, Criteria, Money, NormalizedListing, Price
from furnest.email.templates import (
    DEFAULT_BODY,
    DEFAULT_SUBJECT,
    SAMPLE_LEASE_MONTHS,
    SAMPLE_MOVE_IN,
    VARIABLES,
    InquiryContext,
    TemplateRenderError,
    context_for,
    long_date,
    render,
    sample_context,
)


def listing(**overrides: object) -> NormalizedListing:
    fields: dict[str, object] = {
        "source": "example",
        "source_id": "1",
        "url": HttpUrl("https://example.com/l/1"),
        "title": "Furnished 1BR",
    }
    return NormalizedListing.model_validate({**fields, **overrides})


@pytest.fixture
def context() -> InquiryContext:
    return sample_context(None, "A. Person\nexample@example.com")


# --- context ------------------------------------------------------------------


def test_context_for_maps_listing_facts_and_plans() -> None:
    criteria = Criteria(move_in=date(2027, 3, 1), move_out=date(2027, 7, 1))
    ctx = context_for(
        listing(
            price=Price(amount=Money.from_major(1850)),
            address=Address(
                line1="1 Example Ave",
                line2="Unit 2",
                city="Springfield",
                region="ST",
                postal_code="00000",
            ),
            beds=1,
            baths=1.5,
            sqft=600,
            contact=Contact(name="Sam"),
        ),
        criteria,
        "sig",
    )

    assert ctx.listing.title == "Furnished 1BR"
    assert ctx.listing.url == "https://example.com/l/1"
    assert ctx.listing.price == "$1,850.00"
    assert ctx.listing.address == "1 Example Ave, Unit 2, Springfield, ST 00000"
    assert ctx.listing.city == "Springfield"
    assert ctx.listing.size == "1 bd · 1.5 ba"
    assert ctx.contact_name == "Sam"
    assert (ctx.move_in_date, ctx.lease_months) == (date(2027, 3, 1), 4)
    assert ctx.signature == "sig"


def test_context_for_leaves_unknowns_empty() -> None:
    ctx = context_for(listing(), None, "")

    assert ctx.listing.price is None
    assert ctx.listing.address is None
    assert ctx.listing.size is None
    assert ctx.contact_name is None
    assert ctx.move_in_date is None
    assert ctx.lease_months is None


def test_sample_context_prefers_the_criteria_plans() -> None:
    assert sample_context(None, "").move_in_date == SAMPLE_MOVE_IN
    assert sample_context(None, "").lease_months == SAMPLE_LEASE_MONTHS
    criteria = Criteria(move_in=date(2027, 5, 2), move_out=date(2028, 2, 2))
    ctx = sample_context(criteria, "")
    assert (ctx.move_in_date, ctx.lease_months) == (date(2027, 5, 2), 9)


def test_every_documented_variable_renders(context: InquiryContext) -> None:
    for variable, _ in VARIABLES:
        for name in variable.split(" / "):
            render("s", "{{ " + name + " }}", context)


# --- the default template ---------------------------------------------------------


def test_default_template_with_everything_known(context: InquiryContext) -> None:
    email = render(DEFAULT_SUBJECT, DEFAULT_BODY, context)

    assert email.subject == f"Inquiry about {context.listing.title}"
    assert email.body.startswith("Hi Alex,\n\n")
    assert "($2,400.00/month) and I'm very interested." in email.body
    assert "move in around January 15, 2027 for 6 months." in email.body
    assert "https://example.com/listings/sample-1" in email.body
    assert email.body.endswith("A. Person\nexample@example.com\n")


def test_default_template_with_nothing_optional(context: InquiryContext) -> None:
    bare = replace(
        context,
        listing=replace(context.listing, price=None),
        contact_name=None,
        move_in_date=None,
        lease_months=None,
        signature="",
    )

    body = render(DEFAULT_SUBJECT, DEFAULT_BODY, bare).body

    assert body.startswith("Hi,\n\n")
    assert '"Sunny furnished 2BR near the park" and I\'m very interested.\n' in body
    assert "move in" not in body
    assert "None" not in body
    assert body.endswith("https://example.com/listings/sample-1\n")


@pytest.mark.parametrize(
    ("move_in", "months", "expected"),
    [
        (None, 6, "I'm looking for a 6-month stay."),
        (date(2027, 2, 3), None, "move in around February 3, 2027."),
        (date(2027, 2, 3), 1, "February 3, 2027 for 1 month."),
    ],
)
def test_default_template_plans_sentence(
    context: InquiryContext, move_in: date | None, months: int | None, expected: str
) -> None:
    ctx = replace(context, move_in_date=move_in, lease_months=months)

    assert expected in render(DEFAULT_SUBJECT, DEFAULT_BODY, ctx).body


def test_long_date() -> None:
    assert long_date(date(2027, 1, 5)) == "January 5, 2027"


def test_migration_seeds_the_same_default() -> None:
    from importlib import import_module

    migration = import_module("furnest.db.migrations.versions.0012_email_templates")
    assert (migration.DEFAULT_SUBJECT, migration.DEFAULT_BODY) == (DEFAULT_SUBJECT, DEFAULT_BODY)


# --- rendering rules -----------------------------------------------------------


def test_subject_is_folded_onto_one_line(context: InquiryContext) -> None:
    ctx = replace(context, listing=replace(context.listing, title="Nice\r\nBcc: x@example.com"))

    subject = render("About {{ listing.title }}\n", "b", ctx).subject

    assert subject == "About Nice Bcc: x@example.com"


def test_output_is_not_html_escaped(context: InquiryContext) -> None:
    ctx = replace(context, signature="<Ann & Bo>")

    assert render("s", "{{ signature }}", ctx).body == "<Ann & Bo>"


@pytest.mark.parametrize(
    ("source", "part", "fragment"),
    [
        ("{{ listing.titel }}", "body", "titel"),
        ("{{ nope }}", "body", "nope"),
        ("{% if %}", "body", "line 1"),
        ("ok\n{{ 1 / 0 }}", "body", "division"),
        ("{{ move_in_date.strftime(1) }}", "body", ""),
    ],
)
def test_errors_name_the_part(
    context: InquiryContext, source: str, part: str, fragment: str
) -> None:
    with pytest.raises(TemplateRenderError) as caught:
        render("s", source, context)

    assert caught.value.part == part
    assert fragment in str(caught.value)


def test_subject_errors_name_the_subject(context: InquiryContext) -> None:
    with pytest.raises(TemplateRenderError, match=r"^subject"):
        render("{{ x }}", "b", context)


@pytest.mark.parametrize(
    "source",
    [
        "{{ listing.__class__ }}",
        "{{ ''.__class__.__mro__ }}",
        "{{ cycler.__init__.__globals__ }}",
        "{{ listing.title.__class__.__subclasses__() }}",
        "{{ lipsum.__globals__['os'] }}",
        "{% set x = [] %}{{ x.append(1) }}",
        "{% set d = {} %}{{ d.update(a=1) }}",
        "{{ move_in_date.__reduce__() }}",
    ],
)
def test_sandbox_blocks_unsafe_access(context: InquiryContext, source: str) -> None:
    with pytest.raises(TemplateRenderError, match=r"unsafe|undefined|no attribute"):
        render("s", source, context)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        pytest.param("{{ 'x' * 10**10 }}", "numbers are limited", id="huge-literal"),
        pytest.param("{{ 'xx' * 99999 }}", "results are limited", id="long-repeat"),
        pytest.param("{{ [1, 2] * 60000 }}", "results are limited", id="long-list"),
        pytest.param("{{ 2 ** 100 }}", "exponents are limited", id="power"),
        pytest.param("{{ 999 * 999 }}", "numbers are limited", id="big-product"),
        pytest.param("{{ '9999999' | int }}", "numbers are limited", id="int-filter"),
        pytest.param("{{ '1e9' | float }}", "numbers are limited", id="float-filter"),
        pytest.param("{% for i in range(5000) %}{% endfor %}", "range", id="range"),
        pytest.param(
            "{% for i in range(999) %}{% for j in range(999) %}x{% endfor %}{% endfor %}",
            "renders to more than",
            id="output",
        ),
        pytest.param("{{ lipsum(99999) }}", "undefined", id="lipsum"),
    ],
)
def test_rendering_is_bounded(context: InquiryContext, source: str, message: str) -> None:
    with pytest.raises(TemplateRenderError, match=message):
        render("s", source, context)


def test_small_arithmetic_and_loops_still_work(context: InquiryContext) -> None:
    source = (
        "{{ lease_months * 2 }} {{ 2 ** 3 }} {{ '-' * 3 }}"
        "{% for i in range(3) %}{{ i }}{% endfor %}"
    )

    assert render("s", source, context).body == "12 8 ---012"
