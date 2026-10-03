"""Session signing, token checks, secret validation and redirect-target sanitising."""

import time

import pytest
from itsdangerous import TimestampSigner, URLSafeTimedSerializer
from pydantic import SecretStr

from furnest.config import AuthSettings
from furnest.web.auth import AuthConfigError, Authenticator, safe_next
from furnest.web.csrf import CsrfSigner

pytestmark = pytest.mark.unit

TOKEN = "unit-test-token-0123456789"  # noqa: S105 - fake secret under test
SECRET = "unit-test-secret-0123456789"  # noqa: S105 - fake secret under test


def _auth(token: str | None = TOKEN, secret: str | None = SECRET, **kw: int) -> AuthSettings:
    return AuthSettings(
        token=SecretStr(token) if token is not None else None,
        session_secret=SecretStr(secret) if secret is not None else None,
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("token", "secret", "name"),
    [
        (None, SECRET, "AUTH_TOKEN"),
        ("short", SECRET, "AUTH_TOKEN"),
        ("change-me-to-a-long-random-token", SECRET, "AUTH_TOKEN"),
        (TOKEN, None, "AUTH_SESSION_SECRET"),
        (TOKEN, "change-me-to-another-long-random-secret", "AUTH_SESSION_SECRET"),
    ],
)
def test_unusable_secrets_are_refused(token: str | None, secret: str | None, name: str) -> None:
    with pytest.raises(AuthConfigError, match=name):
        Authenticator(_auth(token, secret))


def test_token_matches_only_exact_token() -> None:
    authenticator = Authenticator(_auth())

    assert authenticator.token_matches(TOKEN)
    assert not authenticator.token_matches(TOKEN + "x")
    assert not authenticator.token_matches("")
    assert not authenticator.token_matches("ünïcode")


def test_issued_session_verifies() -> None:
    authenticator = Authenticator(_auth())

    assert authenticator.session_valid(authenticator.issue_session())
    assert not authenticator.session_valid(None)
    assert not authenticator.session_valid("")


def test_tampered_session_is_rejected() -> None:
    authenticator = Authenticator(_auth())
    cookie = authenticator.issue_session()
    payload, _, signature = cookie.rpartition(".")

    flipped = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    assert not authenticator.session_valid(f"{payload}.{flipped}")
    assert not authenticator.session_valid("garbage")


def test_session_signed_with_another_secret_is_rejected() -> None:
    other = Authenticator(_auth(secret=SECRET + "-other"))

    assert not Authenticator(_auth()).session_valid(other.issue_session())


def test_rotating_token_invalidates_sessions() -> None:
    cookie = Authenticator(_auth()).issue_session()

    assert not Authenticator(_auth(token=TOKEN + "-rotated")).session_valid(cookie)


def test_validly_signed_but_wrong_payload_is_rejected() -> None:
    serializer = URLSafeTimedSerializer(SECRET, salt="furnest.session")
    authenticator = Authenticator(_auth())

    for payload in ({"v": 1, "tf": "0" * 64}, {"v": 2}, ["list"], "string"):
        assert not authenticator.session_valid(serializer.dumps(payload))


def test_expired_session_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    authenticator = Authenticator(_auth(session_max_age_seconds=60))
    issued_at = int(time.time()) - 61
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: issued_at)
    cookie = authenticator.issue_session()
    monkeypatch.undo()

    assert not authenticator.session_valid(cookie)


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/", "/"),
        ("/listings?page=2", "/listings?page=2"),
        (None, "/"),
        ("", "/"),
        ("https://evil.example.com/", "/"),
        ("//evil.example.com/", "/"),
        ("/\\evil.example.com", "/"),
        ("/ok\\..", "/"),
        ("/a\r\nSet-Cookie: x=1", "/"),
        ("javascript:alert(1)", "/"),
        ("relative/path", "/"),
    ],
)
def test_safe_next(target: str | None, expected: str) -> None:
    assert safe_next(target) == expected


def test_csrf_signer_roundtrip_and_tamper() -> None:
    signer = CsrfSigner(SECRET)
    signed = signer.sign("tok")

    assert signer.unsign(signed) == "tok"
    assert signer.unsign(signed + "x") is None
    assert signer.unsign(None) is None
    assert signer.unsign(CsrfSigner("other-secret-0123456789").sign("tok")) is None
    assert signer.unsign(signer._serializer.dumps(123)) is None
