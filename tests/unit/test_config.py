"""Environment settings, ``.env.example`` coverage and runtime override precedence."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from furnest.config import (
    INQUIRY_MODE,
    RUNTIME_SETTINGS,
    Environment,
    InMemorySettingsStore,
    InquiryMode,
    LogLevel,
    RuntimeSettings,
    Settings,
    SmtpSecurity,
    env_var_names,
    get_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- Environment parsing ------------------------------------------------------


def test_defaults_load_with_empty_environment() -> None:
    settings = Settings()

    assert settings.app.environment is Environment.PRODUCTION
    assert settings.app.inquiry_mode is InquiryMode.REVIEW
    assert settings.db.busy_timeout_ms == 5000
    assert settings.smtp.port == 587
    assert settings.discord.bot_token is None
    assert settings.sentry.dsn is None


def test_env_vars_are_parsed_and_coerced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_SECURITY", "ssl")
    monkeypatch.setenv("SOURCES_BROWSER_ENABLED", "true")
    monkeypatch.setenv("BASE_URL", "https://furnest.example.com")

    settings = Settings()

    assert settings.app.environment is Environment.DEVELOPMENT
    assert settings.logging.level is LogLevel.DEBUG
    assert settings.smtp.port == 2525
    assert settings.smtp.security is SmtpSecurity.SSL
    assert settings.sources.browser_enabled is True
    assert str(settings.app.base_url) == "https://furnest.example.com/"


def test_dotenv_file_in_working_directory_is_read() -> None:
    Path(".env").write_text("DISCORD_CHANNEL_ID=123\n")

    assert Settings().discord.channel_id == "123"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SMTP_PORT", "0"),
        ("IMAP_PORT", "70000"),
        ("BASE_URL", "not a url"),
        ("INQUIRY_MODE", "sometimes"),
        ("SENTRY_TRACES_SAMPLE_RATE", "1.5"),
        ("SMTP_FROM_ADDRESS", "not-an-email"),
        ("DB_URL", "postgresql://db.example.com/furnest"),
    ],
)
def test_invalid_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings()


def test_secrets_are_not_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "super-secret-value")

    settings = Settings()

    assert "super-secret-value" not in repr(settings)
    assert settings.discord.bot_token is not None
    assert settings.discord.bot_token.get_secret_value() == "super-secret-value"


def test_database_url_defaults_to_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", "/srv/furnest")

    assert Settings().database_url == "sqlite:////srv/furnest/furnest.db"


def test_database_url_prefers_db_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_URL", "sqlite:///other.db")

    assert Settings().database_url == "sqlite:///other.db"


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


# --- .env.example ---------------------------------------------------------------


def test_every_setting_is_documented_in_env_example() -> None:
    example = (REPO_ROOT / ".env.example").read_text()
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", example, flags=re.MULTILINE))

    missing = [name for name in env_var_names() if name not in documented]

    assert missing == []


def test_env_example_has_no_unknown_settings() -> None:
    example = (REPO_ROOT / ".env.example").read_text()
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", example, flags=re.MULTILINE))

    assert documented - set(env_var_names()) == set()


def test_env_example_loads_as_valid_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    Path(".env").write_text((REPO_ROOT / ".env.example").read_text())

    settings = Settings()

    assert settings.app.base_url.host == "furnest.example.com"


# --- Runtime overrides ----------------------------------------------------------


def test_runtime_registry_contains_inquiry_mode() -> None:
    assert RUNTIME_SETTINGS["inquiry_mode"] is INQUIRY_MODE


def test_runtime_falls_back_to_env_default_without_stored_value() -> None:
    runtime = RuntimeSettings(Settings(), InMemorySettingsStore())

    assert runtime.get(INQUIRY_MODE) is InquiryMode.REVIEW
    assert runtime.env_pinned(INQUIRY_MODE) is False


def test_stored_value_overrides_env_default() -> None:
    store = InMemorySettingsStore()
    runtime = RuntimeSettings(Settings(), store)

    runtime.set(INQUIRY_MODE, InquiryMode.AUTO)

    assert store.get("inquiry_mode") == "auto"
    assert runtime.get(INQUIRY_MODE) is InquiryMode.AUTO


def test_explicit_env_var_wins_over_stored_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INQUIRY_MODE", "review")
    runtime = RuntimeSettings(Settings(), InMemorySettingsStore({"inquiry_mode": "auto"}))

    assert runtime.env_pinned(INQUIRY_MODE) is True
    assert runtime.get(INQUIRY_MODE) is InquiryMode.REVIEW


def test_invalid_stored_value_falls_back_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    runtime = RuntimeSettings(Settings(), InMemorySettingsStore({"inquiry_mode": "bogus"}))

    assert runtime.get(INQUIRY_MODE) is InquiryMode.REVIEW
    assert "inquiry_mode" in caplog.text


def test_set_rejects_invalid_values() -> None:
    store = InMemorySettingsStore()
    runtime = RuntimeSettings(Settings(), store)

    with pytest.raises(ValidationError):
        runtime.set(INQUIRY_MODE, "sometimes")  # type: ignore[misc]
    assert store.get("inquiry_mode") is None


def test_reset_restores_env_default() -> None:
    runtime = RuntimeSettings(Settings(), InMemorySettingsStore({"inquiry_mode": "auto"}))

    runtime.reset(INQUIRY_MODE)

    assert runtime.get(INQUIRY_MODE) is InquiryMode.REVIEW
