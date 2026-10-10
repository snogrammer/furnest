"""Inquiry templates: user-editable Jinja2 that renders an inquiry's subject and body.

Templates are written in the dashboard (``/email-templates``) and stored in
``email_templates`` (:mod:`furnest.db.email_template_store`), so they are untrusted
input. They render in an :class:`~jinja2.sandbox.ImmutableSandboxedEnvironment`:
attributes starting with ``_`` (``__class__``, ``__globals__`` and the rest of the
usual escapes) are refused, and so are methods that mutate a list or dict. The
context holds only plain values (:class:`InquiryContext`), never ORM rows or
pydantic models, so there is nothing behind it to reach.

Rendering is also kept cheap, so a template can't tie up or exhaust the web
process: number literals, arithmetic results, ``|int``/``|float`` and ``range()``
are capped at :data:`MAX_RENDER_LENGTH`, ``*`` can't build an oversized string or
list, and rendering stops once the output passes that length.

Undefined names fail (``StrictUndefined``) rather than render as blanks, so a typo
like ``{{ listing.titel }}`` is caught when the template is previewed or saved, not
in a sent email. Optional facts the listing doesn't have are ``None``; templates
guard them with ``{% if %}``. The output is plain text, so nothing is HTML-escaped.

:func:`context_for` builds the context for a real listing (the inquiry flow, #42);
:func:`sample_context` builds one for the editor's live preview.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from jinja2 import StrictUndefined, TemplateError, TemplateSyntaxError, nodes
from jinja2.exceptions import SecurityError
from jinja2.filters import do_float, do_int
from jinja2.runtime import Context
from jinja2.sandbox import ImmutableSandboxedEnvironment
from pydantic import HttpUrl

from furnest.domain import Address, Contact, Criteria, Money, NormalizedListing, Price, beds_baths

DEFAULT_TEMPLATE_NAME = "default"

# Migration 0012 seeds the same text; keep the two in step.
DEFAULT_SUBJECT = "Inquiry about {{ listing.title }}"
DEFAULT_BODY = """\
Hi{% if contact_name %} {{ contact_name }}{% endif %},

I came across your listing "{{ listing.title }}"
{%- if listing.price %} ({{ listing.price }}/month){% endif %} and I'm very interested.
{%- if move_in_date %} I'm looking to move in around {{ move_in_date|long_date }}
{%- if lease_months %} for {{ lease_months }} month{{ "s" if lease_months != 1 }}{% endif %}.
{%- elif lease_months %} I'm looking for a {{ lease_months }}-month stay.{% endif %}

Is it still available? I'd be glad to answer any questions or set up a viewing.

