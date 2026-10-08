"""The settings page's view model: every env setting, redaction, integrations, runtime rows."""

from datetime import time
from pathlib import Path

import pytest
from pydantic import AnyUrl, HttpUrl, SecretStr

from furnest.config import (
    AdapterOverrides,
    AppSettings,
    AuthSettings,
    DiscordSettings,
    GeocodingProvider,
    GeocodingSettings,
    ImapSettings,
    InMemorySettingsStore,
    InquiryMode,
    LlmProvider,
    LlmSettings,
    MetricsSettings,
    ReverseImageProvider,
    ReverseImageSettings,
    RuntimeSettings,
    SentrySettings,
    Settings,
    SmtpSettings,
    SourcesSettings,
    WebSettings,
    env_var_names,
)
from furnest.web.settings_view import (
    Origin,
    SettingRow,
    Status,
    build_groups,
    build_integrations,
    build_runtime_rows,
    display,
    mask_url,
)

# Distinct from every env var name, so finding it means a value leaked.
LEAK = "leak-sentinel-7f3a"
FAKE = "fake-credential"


def rows(settings: Settings) -> dict[str, SettingRow]:
    return {row.env_var: row for group in build_groups(settings) for row in group.rows}


def statuses(settings: Settings) -> dict[str, Status]:
    return {item.name: item.status for item in build_integrations(settings)}


# --- env groups ---------------------------------------------------------------


def test_every_env_var_appears_once_in_order() -> None:
    shown = [row.env_var for group in build_groups(Settings()) for row in group.rows]

    assert shown == env_var_names()


def test_explicit_values_are_marked_env_and_the_rest_default() -> None:
    settings = Settings(app=AppSettings(timezone="America/Denver"))

    by_name = rows(settings)

    assert by_name["TIMEZONE"].origin is Origin.ENV
    assert by_name["TIMEZONE"].value == "America/Denver"
    assert by_name["TIMEZONE"].default == "UTC"
    assert by_name["ENVIRONMENT"].origin is Origin.DEFAULT


def test_groups_have_titles_and_docstring_summaries() -> None:
    groups = {group.key: group for group in build_groups(Settings())}

    assert groups["db"].title == "Database"
    assert groups["reverse_image"].title == "Reverse image"
    assert groups["app"].summary is None
    summary = groups["grouping"].summary
    assert summary is not None
    assert summary.startswith("Cross-source grouping")
    assert "\n" not in summary


def test_secrets_show_only_whether_they_are_set() -> None:
    settings = Settings(
        auth=AuthSettings(token=SecretStr(LEAK), session_secret=SecretStr(LEAK)),
        discord=DiscordSettings(bot_token=SecretStr(LEAK)),
        sentry=SentrySettings(dsn=SecretStr(f"https://{LEAK}@sentry.example.com/1")),
    )

    by_name = rows(settings)

    assert by_name["AUTH_TOKEN"].value == "set"
    assert by_name["AUTH_TOKEN"].secret is True
    assert by_name["DISCORD_BOT_TOKEN"].value == "set"
    assert by_name["LLM_API_KEY"].value is None
    assert by_name["LLM_API_KEY"].secret is True
    assert by_name["TIMEZONE"].secret is False
    assert LEAK not in repr(build_groups(settings))


def test_url_passwords_and_adapter_keys_are_redacted() -> None:
    settings = Settings(
        sources=SourcesSettings(
            proxy_url=AnyUrl(f"http://user:{LEAK}@proxy.example.com:8080"),
            adapters={
                "zillow": AdapterOverrides(
                    api_key=SecretStr(LEAK),
                    proxy_url=AnyUrl(f"http://u:{LEAK}@p.example.com"),
                    search_locations=("Denver, CO", "Boulder, CO"),
                    enabled=True,
                    max_results=20,
                )
            },
        )
    )

    by_name = rows(settings)

    assert by_name["SOURCES_PROXY_URL"].value == "http://user:***@proxy.example.com:8080/"
    adapters = by_name["SOURCES_ADAPTERS"].value
    assert adapters is not None
    assert '"api_key": "set"' in adapters
    assert '"proxy_url": "http://u:***@p.example.com/"' in adapters
    assert '"search_locations": "Denver, CO, Boulder, CO"' in adapters
    assert '"enabled": true' in adapters
    assert '"max_results": 20' in adapters
    assert "interval_minutes" not in adapters  # unset fields are left out
    assert LEAK not in repr(build_groups(settings))


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (None, None),
        (True, "true"),
        (False, "false"),
        (InquiryMode.AUTO, "auto"),
        (time(8, 30), "08:30"),
        (["10.0.0.1", "10.0.0.0/8"], "10.0.0.1, 10.0.0.0/8"),
        ([], None),
        ({}, None),
        (Path("/data"), "/data"),
        (5000, "5000"),
        (2.5, "2.5"),
        ("plain", "plain"),
    ],
)
def test_display(value: object, shown: str | None) -> None:
    assert display(value) == shown


def test_display_refuses_unknown_types() -> None:
    with pytest.raises(TypeError, match="no display rule"):
        display(object())


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("http://u:p@h:1/x", "http://u:***@h:1/x"),
        # A user without a password is usually a token.
        ("http://tok@h/", "http://***@h/"),
        ("http://@h/", "http://@h/"),
        ("sqlite:////data/furnest.db", "sqlite:////data/furnest.db"),
        ("furnest/0 (+https://example.com/furnest)", "furnest/0 (+https://example.com/furnest)"),
        ("me@example.com", "me@example.com"),
    ],
)
def test_mask_url(text: str, masked: str) -> None:
    assert mask_url(text) == masked


