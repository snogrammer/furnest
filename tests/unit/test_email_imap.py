"""Reading a received message (``parse_reply``) and opening the IMAP connection."""

import imaplib
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import pytest

from furnest.config import ImapSettings, SmtpSecurity
from furnest.email import imap
from furnest.email.imap import fetched, header, html_to_text, imap_date, open_imap, parse_reply

INQUIRY = "<inquiry-1@example.com>"


def _reply(**headers: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "Alex Lister <lister@example.com>"
    message["To"] = "furnest@example.com"
    message["Subject"] = "Re: Your listing"
    message["Date"] = "Tue, 06 Oct 2026 14:30:00 -0600"
    message["Message-ID"] = "<reply-1@example.net>"
    for name, value in headers.items():
        del message[name.replace("_", "-")]
        message[name.replace("_", "-")] = value
    return message


def test_a_plain_reply_is_read() -> None:
    message = _reply(In_Reply_To=INQUIRY)
    message.set_content("Yes, it's available.\n")

    reply = parse_reply(bytes(message))

    assert reply.message_id == "<reply-1@example.net>"
    assert reply.references == (INQUIRY,)
    assert reply.from_address == "lister@example.com"
    assert reply.to_address == "furnest@example.com"
    assert reply.subject == "Re: Your listing"
    assert reply.body == "Yes, it's available."
    assert reply.date == datetime(2026, 10, 6, 20, 30, tzinfo=UTC)


def test_references_follow_in_reply_to_without_duplicates() -> None:
    message = _reply(
        In_Reply_To=INQUIRY,
        References=f"<older@example.com> {INQUIRY}",
    )
    message.set_content("ok")

    assert parse_reply(bytes(message)).references == (INQUIRY, "<older@example.com>")


def test_references_alone_are_enough() -> None:
    message = _reply(References=INQUIRY)
    message.set_content("ok")

    assert parse_reply(bytes(message)).references == (INQUIRY,)


def test_the_plain_part_is_preferred_over_html() -> None:
    message = _reply()
    message.set_content("plain text")
    message.add_alternative("<p>html text</p>", subtype="html")

    assert parse_reply(bytes(message)).body == "plain text"


def test_an_html_only_reply_is_turned_into_text() -> None:
    message = _reply()
    message.set_content(
        "<html><style>p{color:red}</style><p>Hi &amp; welcome</p><p>Line<br>two</p>"
        "<script>alert(1)</script></html>",
        subtype="html",
    )

    body = parse_reply(bytes(message)).body

    assert "Hi & welcome" in body
    assert "Line\ntwo" in body
    assert "color" not in body
    assert "alert" not in body


def test_an_encoded_subject_is_decoded() -> None:
    message = _reply(Subject="=?utf-8?q?Re:_caf=C3=A9_near_the_park?=")
    message.set_content("ok")

    assert parse_reply(bytes(message)).subject == "Re: café near the park"


def test_a_missing_message_id_is_derived_from_the_content() -> None:
    message = _reply()
    del message["Message-ID"]
    message.set_content("ok")
    raw = bytes(message)

    first = parse_reply(raw).message_id

    assert first.endswith("@furnest.invalid>")
    assert parse_reply(raw).message_id == first
    assert parse_reply(raw + b"\n").message_id != first


def test_an_overlong_message_id_is_replaced() -> None:
    message = _reply(Message_ID=f"<{'x' * 300}@example.net>")
    message.set_content("ok")

    assert parse_reply(bytes(message)).message_id.endswith("@furnest.invalid>")


@pytest.mark.parametrize("value", ["", "not a date"])
def test_a_missing_or_unreadable_date_is_none(value: str) -> None:
    message = _reply()
    del message["Date"]
    if value:
        message["Date"] = value
    message.set_content("ok")

    assert parse_reply(bytes(message)).date is None


def test_an_unparseable_date_header_is_none() -> None:
    raw = b"From: lister@example.com\r\nDate: Tue, 99 Foo 2026\r\n\r\nok"

    assert parse_reply(raw).date is None


def test_a_header_the_parser_chokes_on_reads_as_empty() -> None:
    class Broken(EmailMessage):
        def __getitem__(self, name: str) -> Any:  # noqa: ANN401
            raise IndexError(name)

    assert header(Broken(), "From") == ""


def test_fetch_responses_without_a_uid_are_skipped() -> None:
    data = [
        (b"1 (UID 7 BODY[] {2}", b"ok"),
        b")",
        (b"2 (FLAGS (\\Seen))", b"no uid"),
        (b"3 (UID 9 BODY[] {0}", None),
    ]

    assert list(fetched(data)) == [("7", b"ok")]


def test_a_date_without_a_zone_is_utc() -> None:
    message = _reply(Date="Tue, 06 Oct 2026 14:30:00 -0000")
    message.set_content("ok")

    assert parse_reply(bytes(message)).date == datetime(2026, 10, 6, 14, 30, tzinfo=UTC)


def test_a_message_without_addresses_or_body() -> None:
    raw = b"Subject: hello\r\nMessage-ID: <m@example.net>\r\nContent-Type: image/png\r\n\r\nxx"

    reply = parse_reply(raw)

    assert reply.from_address is None
    assert reply.to_address is None
    assert reply.body == ""


def test_an_unknown_charset_is_read_as_utf8() -> None:
    raw = (
        b"From: lister@example.com\r\nMessage-ID: <m@example.net>\r\n"
        b"Content-Type: text/plain; charset=no-such-charset\r\n\r\ncaf\xc3\xa9"
    )

    assert parse_reply(raw).body == "café"


def test_long_bodies_are_capped() -> None:
    message = _reply()
    message.set_content("x" * (imap.MAX_BODY_LENGTH + 10))

    assert len(parse_reply(bytes(message)).body) == imap.MAX_BODY_LENGTH


def test_html_to_text_collapses_blank_lines() -> None:
    assert html_to_text("<p>a</p><p></p><p></p><p></p><p>b</p>") == "a\n\nb"


def test_imap_dates_are_english() -> None:
    assert imap_date(datetime(2026, 3, 5, tzinfo=UTC)) == "05-Mar-2026"


# --- open_imap ------------------------------------------------------------------


class _Recorder:
    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self.calls = calls

    def __call__(self, name: str) -> Any:  # noqa: ANN401
        calls = self.calls

        class Fake:
            def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
                calls.append((name, (args, kwargs)))

            def starttls(self, **kwargs: Any) -> None:  # noqa: ANN401
                calls.append(("starttls", kwargs))

        return Fake


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    recorded: list[tuple[str, Any]] = []
    make = _Recorder(recorded)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", make("IMAP4_SSL"))
    monkeypatch.setattr(imaplib, "IMAP4", make("IMAP4"))
    return recorded


def _settings(security: SmtpSecurity) -> ImapSettings:
    return ImapSettings(host="imap.example.com", port=1993, security=security, timeout_seconds=5)


def test_open_imap_uses_implicit_tls(calls: list[tuple[str, Any]]) -> None:
    open_imap(_settings(SmtpSecurity.SSL))

    ((name, (args, kwargs)),) = calls
    assert name == "IMAP4_SSL"
    assert args == ("imap.example.com", 1993)
    assert kwargs["timeout"] == 5
    assert kwargs["ssl_context"] is not None


def test_open_imap_upgrades_with_starttls(calls: list[tuple[str, Any]]) -> None:
    open_imap(_settings(SmtpSecurity.STARTTLS))

    assert [name for name, _ in calls] == ["IMAP4", "starttls"]
    assert calls[1][1]["ssl_context"] is not None


def test_open_imap_plain(calls: list[tuple[str, Any]]) -> None:
    open_imap(_settings(SmtpSecurity.NONE))

    assert [name for name, _ in calls] == ["IMAP4"]


def test_open_imap_needs_a_host() -> None:
    with pytest.raises(ValueError, match="IMAP_HOST"):
        open_imap(ImapSettings())
