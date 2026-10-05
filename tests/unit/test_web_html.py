"""render_description: scraped descriptions never reach the page as raw HTML."""

import pytest

from furnest.web.html import render_description


@pytest.mark.parametrize(
    ("dirty", "forbidden"),
    [
        ("<p>Hi</p><script>alert(1)</script>", ["<script", "alert(1)"]),
        ('<img src="x" onerror="alert(1)">', ["<img", "onerror", "alert"]),
        ('<a href="javascript:alert(1)">click</a>', ["<a", "javascript:", "href"]),
        ('<iframe src="https://example.com"></iframe>', ["<iframe", "example.com"]),
        ("<style>body { display: none }</style><p>x</p>", ["<style", "display"]),
        ('<p onclick="steal()" style="color:red">x</p>', ["onclick", "steal", "style"]),
        ("<svg><script>alert(1)</script></svg>", ["<svg", "<script", "alert"]),
        ("<!-- <script>alert(1)</script> --><p>x</p>", ["<!--", "<script", "alert"]),
        ('<form action="/logout"><button>Go</button></form>', ["<form", "<button", "/logout"]),
    ],
)
def test_dangerous_html_is_neutralized(dirty: str, forbidden: list[str]) -> None:
    html = str(render_description(dirty))

    for fragment in forbidden:
        assert fragment not in html


def test_formatting_tags_survive_without_attributes() -> None:
    html = str(
        render_description('<p class="x">One <strong>two</strong></p><ul><li><em>a</em></li></ul>')
    )

    assert html == "<p>One <strong>two</strong></p><ul><li><em>a</em></li></ul>"


def test_plain_text_is_escaped_with_paragraphs_and_line_breaks() -> None:
    html = str(render_description("Line one\nline two\r\n\r\n\nRent < $2,000 & up"))

    assert html == "<p>Line one<br>line two</p><p>Rent &lt; $2,000 &amp; up</p>"


def test_empty_text_renders_nothing() -> None:
    assert str(render_description("  \n\n ")) == ""