{{ listing.url }}
{% if signature %}
{{ signature }}
{% endif %}"""

# The editor's caps; long enough for any inquiry, short enough to keep renders cheap.
MAX_SUBJECT_LENGTH = 300
MAX_BODY_LENGTH = 20_000
MAX_SIGNATURE_LENGTH = 2_000


def long_date(value: date) -> str:
    """The ``long_date`` filter: ``January 15, 2027``."""
    return f"{value:%B} {value.day}, {value.year}"


# Upper bound for a rendered part, and for any number a template works with.
MAX_RENDER_LENGTH = 100_000
_MAX_RANGE = 1_000
_MAX_EXPONENT = 64


def _bounded_number(value: Any) -> Any:  # noqa: ANN401 - whatever the template computed
    is_number = isinstance(value, int | float) and not isinstance(value, bool)
    if is_number and not abs(value) <= MAX_RENDER_LENGTH:  # also catches nan and inf
        msg = f"numbers are limited to {MAX_RENDER_LENGTH:,}"
        raise SecurityError(msg)
    return value


def _bounded_range(*args: int) -> range:
    span = range(*args)
    if len(span) > _MAX_RANGE:
        msg = f"range() is limited to {_MAX_RANGE:,} items"
        raise SecurityError(msg)
    return span


class _BoundedEnvironment(ImmutableSandboxedEnvironment):
    """The sandbox, with ``*`` and ``**`` checked before and after they run."""

    intercepted_binops = frozenset({"*", "**"})

    def call_binop(
        self,
        context: Context,
        operator: str,
        left: Any,  # noqa: ANN401 - template values
        right: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        if operator == "**" and isinstance(right, int | float) and abs(right) > _MAX_EXPONENT:
            msg = f"exponents are limited to {_MAX_EXPONENT}"
            raise SecurityError(msg)
        # Size a repetition ("x" * n) before building it.
        for sequence, times in ((left, right), (right, left)):
            if (
                isinstance(sequence, str | list | tuple)
                and isinstance(times, int)
                and len(sequence) * times > MAX_RENDER_LENGTH
            ):
                msg = f"results are limited to {MAX_RENDER_LENGTH:,} items"
                raise SecurityError(msg)
        return _bounded_number(super().call_binop(context, operator, left, right))


_env = _BoundedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)
_env.filters["long_date"] = long_date
_env.filters["int"] = lambda *a, **kw: _bounded_number(do_int(*a, **kw))
_env.filters["float"] = lambda *a, **kw: _bounded_number(do_float(*a, **kw))
_env.globals["range"] = _bounded_range
del _env.globals["lipsum"]  # one call can generate megabytes of filler


@dataclass(frozen=True)
class ListingFacts:
    """What a template can say about the listing, as ``listing.<field>``."""

    title: str
    url: str
    source: str
    price: str | None  # monthly, formatted: "$2,400.00"
    address: str | None  # one line: "12 Example St, Unit 3, Springfield, ST 00000"
    city: str | None
    size: str | None  # "2 bd · 1 ba"
    beds: int | None
    baths: float | None
    sqft: int | None
    available_from: date | None
    min_lease_months: int | None
    max_lease_months: int | None


@dataclass(frozen=True)
class InquiryContext:
    """Everything a template can use. :data:`VARIABLES` documents it for the editor."""

    listing: ListingFacts
    contact_name: str | None
    move_in_date: date | None
    lease_months: int | None
    signature: str


# Shown beside the editor: (variable, what it holds).
VARIABLES: tuple[tuple[str, str], ...] = (
    ("listing.title", "Listing title"),
    ("listing.url", "Link to the listing"),
    ("listing.source", "Source name"),
    ("listing.price", "Monthly price, formatted (may be empty)"),
    ("listing.address", "Address on one line (may be empty)"),
    ("listing.city", "City (may be empty)"),
    ("listing.size", "Beds and baths, e.g. 2 bd · 1 ba (may be empty)"),
    ("listing.beds / listing.baths / listing.sqft", "Numbers (may be empty)"),
    ("listing.available_from", "Date the listing is available (may be empty)"),
    ("listing.min_lease_months / listing.max_lease_months", "Lease limits (may be empty)"),
    ("contact_name", "Lister's name (may be empty)"),
    ("move_in_date", "Your move-in date, from the criteria; move_in_date|long_date formats it"),
    ("lease_months", "Your lease length in months, from the criteria (may be empty)"),
    ("signature", "Your signature"),
)


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    body: str


class TemplateRenderError(ValueError):
    """The template doesn't parse, uses an unknown name or tried something the sandbox refuses."""

    def __init__(self, part: str, message: str, line: int | None = None) -> None:
        self.part = part
        self.message = message
        self.line = line
        where = f"{part}, line {line}" if line else part
        super().__init__(f"{where}: {message}")


def _one_line_address(address: Address) -> str | None:
    region = " ".join(part for part in (address.region, address.postal_code) if part)
    parts = (address.line1, address.line2, address.city, region or None)
    return ", ".join(part for part in parts if part) or None