def test_trusted_proxies_list_is_joined() -> None:
    settings = Settings(web=WebSettings(trusted_proxies=["127.0.0.1", "172.16.0.0/12"]))

    assert rows(settings)["WEB_TRUSTED_PROXIES"].value == "127.0.0.1, 172.16.0.0/12"


# --- integrations ---------------------------------------------------------------


def test_defaults_leave_optional_integrations_off() -> None:
    status = statuses(Settings())

    for name in (
        "Discord notifications",
        "Discord buttons",
        "Email sending (SMTP)",
        "Reply detection (IMAP)",
        "LLM extraction",
        "Geocoding",
        "Reverse image search",
        "Error reporting (Sentry)",
        "Prometheus metrics",
        "Headless browser",
    ):
        assert status[name] is Status.OFF, name
    for name in (
        "Photo hashing",
        "Cross-source grouping",
        "Alternatives finder",
        "Scam and risk signals",
    ):
        assert status[name] is Status.ON, name


def test_fully_configured_integrations_are_on() -> None:
    settings = Settings(
        discord=DiscordSettings(
            bot_token=SecretStr(FAKE), channel_id="1", public_key="a" * 64, digest_time=time(9, 0)
        ),
        smtp=SmtpSettings(host="smtp.example.com", from_address="me@example.com"),
        imap=ImapSettings(
            enabled=True, host="imap.example.com", username="u", password=SecretStr(FAKE)
        ),
        llm=LlmSettings(provider=LlmProvider.ANTHROPIC, model="claude-haiku-5-5"),
        geocoding=GeocodingSettings(
            provider=GeocodingProvider.NOMINATIM,
            base_url=HttpUrl("https://nominatim.example.com"),
            user_agent="furnest-test",
        ),
        reverse_image=ReverseImageSettings(
            provider=ReverseImageProvider.SERPAPI, api_key=SecretStr(FAKE)
        ),
        sentry=SentrySettings(dsn=SecretStr("https://k@sentry.example.com/1")),
        metrics=MetricsSettings(enabled=True),
        sources=SourcesSettings(browser_enabled=True),
    )

    integrations = {item.name: item for item in build_integrations(settings)}

    assert all(item.status is Status.ON for item in integrations.values())
    assert "daily digest at 09:00" in integrations["Discord notifications"].detail
    assert "alerts after 3 failed runs" in integrations["Discord notifications"].detail
    assert "smtp.example.com:587" in integrations["Email sending (SMTP)"].detail
    assert "claude-haiku-5-5" in integrations["LLM extraction"].detail
    assert "nominatim.example.com" in integrations["Geocoding"].detail


def test_discord_without_alerts_or_digest_says_so() -> None:
    settings = Settings(
        discord=DiscordSettings(bot_token=SecretStr(FAKE), channel_id="1", alert_after_failures=0)
    )

    detail = {item.name: item.detail for item in build_integrations(settings)}

    assert "no daily digest" in detail["Discord notifications"]
    assert "no source alerts" in detail["Discord notifications"]


def test_llm_without_a_model_still_reads_well() -> None:
    settings = Settings(llm=LlmSettings(provider=LlmProvider.ANTHROPIC))

    detail = {item.name: item.detail for item in build_integrations(settings)}

    assert detail["LLM extraction"] == "Provider anthropic."


@pytest.mark.parametrize(
    ("settings", "name", "missing"),
    [
        (
            Settings(discord=DiscordSettings(bot_token=SecretStr(FAKE))),
            "Discord notifications",
            "DISCORD_CHANNEL_ID",
        ),
        (
            Settings(discord=DiscordSettings(bot_token=SecretStr(FAKE), channel_id="1")),
            "Discord buttons",
            "DISCORD_PUBLIC_KEY",
        ),
        (Settings(smtp=SmtpSettings(host="smtp.example.com")), "Email sending (SMTP)", "SMTP_FROM"),
        (
            Settings(reverse_image=ReverseImageSettings(provider=ReverseImageProvider.TINEYE)),
            "Reverse image search",
            "REVERSE_IMAGE_API_KEY",
        ),
    ],
)
def test_half_configured_integrations_are_incomplete(
    settings: Settings, name: str, missing: str
) -> None:
    integrations = {item.name: item for item in build_integrations(settings)}

    assert integrations[name].status is Status.INCOMPLETE
    assert missing in integrations[name].detail


def test_features_turned_off_say_which_var() -> None:
    settings = Settings(
        images=Settings().images.model_copy(update={"enabled": False}),
    )

    integrations = {item.name: item for item in build_integrations(settings)}

    assert integrations["Photo hashing"].status is Status.OFF
    assert integrations["Photo hashing"].detail == "IMAGES_ENABLED is false."


# --- runtime settings -------------------------------------------------------------


def test_runtime_rows_show_the_stored_value_and_choices() -> None:
    runtime = RuntimeSettings(Settings(), InMemorySettingsStore({"inquiry_mode": "auto"}))

    (row,) = build_runtime_rows(runtime)

    assert row.key == "inquiry_mode"
    assert row.env_var == "INQUIRY_MODE"
    assert row.value == "auto"
    assert row.pinned is False
    assert row.choices == ("auto", "review")


def test_runtime_row_is_pinned_when_its_env_var_is_set() -> None:
    settings = Settings(app=AppSettings(inquiry_mode=InquiryMode.REVIEW))
    runtime = RuntimeSettings(settings, InMemorySettingsStore({"inquiry_mode": "auto"}))

    (row,) = build_runtime_rows(runtime)

    assert row.pinned is True
    assert row.value == "review"
