"""Rendering scraped listing descriptions safely.

A description comes from a source we don't control, and may be plain text or HTML.
It is never rendered raw. HTML goes through nh3 with a small allowlist of
formatting tags and no attributes at all, so there are no links, images, styles or
event handlers left, and ``script`` and ``style`` lose their content too. Plain text
is escaped, with blank lines becoming paragraphs and single newlines line breaks.
"""

import re

import nh3
from markupsafe import Markup, escape

ALLOWED_TAGS = frozenset({"p", "br", "ul", "ol", "li", "strong", "em", "b", "i"})
# Dropped along with everything inside them, not just unwrapped.
_DROPPED_WITH_CONTENT = frozenset({"script", "style", "template", "noscript", "textarea"})

_TAG = re.compile(r"</?[a-zA-Z][^>]*>|<!--")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def render_description(text: str) -> Markup:
    """``text`` as safe HTML: sanitized if it is HTML, paragraphs if it is plain text."""
    if _TAG.search(text):
        return Markup(  # noqa: S704 - nh3 output keeps only ALLOWED_TAGS, without attributes
            nh3.clean(
                text,
                tags=set(ALLOWED_TAGS),
                clean_content_tags=set(_DROPPED_WITH_CONTENT),
                attributes={},
                url_schemes=set(),
                link_rel=None,
                strip_comments=True,
            )
        )
    paragraphs = [p.strip() for p in _PARAGRAPH_BREAK.split(text.replace("\r\n", "\n"))]
    return Markup("").join(
        Markup("<p>{}</p>").format(Markup("<br>").join(escape(line) for line in p.split("\n")))
        for p in paragraphs
        if p
    )
