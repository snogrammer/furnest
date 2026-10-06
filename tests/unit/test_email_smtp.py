"""Building messages, classifying SMTP failures and choosing the connection type."""

import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, ClassVar, Self

import pytest

from furnest.config import SmtpSecurity, SmtpSettings
from furnest.email.smtp import SmtpSender, build_message, is_permanent

PASSWORD = "secret"  # noqa: S105 - fake secret under test


def _message(**overrides: Any) -> EmailMessage:  # noqa: ANN401
    fields: dict[str, Any] = {
        "from_address": "furnest@example.com",
        "from_name": None,
        "to": "landlord@example.com",
        "subject": "Furnished rental",
        "body": "Hello!",
        "message_id": "<abc@example.com>",
    }
    return build_message(**(fields | overrides))


def test_build_message_sets_headers_and_body() -> None:
    message = _message()
    assert message["From"] == "furnest@example.com"
    assert message["To"] == "landlord@example.com"
    assert message["Subject"] == "Furnished rental"
    assert message["Message-ID"] == "<abc@example.com>"
    assert message["Date"].datetime.utcoffset().total_seconds() == 0
    assert message.get_content().strip() == "Hello!"


def test_build_message_with_display_name_and_unicode() -> None:
    message = _message(from_name="Renter Résumé", subject="Café ☕", body="Déjà vu — ok")
    (sender,) = message["From"].addresses
    assert (sender.display_name, sender.addr_spec) == ("Renter Résumé", "furnest@example.com")
    assert message["Subject"] == "Café ☕"
    assert message.get_content_charset() == "utf-8"
    assert message.get_content().strip() == "Déjà vu — ok"
    # Non-ASCII headers go out RFC 2047-encoded.
    assert b"Subject: =?utf-8?" in message.as_bytes()


@pytest.mark.parametrize(
    ("error", "permanent"),
    [
        (smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"no such user")}), True),
        (smtplib.SMTPRecipientsRefused({"a@example.com": (450, b"mailbox busy")}), False),
        (smtplib.SMTPSenderRefused(553, b"not allowed", "furnest@example.com"), True),
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), True),
        (smtplib.SMTPDataError(554, b"rejected"), True),
        (smtplib.SMTPDataError(451, b"try later"), False),
        (smtplib.SMTPServerDisconnected("gone"), False),
        (smtplib.SMTPNotSupportedError("no SMTPUTF8"), True),
        (ConnectionRefusedError(), False),
        (TimeoutError(), False),
        (UnicodeEncodeError("ascii", "é", 0, 1, "bad"), True),
    ],
)
def test_is_permanent(error: BaseException, permanent: bool) -> None:
    assert is_permanent(error) is permanent


class FakeSmtp:
    """Records what SmtpSender does with a connection."""

    instances: ClassVar[list["FakeSmtp"]] = []

    def __init__(self, host: str, port: int, *, timeout: float, **kwargs: object) -> None:
        self.host, self.port, self.timeout, self.kwargs = host, port, timeout, kwargs
        self.calls: list[str] = []
        self.login_args: tuple[str, str] | None = None
        self.sent: list[EmailMessage] = []
        type(self).instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, *, context: ssl.SSLContext) -> None:
        assert isinstance(context, ssl.SSLContext)
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append("login")
        self.login_args = (user, password)

    def send_message(self, message: EmailMessage) -> None:
        self.calls.append("send")
        self.sent.append(message)


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> dict[str, type[FakeSmtp]]:
    class Plain(FakeSmtp):
        instances: ClassVar[list[FakeSmtp]] = []

    class Ssl(FakeSmtp):
        instances: ClassVar[list[FakeSmtp]] = []

    monkeypatch.setattr(smtplib, "SMTP", Plain)
    monkeypatch.setattr(smtplib, "SMTP_SSL", Ssl)
    return {"plain": Plain, "ssl": Ssl}


def _settings(**overrides: Any) -> SmtpSettings:  # noqa: ANN401
    fields: dict[str, Any] = {"host": "smtp.example.com", "timeout_seconds": 7.5}
    return SmtpSettings(**(fields | overrides))


def test_starttls_with_login(fakes: dict[str, type[FakeSmtp]]) -> None:
    settings = _settings(username="user@example.com", password=PASSWORD, port=587)
    SmtpSender(settings).send(_message())
    (conn,) = fakes["plain"].instances
    assert (conn.host, conn.port, conn.timeout) == ("smtp.example.com", 587, 7.5)
    assert conn.calls == ["ehlo", "starttls", "ehlo", "login", "send", "quit"]
    assert conn.login_args == ("user@example.com", PASSWORD)
    assert not fakes["ssl"].instances


def test_ssl_uses_implicit_tls(fakes: dict[str, type[FakeSmtp]]) -> None:
    SmtpSender(_settings(security=SmtpSecurity.SSL, port=465)).send(_message())
    (conn,) = fakes["ssl"].instances
    assert isinstance(conn.kwargs["context"], ssl.SSLContext)
    assert conn.timeout == 7.5
    assert conn.calls == ["ehlo", "send", "quit"]
    assert not fakes["plain"].instances


def test_plain_connection_without_login(fakes: dict[str, type[FakeSmtp]]) -> None:
    SmtpSender(_settings(security=SmtpSecurity.NONE, port=25)).send(_message())
    (conn,) = fakes["plain"].instances
    assert conn.calls == ["ehlo", "send", "quit"]
    assert conn.login_args is None


def test_login_without_password_sends_empty_string(fakes: dict[str, type[FakeSmtp]]) -> None:
    SmtpSender(_settings(security=SmtpSecurity.NONE, username="relay")).send(_message())
    assert fakes["plain"].instances[0].login_args == ("relay", "")


def test_sender_needs_a_host() -> None:
    with pytest.raises(ValueError, match="SMTP_HOST"):
        SmtpSender(SmtpSettings())