def context_for(
    listing: NormalizedListing, criteria: Criteria | None, signature: str
) -> InquiryContext:
    """The context for an inquiry about ``listing``, with the searcher's plans from ``criteria``."""
    monthly = listing.monthly_price
    return InquiryContext(
        listing=ListingFacts(
            title=listing.title,
            url=str(listing.url),
            source=listing.source,
            price=monthly.format() if monthly else None,
            address=_one_line_address(listing.address),
            city=listing.address.city,
            size=beds_baths(listing.beds, listing.baths),
            beds=listing.beds,
            baths=listing.baths,
            sqft=listing.sqft,
            available_from=listing.available_from,
            min_lease_months=listing.min_lease_months,
            max_lease_months=listing.max_lease_months,
        ),
        contact_name=listing.contact.name,
        move_in_date=criteria.move_in if criteria else None,
        lease_months=_stay_months(criteria),
        signature=signature,
    )


def _stay_months(criteria: Criteria | None) -> int | None:
    """The planned stay in whole months, from the target move-in to move-out."""
    if criteria is None or criteria.move_in is None or criteria.move_out is None:
        return None
    return max(round((criteria.move_out - criteria.move_in).days / 30.4375), 1)


SAMPLE_LISTING = NormalizedListing(
    source="example",
    source_id="sample-1",
    url=HttpUrl("https://example.com/listings/sample-1"),
    title="Sunny furnished 2BR near the park",
    price=Price(amount=Money.from_major(2400)),
    address=Address(line1="123 Example St", city="Springfield", region="ST", postal_code="00000"),
    beds=2,
    baths=1,
    sqft=900,
    available_from=date(2027, 1, 1),
    min_lease_months=3,
    max_lease_months=12,
    contact=Contact(name="Alex"),
)
SAMPLE_MOVE_IN = date(2027, 1, 15)
SAMPLE_LEASE_MONTHS = 6


def sample_context(criteria: Criteria | None, signature: str) -> InquiryContext:
    """The preview context: a made-up listing, and the criteria's plans or sample ones."""
    context = context_for(SAMPLE_LISTING, criteria, signature)
    return InquiryContext(
        listing=context.listing,
        contact_name=context.contact_name,
        move_in_date=context.move_in_date or SAMPLE_MOVE_IN,
        lease_months=context.lease_months or SAMPLE_LEASE_MONTHS,
        signature=signature,
    )


def _render_part(part: str, source: str, context: InquiryContext) -> str:
    try:
        ast = _env.parse(source)
        for const in ast.find_all(nodes.Const):
            _bounded_number(const.value)
        chunks = _env.from_string(ast).generate(
            listing=context.listing,
            contact_name=context.contact_name,
            move_in_date=context.move_in_date,
            lease_months=context.lease_months,
            signature=context.signature,
        )
        rendered: list[str] = []
        length = 0
        for chunk in chunks:
            length += len(chunk)
            if length > MAX_RENDER_LENGTH:
                msg = f"renders to more than {MAX_RENDER_LENGTH:,} characters"
                raise TemplateRenderError(part, msg)
            rendered.append(chunk)
        return "".join(rendered)
    except TemplateRenderError:
        raise
    except TemplateSyntaxError as exc:
        raise TemplateRenderError(part, exc.message or str(exc), exc.lineno) from exc
    except TemplateError as exc:  # UndefinedError and SecurityError are TemplateErrors
        raise TemplateRenderError(part, exc.message or str(exc)) from exc
    except (TypeError, ValueError, ArithmeticError) as exc:
        # e.g. {{ move_in_date.strftime(1) }} or {{ 1 / 0 }}: still the template's fault.
        raise TemplateRenderError(part, str(exc)) from exc


def render(subject: str, body: str, context: InquiryContext) -> RenderedEmail:
    """Render a template. Raises :class:`TemplateRenderError` with the part and line at fault.

    The subject is folded onto one line: it becomes a header, and a newline there
    would let a template (or a scraped title) add headers of its own.
    """
    rendered_subject = " ".join(_render_part("subject", subject, context).split())
    return RenderedEmail(subject=rendered_subject, body=_render_part("body", body, context))
