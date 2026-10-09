"""Environment settings, ``.env.example`` coverage and runtime override precedence."""

import re
import subprocess
import sys
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from furnest.config import (
    INQUIRY_MODE,
    RUNTIME_SETTINGS,
    Environment,
    GeocodingProvider,
    ImapSettings,
    InMemorySettingsStore,
    InquiryMode,
    LlmProvider,
    LogLevel,
    RuntimeSettings,
    Settings,
    SmtpSecurity,
    SmtpSettings,
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
    assert settings.discord.request_timeout_seconds == 10.0
    assert settings.discord.max_retries == 3
    assert settings.sentry.dsn is None


def test_env_vars_are_parsed_and_coerced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_SECURITY", "ssl")
    monkeypatch.setenv("SOURCES_BROWSER_ENABLED", "true")
    monkeypatch.setenv("SOURCES_RESPECT_ROBOTS", "false")
    monkeypatch.setenv("SOURCES_MAX_RETRIES", "5")
    monkeypatch.setenv("SOURCES_REMOVE_AFTER_MISSED_RUNS", "4")
    monkeypatch.setenv("SOURCES_REMOVE_AFTER_HOURS", "72")
    monkeypatch.setenv("SOURCES_RUN_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("SOURCES_MAX_RESULTS", "40")
    monkeypatch.setenv("BASE_URL", "https://furnest.example.com")
    monkeypatch.setenv("PIPELINE_SNOOZE_HOURS", "24")
    monkeypatch.setenv("PIPELINE_UNSNOOZE_INTERVAL_MINUTES", "5")

    settings = Settings()

    assert settings.app.environment is Environment.DEVELOPMENT
    assert settings.logging.level is LogLevel.DEBUG
    assert settings.smtp.port == 2525
    assert settings.smtp.security is SmtpSecurity.SSL
    assert settings.sources.browser_enabled is True
    assert settings.sources.respect_robots is False
    assert settings.sources.max_retries == 5
    assert settings.sources.remove_after_missed_runs == 4
    assert settings.sources.remove_after_hours == 72
    assert settings.sources.run_timeout_seconds == 600
    assert settings.sources.max_results == 40
    assert str(settings.app.base_url) == "https://furnest.example.com/"
    assert settings.pipeline.snooze_hours == 24
    assert settings.pipeline.unsnooze_interval_minutes == 5


def test_sources_adapters_json_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SOURCES_ADAPTERS",
        '{"example_source": {"enabled": true, "interval_minutes": 30, "api_key": "k"}}',
    )

    adapters = Settings().sources.adapters

    assert set(adapters) == {"example_source"}
    overrides = adapters["example_source"]
    assert overrides.enabled is True
    assert overrides.interval_minutes == 30
    assert overrides.api_key is not None
    assert overrides.api_key.get_secret_value() == "k"
    assert overrides.jitter_seconds is None
    assert overrides.model_fields_set == {"enabled", "interval_minutes", "api_key"}


@pytest.mark.parametrize(
    "value",
    [
        '{"example_source": {"enabeld": true}}',
        '{"Example-Source": {"enabled": true}}',
        '{"example_source": {"interval_minutes": 0}}',
        "not json",
    ],
)
def test_invalid_sources_adapters_are_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SOURCES_ADAPTERS", value)

    with pytest.raises(ValueError, match=r"SOURCES_ADAPTERS|adapters|error"):
        Settings()


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
        ("DISCORD_MAX_RETRIES", "-1"),
        ("DISCORD_REQUEST_TIMEOUT_SECONDS", "0"),
        ("DISCORD_DIGEST_TIME", "25:00"),
        ("DISCORD_DIGEST_TOP", "0"),
        ("DISCORD_ALERT_AFTER_FAILURES", "-1"),
        ("TIMEZONE", "Mars/Olympus_Mons"),
    ],
)
def test_invalid_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings()


def test_discord_client_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_REQUEST_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("DISCORD_MAX_RETRIES", "0")

    discord = Settings().discord

    assert discord.request_timeout_seconds == 2.5
    assert discord.max_retries == 0


def test_digest_and_alert_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_DIGEST_TIME", "08:30")
    monkeypatch.setenv("DISCORD_DIGEST_TOP", "3")
    monkeypatch.setenv("DISCORD_ALERT_AFTER_FAILURES", "0")
    monkeypatch.setenv("TIMEZONE", "America/Chicago")

    settings = Settings()

    assert settings.discord.digest_time == time(8, 30)
    assert settings.discord.digest_top == 3
    assert settings.discord.alert_after_failures == 0
    assert settings.app.tzinfo == ZoneInfo("America/Chicago")


def test_digest_is_off_and_alerts_on_by_default() -> None:
    discord = Settings().discord
    assert discord.digest_time is None
    assert discord.alert_after_failures == 3


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


