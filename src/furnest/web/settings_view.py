"""What the settings page shows: every env setting, the integrations, and runtime settings.

Env settings are read once per process and can't change while it runs, so the page
only displays them, one table per env group. Each row says whether the value was
set explicitly (env or ``.env``) or is the default. The runtime settings in
:data:`furnest.config.RUNTIME_SETTINGS` are the editable part. A new entry there shows
up as a form with no change here.

Nothing secret reaches the template. A ``SecretStr`` is shown only as "set" or
"not set", and the password in a URL with credentials (a proxy, say) is replaced by
``***``. Values are redacted while still typed, before anything is turned into text.

Integration status is worked out from the same settings the app checks before using
an integration. "incomplete" means some of its settings are present but not all, a
setup the env validators allow but that leaves the integration off.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, get_args
from urllib.parse import urlsplit, urlunsplit

from pydantic import AnyUrl, BaseModel, SecretStr
from pydantic_settings import BaseSettings

from furnest.config import (
    RUNTIME_SETTINGS,
    AdapterOverrides,
    GeocodingProvider,
    LlmProvider,
    ReverseImageProvider,
    RuntimeSetting,
    RuntimeSettings,
    Settings,
    env_groups,
    env_var_name,
)

# What a secret shows instead of its value, and what replaces a URL password.
PRESENT = "set"
MASK = "***"


class Origin(StrEnum):
    ENV = "env"
    DEFAULT = "default"


@dataclass(frozen=True)
class SettingRow:
    env_var: str
    # Display text, already redacted; ``None`` when the setting has no value.
    value: str | None
    origin: Origin
    default: str | None
    secret: bool


@dataclass(frozen=True)
class GroupView:
    key: str
    title: str
    summary: str | None
    rows: list[SettingRow]


class Status(StrEnum):
    ON = "on"
    OFF = "off"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class Integration:
    name: str
    status: Status
    detail: str
    env_vars: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeRow:
    setting: RuntimeSetting[Any]
    env_var: str
    value: str
    # The env var is set, so the stored value is ignored and the form is read-only.
    pinned: bool
    # The allowed values for an enum setting; empty means free text.
    choices: tuple[str, ...]

    @property
    def key(self) -> str:
        return self.setting.key


# --- Env groups ---------------------------------------------------------------


def build_groups(settings: Settings) -> list[GroupView]:
    """One view per env group, fields in declaration order."""
    views = []
    for key, group_cls in env_groups().items():
        group: BaseSettings = getattr(settings, key)
        rows = [
            SettingRow(
                env_var=env_var_name(group_cls, name),
                value=display(getattr(group, name)),
                origin=Origin.ENV if name in group.model_fields_set else Origin.DEFAULT,
                default=display(field.get_default(call_default_factory=True)),
                secret=_is_secret(field.annotation),
            )
            for name, field in group_cls.model_fields.items()
        ]
        views.append(GroupView(key=key, title=_title(key), summary=_summary(group_cls), rows=rows))
    return views


def display(value: object) -> str | None:
    """``value`` as display text with secrets and URL passwords redacted."""
    if value is None:
        return None
    if isinstance(value, SecretStr):
        return PRESENT
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, time):
        return value.isoformat(timespec="minutes")
    if isinstance(value, list | tuple):
        return ", ".join(str(display(item)) for item in value) or None
    if isinstance(value, dict):
        if not value:
            return None
        return json.dumps({str(k): _redacted(v) for k, v in value.items()}, indent=2)
    if isinstance(value, AnyUrl | str):
        return mask_url(str(value))
    if isinstance(value, Path | int | float):
        return str(value)
    msg = f"no display rule for {type(value).__name__}"
    raise TypeError(msg)


def mask_url(text: str) -> str:
    """``text`` with the password of a ``scheme://user:password@host`` URL replaced."""
    if "://" not in text or "@" not in text:
        return text
    parts = urlsplit(text)
    if parts.password is None:
        return text
    userinfo, _, hostinfo = parts.netloc.rpartition("@")
    user = userinfo.split(":", 1)[0]
    return urlunsplit(parts._replace(netloc=f"{user}:{MASK}@{hostinfo}"))


def _redacted(value: object) -> object:
    """A JSON-ready copy of one ``SOURCES_ADAPTERS`` entry with its secrets redacted."""
    if isinstance(value, AdapterOverrides):
        return {
            name: _redacted(getattr(value, name))
            for name in AdapterOverrides.model_fields
            if name in value.model_fields_set
        }
    if isinstance(value, BaseModel):  # pragma: no cover - only AdapterOverrides today
        msg = f"no redaction rule for {type(value).__name__}"
        raise TypeError(msg)
    if isinstance(value, bool | int | float) or value is None:
        return value
    return display(value)


def _is_secret(annotation: object) -> bool:
    return annotation is SecretStr or SecretStr in get_args(annotation)


_TITLES = {"db": "Database", "llm": "LLM", "smtp": "SMTP", "imap": "IMAP"}


def _title(key: str) -> str:
    return _TITLES.get(key, key.replace("_", " ").capitalize())


def _summary(group_cls: type[BaseSettings]) -> str | None:
    """The first paragraph of the group's own docstring, if it has one."""
    doc: str | None = group_cls.__dict__.get("__doc__")
    if not doc:
        return None
    return " ".join(doc.strip().split("\n\n", 1)[0].split())


# --- Integrations -------------------------------------------------------------


