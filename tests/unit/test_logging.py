"""Structured logging: JSON output, context binding, and secret redaction."""

import json
import logging
from typing import Any

import pytest
from pydantic import SecretStr

from furnest.config import LogFormat, LoggingSettings, LogLevel, Settings
from furnest.logging import (
    REDACTED,
    Redactor,
    bind_context,
    bound_context,
    configure_logging,
    error_text,
    get_logger,
    register_secrets,
    secret_values,
)

SECRET = "s3cr3t-value-123"  # noqa: S105 - fake secret under test
BEARER = "eyJhbGciOi.payload.sig"


def json_lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def json_settings(level: LogLevel = LogLevel.DEBUG) -> LoggingSettings:
    return LoggingSettings(level=level, format=LogFormat.JSON)


def test_json_lines_carry_level_timestamp_logger_and_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(json_settings())
    bind_context(component="worker")

    with bound_context(job_id=42, source="example-source", request_id="req-1"):
        get_logger("furnest.test").info("scraped", listings=3)
    get_logger("furnest.test").info("after")

    first, second = json_lines(capsys.readouterr().err)
    assert first["event"] == "scraped"
    assert first["level"] == "info"
    assert first["logger"] == "furnest.test"
    assert first["timestamp"].endswith("Z")
    assert first["listings"] == 3
    assert (first["job_id"], first["source"], first["request_id"]) == (
        42,
        "example-source",
        "req-1",
    )
    assert first["component"] == second["component"] == "worker"
    assert "job_id" not in second


def test_secrets_never_appear_in_log_output(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_settings(), secrets=[SECRET])
    log = get_logger("furnest.test")

    log.info("secretstr kwarg", value=SecretStr(SECRET))
    log.info("token kwarg", bot_token=SECRET, smtp_password=SECRET)
    log.info("headers", headers={"Authorization": f"Bearer {BEARER}", "Accept": "text/html"})
    log.info(f"calling api with Authorization: Bearer {BEARER}")
    log.info("fetched", url=f"https://api.example.com/x?api_key={SECRET}")
    log.info("connecting", url=f"smtp://user:{SECRET}@mail.example.com")
    log.info("nested", payload=[{"note": f"configured value {SECRET} leaked"}])
    try:
        raise RuntimeError(f"login failed for token {SECRET}")
    except RuntimeError:
        log.exception("boom")
    logging.getLogger("sqlalchemy.engine").warning("connect args %s", {"password": SECRET})

    err = capsys.readouterr().err
    assert SECRET not in err
    assert BEARER not in err
    lines = json_lines(err)
    assert len(lines) == 9
    assert all(REDACTED in json.dumps(line) for line in lines)
    assert lines[2]["headers"]["Accept"] == "text/html"
    assert "RuntimeError" in lines[7]["exception"]


def test_console_format_is_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(LoggingSettings(format=LogFormat.CONSOLE), secrets=[SECRET])

    get_logger("furnest.test").warning("hello", token=SECRET)

    err = capsys.readouterr().err
    assert "hello" in err
    assert SECRET not in err
    with pytest.raises(json.JSONDecodeError):
        json.loads(err.splitlines()[0])


def test_level_filters_lower_levels(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_settings(LogLevel.WARNING))
    log = get_logger("furnest.test")

    log.info("dropped")
    log.warning("kept")

    assert [line["event"] for line in json_lines(capsys.readouterr().err)] == ["kept"]


def test_reconfiguring_does_not_duplicate_output(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_settings())
    configure_logging(json_settings())

    get_logger("furnest.test").info("once")

    assert len(json_lines(capsys.readouterr().err)) == 1


@pytest.mark.parametrize(
    ("key", "redacted"),
    [
        ("password", True),
        ("bot_token", True),
        ("X-Api-Key", True),
        ("Authorization", True),
        ("Set-Cookie", True),
        ("dsn", True),
        ("session_secret", True),
        ("input_tokens", False),
        ("token_count", False),
        ("listing_id", False),
    ],
)
def test_redaction_by_key(key: str, redacted: bool) -> None:
    result = Redactor().redact({key: "value"})

    assert result[key] == (REDACTED if redacted else "value")


def test_redactor_keeps_scalars_and_ignores_short_secrets() -> None:
    redactor = Redactor(secrets=["abc"])

    assert redactor.redact({"n": 1, "ok": True, "x": None, "s": "abc"}) == {
        "n": 1,
        "ok": True,
        "x": None,
        "s": "abc",
    }
    assert redactor.redact({"password": None}) == {"password": None}


def test_secret_values_collects_set_secrets_only() -> None:
    settings = Settings.model_validate(
        {
            "auth": {"token": "dashboard-token"},
            "smtp": {"password": "smtp-password"},
            "llm": {"api_key": ""},
        }
    )

    assert sorted(secret_values(settings)) == ["dashboard-token", "smtp-password"]


def test_secret_values_include_proxy_url_credentials() -> None:
    settings = Settings.model_validate(
        {
            "sources": {
                "proxy_url": "http://proxyuser:proxy%40pass@proxy.example.com:8080",
                "adapters": {
                    "token_user": {"proxy_url": "https://proxy-token@proxy.example.com"},
                    "no_creds": {"proxy_url": "https://proxy.example.com"},
                },
            }
        }
    )

    assert secret_values(settings) == ["proxy@pass", "proxy-token"]


def test_bare_key_query_parameter_is_redacted() -> None:
    text = Redactor([]).redact_text("GET https://maps.example.com/geocode?q=x&key=abc123")

    assert "abc123" not in text
    assert "q=x" in text


def test_secret_values_include_per_adapter_api_keys() -> None:
    settings = Settings.model_validate(
        {
            "sources": {
                "adapters": {
                    "with_key": {"api_key": "adapter-key"},
                    "empty_key": {"api_key": ""},
                    "no_key": {"enabled": True},
                }
            }
        }
    )

    assert secret_values(settings) == ["adapter-key"]


def test_error_text_removes_configured_secrets_and_credentials() -> None:
    configure_logging(json_settings(), secrets=[SECRET])

    text = error_text(
        RuntimeError(f"proxy http://u:{SECRET}@proxy.example.com failed; token {SECRET}")
    )

    assert text.startswith("RuntimeError: proxy http://")
    assert SECRET not in text
    assert REDACTED in text


def test_secrets_registered_later_are_redacted_too(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_settings(), secrets=[SECRET])
    later = "db-token-98765"

    register_secrets([later, "abc"])  # too short to redact by value, kept out
    get_logger("furnest.test").info("stored", note=f"using {later} and {SECRET}")

    line = json_lines(capsys.readouterr().err)[0]
    assert later not in json.dumps(line)
    assert SECRET not in json.dumps(line)
    assert error_text(RuntimeError(f"bad token {later}")) == f"RuntimeError: bad token {REDACTED}"


def test_add_secrets_keeps_earlier_ones_and_prefers_the_longest() -> None:
    redactor = Redactor(["short-secret"])

    redactor.add_secrets(["short-secret-and-more"])

    assert redactor.redact_text("x short-secret-and-more y") == f"x {REDACTED} y"
    assert redactor.redact_text("x short-secret y") == f"x {REDACTED} y"