def test_llm_extractor_is_off_by_default() -> None:
    llm = Settings().llm

    assert llm.provider is LlmProvider.NONE
    assert llm.openai_compatible_enabled is False
    assert llm.timeout_seconds == 30


def test_openai_compatible_llm_is_enabled_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "llama3.1")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "12.5")

    llm = Settings().llm

    assert llm.openai_compatible_enabled is True
    assert llm.timeout_seconds == 12.5


@pytest.mark.parametrize("missing", ["LLM_BASE_URL", "LLM_MODEL"])
def test_openai_compatible_llm_requires_base_url_and_model(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.com/v1")
    monkeypatch.setenv("LLM_MODEL", "model-name")
    monkeypatch.delenv(missing)

    with pytest.raises(ValidationError, match="requires LLM_BASE_URL and LLM_MODEL"):
        Settings()


def test_llm_timeout_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValidationError):
        Settings()


def test_geocoding_is_off_by_default() -> None:
    geocoding = Settings().geocoding

    assert geocoding.provider is GeocodingProvider.NONE
    assert geocoding.requests_per_minute == 60
    assert geocoding.miss_ttl_days == 30


def test_nominatim_geocoding_loads_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEOCODING_PROVIDER", "nominatim")
    monkeypatch.setenv("GEOCODING_BASE_URL", "https://geocoder.example.com")
    monkeypatch.setenv("GEOCODING_USER_AGENT", "furnest (admin@example.com)")
    monkeypatch.setenv("GEOCODING_REQUESTS_PER_MINUTE", "120")

    geocoding = Settings().geocoding

    assert geocoding.provider is GeocodingProvider.NOMINATIM
    assert geocoding.requests_per_minute == 120


@pytest.mark.parametrize("missing", ["GEOCODING_BASE_URL", "GEOCODING_USER_AGENT"])
def test_nominatim_geocoding_requires_base_url_and_user_agent(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setenv("GEOCODING_PROVIDER", "nominatim")
    monkeypatch.setenv("GEOCODING_BASE_URL", "https://geocoder.example.com")
    monkeypatch.setenv("GEOCODING_USER_AGENT", "furnest (admin@example.com)")
    monkeypatch.delenv(missing)

    with pytest.raises(
        ValidationError, match="requires GEOCODING_BASE_URL and GEOCODING_USER_AGENT"
    ):
        Settings()


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


def test_worker_heartbeat_interval_must_be_below_max_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "120")
    monkeypatch.setenv("WORKER_HEARTBEAT_MAX_AGE_SECONDS", "120")

    with pytest.raises(ValidationError, match="must be below WORKER_HEARTBEAT_MAX_AGE_SECONDS"):
        Settings()


def test_discord_public_key_must_be_hex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", "not-a-key")

    with pytest.raises(ValidationError, match="DISCORD_PUBLIC_KEY"):
        Settings()


def test_discord_interaction_max_age(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().discord.interaction_max_age_seconds == 300
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", "aB" * 32)
    monkeypatch.setenv("DISCORD_INTERACTION_MAX_AGE_SECONDS", "60")

    discord = Settings().discord

    assert discord.public_key == "aB" * 32
    assert discord.interaction_max_age_seconds == 60


def test_smtp_defaults_and_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    smtp = SmtpSettings()
    assert smtp.from_name is None
    assert smtp.timeout_seconds == 30.0
    assert smtp.configured is False

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    assert SmtpSettings().configured is False  # no From address yet
    monkeypatch.setenv("SMTP_FROM_ADDRESS", "furnest@example.com")
    monkeypatch.setenv("SMTP_FROM_NAME", "furnest")
    monkeypatch.setenv("SMTP_TIMEOUT_SECONDS", "5")
    smtp = SmtpSettings()
    assert smtp.configured is True
    assert smtp.from_name == "furnest"
    assert smtp.timeout_seconds == 5.0


def test_imap_is_off_by_default() -> None:
    imap = ImapSettings()
    assert imap.enabled is False
    assert imap.configured is False
    assert imap.security is SmtpSecurity.SSL
    assert imap.lookback_days == 14


def test_imap_enabled_requires_a_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAP_ENABLED", "true")
    monkeypatch.setenv("IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_USERNAME", "furnest@example.com")
    with pytest.raises(ValidationError, match="IMAP_ENABLED=true requires"):
        ImapSettings()

    monkeypatch.setenv("IMAP_PASSWORD", "imap-password")
    monkeypatch.setenv("IMAP_SECURITY", "starttls")
    imap = ImapSettings()
    assert imap.enabled is True
    assert imap.configured is True
    assert imap.security is SmtpSecurity.STARTTLS


def test_readme_config_table_is_current() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_config_docs.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