def build_integrations(settings: Settings) -> list[Integration]:
    """Each optional integration and whether it is on, off or half configured."""
    discord = settings.discord
    smtp = settings.smtp
    imap = settings.imap
    llm = settings.llm
    geocoding = settings.geocoding
    reverse = settings.reverse_image
    posting = {
        "DISCORD_BOT_TOKEN": discord.bot_token is not None,
        "DISCORD_CHANNEL_ID": bool(discord.channel_id),
    }
    digest = (
        f"daily digest at {discord.digest_time.isoformat(timespec='minutes')}"
        if discord.digest_time is not None
        else "no daily digest"
    )
    alerts = (
        f"source alerts after {discord.alert_after_failures} failed runs"
        if discord.alert_after_failures
        else "no source alerts"
    )
    return [
        _requires("Discord notifications", posting, on=f"Posts matches; {digest}, {alerts}."),
        _requires(
            "Discord buttons",
            {**posting, "DISCORD_PUBLIC_KEY": discord.public_key is not None},
            on="Buttons on posts are answered through /discord/interactions.",
        ),
        _requires(
            "Email sending (SMTP)",
            {
                "SMTP_HOST": smtp.host is not None,
                "SMTP_FROM_ADDRESS": smtp.from_address is not None,
            },
            on=f"Sends inquiries through {smtp.host}:{smtp.port}.",
        ),
        _switch(
            "Reply detection (IMAP)",
            imap.enabled,
            on=f"Checks {imap.folder} on {imap.host} every {imap.poll_interval_seconds}s.",
            off="IMAP_ENABLED is false.",
            env_vars=("IMAP_ENABLED", "IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD"),
        ),
        _switch(
            "LLM extraction",
            llm.provider is not LlmProvider.NONE,
            on=f"Provider {llm.provider.value}" + (f", model {llm.model}." if llm.model else "."),
            off="LLM_PROVIDER is none.",
            env_vars=("LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL"),
        ),
        _switch(
            "Geocoding",
            geocoding.provider is not GeocodingProvider.NONE,
            on=f"Provider {geocoding.provider.value} at {display(geocoding.base_url)}.",
            off="GEOCODING_PROVIDER is none.",
            env_vars=("GEOCODING_PROVIDER", "GEOCODING_BASE_URL", "GEOCODING_USER_AGENT"),
        ),
        _requires(
            "Reverse image search",
            {
                "REVERSE_IMAGE_PROVIDER": reverse.provider is not ReverseImageProvider.NONE,
                "REVERSE_IMAGE_API_KEY": reverse.api_key is not None,
            },
            on=f"Provider {reverse.provider.value}, up to {reverse.max_photos} photos a property.",
        ),
        _switch(
            "Error reporting (Sentry)",
            settings.sentry.dsn is not None,
            on="Errors are sent to Sentry.",
            off="SENTRY_DSN is not set.",
            env_vars=("SENTRY_DSN",),
        ),
        _switch(
            "Prometheus metrics",
            settings.metrics.enabled,
            on="GET /metrics, with the Bearer AUTH_TOKEN.",
            off="METRICS_ENABLED is false.",
            env_vars=("METRICS_ENABLED",),
        ),
        _switch(
            "Headless browser",
            settings.sources.browser_enabled,
            on="Sources that need a browser may use one; see Sources for each one's status.",
            off="SOURCES_BROWSER_ENABLED is false.",
            env_vars=("SOURCES_BROWSER_ENABLED",),
        ),
        _feature("Photo hashing", settings.images.enabled, "IMAGES_ENABLED"),
        _feature("Cross-source grouping", settings.grouping.enabled, "GROUPING_ENABLED"),
        _feature("Alternatives finder", settings.enrichment.enabled, "ENRICHMENT_ENABLED"),
        _feature("Scam and risk signals", settings.risk.enabled, "RISK_ENABLED"),
    ]


def _requires(name: str, needed: dict[str, bool], *, on: str) -> Integration:
    """On with every setting in ``needed``, off with none, otherwise incomplete."""
    missing = [env_var for env_var, present in needed.items() if not present]
    env_vars = tuple(needed)
    if not missing:
        return Integration(name, Status.ON, on, env_vars)
    if len(missing) == len(needed):
        return Integration(name, Status.OFF, f"Set {', '.join(env_vars)} to turn it on.", env_vars)
    return Integration(name, Status.INCOMPLETE, f"Missing {', '.join(missing)}.", env_vars)


def _switch(
    name: str, enabled: bool, *, on: str, off: str, env_vars: tuple[str, ...]
) -> Integration:
    """An integration the env validators keep from being half configured."""
    return Integration(name, Status.ON if enabled else Status.OFF, on if enabled else off, env_vars)


def _feature(name: str, enabled: bool, env_var: str) -> Integration:
    return _switch(name, enabled, on="Enabled.", off=f"{env_var} is false.", env_vars=(env_var,))


# --- Runtime settings ---------------------------------------------------------


def build_runtime_rows(runtime: RuntimeSettings) -> list[RuntimeRow]:
    """Every dashboard-editable setting with its current value."""
    groups = env_groups()
    rows = []
    for setting in RUNTIME_SETTINGS.values():
        type_ = setting.type_
        choices = (
            tuple(str(member.value) for member in type_)
            if isinstance(type_, type) and issubclass(type_, Enum)
            else ()
        )
        rows.append(
            RuntimeRow(
                setting=setting,
                env_var=env_var_name(groups[setting.group], setting.field),
                value=display(runtime.get(setting)) or "",
                pinned=runtime.env_pinned(setting),
                choices=choices,
            )
        )
    return rows
