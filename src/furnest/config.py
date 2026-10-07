"""Configuration: environment settings plus settings that can be changed at runtime.

furnest is configured in two layers:

1. **Environment.** Infrastructure and secrets come from environment variables
   (or a ``.env`` file in the working directory). Each concern gets its own small
   ``BaseSettings`` group with a flat env prefix, so ``DISCORD_BOT_TOKEN`` maps to
   ``settings.discord.bot_token``. Every field is documented in ``.env.example``,
   and a test keeps the two in sync.

2. **Runtime overrides.** A few preferences can be edited from the dashboard and
   are kept in a key/value :class:`SettingsStore` (the ``settings`` table, #4).
   :class:`RuntimeSettings` resolves them in this order:

   * an env var that is explicitly set always wins, so operators can pin a value;
   * otherwise a valid stored value is used;
   * otherwise the env group's default applies.

Optional integrations (Sentry, LLM, geocoding, reverse image search, IMAP) default
to "off" and stay inert until configured.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import time
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    HttpUrl,
    JsonValue,
    NonNegativeFloat,
    PositiveFloat,
    SecretStr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)

Port = Annotated[int, Field(ge=1, le=65535)]
PositiveInt = Annotated[int, Field(gt=0)]


class Environment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"
    TEST = "test"


class InquiryMode(StrEnum):
    """Whether inquiry emails are sent automatically or drafted for review first."""

    AUTO = "auto"
    REVIEW = "review"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class SmtpSecurity(StrEnum):
    STARTTLS = "starttls"
    SSL = "ssl"
    NONE = "none"


class LlmProvider(StrEnum):
    NONE = "none"
    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai_compatible"


class GeocodingProvider(StrEnum):
    NONE = "none"
    NOMINATIM = "nominatim"


class ReverseImageProvider(StrEnum):
    NONE = "none"
    SERPAPI = "serpapi"


# --- Environment groups ------------------------------------------------------


class _Group(BaseSettings):
    """Shared behaviour for every env group; subclasses only set ``env_prefix``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )


class AppSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="")

    environment: Environment = Environment.PRODUCTION
    base_url: HttpUrl = HttpUrl("http://localhost:8000")
    data_dir: Path = Path("/data")
    inquiry_mode: InquiryMode = InquiryMode.REVIEW
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = f"TIMEZONE {value!r} is not an IANA timezone name"
            raise ValueError(msg) from exc
        return value

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class DbSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="DB_")

    # Unset means "furnest.db inside DATA_DIR"; see Settings.database_url.
    url: str | None = None
    busy_timeout_ms: PositiveInt = 5000
    auto_migrate: bool = False

    @field_validator("url")
    @classmethod
    def _sqlite_only(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("sqlite"):
            msg = "only SQLite URLs are supported (sqlite:///path/to/furnest.db)"
            raise ValueError(msg)
        return value


class AuthSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="AUTH_")

    token: SecretStr | None = None
    session_secret: SecretStr | None = None
    cookie_secure: bool = True
    session_max_age_seconds: PositiveInt = 7 * 24 * 3600
    # Failed logins allowed per client IP within the window before it is locked out.
    login_max_attempts: PositiveInt = 5
    login_window_seconds: PositiveInt = 300


class WebSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="WEB_")

    # Loopback by default; containers set WEB_HOST=0.0.0.0 behind the reverse proxy.
    host: str = "127.0.0.1"
    port: Port = 8000
    # Reverse proxies (IPs or CIDRs) whose X-Forwarded-For/-Proto headers are believed.
    # Empty means forwarded headers are ignored and the socket peer is the client.
    trusted_proxies: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


class WorkerSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="WORKER_")

    # /readyz fails when the newest worker heartbeat is older than this.
    heartbeat_max_age_seconds: PositiveInt = 120
    heartbeat_interval_seconds: PositiveInt = 30
    # How long an idle queue consumer sleeps before looking for work again.
    poll_interval_seconds: PositiveFloat = 2.0
    # Queue consumer threads, i.e. how many queued jobs run at once.
    concurrency: PositiveInt = 2
    # Default per-job timeout; a job handler can register its own.
    job_timeout_seconds: PositiveInt = 300
    # Failed queued jobs retry after min(max, base * 2^(attempt-1)) seconds, jittered.
    retry_base_seconds: PositiveInt = 30
    retry_max_seconds: PositiveInt = 3600
    # On SIGTERM, how long running jobs get to finish. Keep it below the container's
    # stop grace period (Docker's default is 10s; set stop_grace_period accordingly).
    shutdown_timeout_seconds: PositiveInt = 25

    @model_validator(mode="after")
    def _heartbeat_beats_faster_than_max_age(self) -> WorkerSettings:
        if self.heartbeat_interval_seconds >= self.heartbeat_max_age_seconds:
            msg = "WORKER_HEARTBEAT_INTERVAL_SECONDS must be below WORKER_HEARTBEAT_MAX_AGE_SECONDS"
            raise ValueError(msg)
        return self


class SentrySettings(_Group):
    model_config = SettingsConfigDict(env_prefix="SENTRY_")

    dsn: SecretStr | None = None
    environment: str | None = None
    traces_sample_rate: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0


class LoggingSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="LOG_")

    level: LogLevel = LogLevel.INFO
    format: LogFormat = LogFormat.JSON


_ED25519_PUBLIC_KEY = re.compile(r"[0-9a-fA-F]{64}")


class DiscordSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="DISCORD_")

    application_id: str | None = None
    public_key: str | None = None
    bot_token: SecretStr | None = None
    channel_id: str | None = None
    request_timeout_seconds: PositiveFloat = 10.0
    # Retries after the first attempt for 5xx responses and transport errors. A 429 is
    # waited out as Discord asks (a few times at most) and has its own budget.
    max_retries: Annotated[int, Field(ge=0)] = 3
    # How far an interaction's signed timestamp may be from our clock, either way,
    # before ``POST /discord/interactions`` rejects it as a possible replay.
    interaction_max_age_seconds: PositiveInt = 300
    # Local time (``TIMEZONE``) of the daily digest; unset means no digest.
    digest_time: time | None = None
    # How many of the day's new suggestions the digest lists.
    digest_top: PositiveInt = 5
    # Alert once a source has failed this many runs in a row; 0 turns alerts off.
    alert_after_failures: Annotated[int, Field(ge=0)] = 3

    @field_validator("public_key")
    @classmethod
    def _public_key_is_hex(cls, value: str | None) -> str | None:
        if value is not None and _ED25519_PUBLIC_KEY.fullmatch(value) is None:
            msg = "DISCORD_PUBLIC_KEY must be the 64-character hex key from the developer portal"
            raise ValueError(msg)
        return value


class SmtpSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="SMTP_")

    host: str | None = None
    port: Port = 587
    username: str | None = None
    password: SecretStr | None = None
    security: SmtpSecurity = SmtpSecurity.STARTTLS
    from_address: EmailStr | None = None
    # Display name in the From header, e.g. "Jane Doe" <furnest@example.com>.
    from_name: str | None = None
    # Connect/read timeout for each SMTP operation; keep it well below the job timeout.
    timeout_seconds: PositiveFloat = 30.0

    @property
    def configured(self) -> bool:
        """True when there is a server to send through and an address to send from."""
        return self.host is not None and self.from_address is not None


class ImapSettings(_Group):
    """Reply detection (``furnest.email.imap``): off unless ``IMAP_ENABLED`` is set."""

    model_config = SettingsConfigDict(env_prefix="IMAP_")

    enabled: bool = False
    host: str | None = None
    # 993 for ssl; 143 for starttls or none.
    port: Port = 993
    username: str | None = None
    password: SecretStr | None = None
    security: SmtpSecurity = SmtpSecurity.SSL
    folder: str = "INBOX"
    poll_interval_seconds: PositiveInt = 300
    # Connect/read timeout for each IMAP operation.
    timeout_seconds: PositiveFloat = 30.0
    # How far back one poll looks for replies, at most.
    lookback_days: PositiveInt = 14

    @property
    def configured(self) -> bool:
        """True when there is a server to log in to."""
        return self.host is not None and self.username is not None and self.password is not None

    @model_validator(mode="after")
    def _enabled_needs_server(self) -> ImapSettings:
        if self.enabled and not self.configured:
            msg = "IMAP_ENABLED=true requires IMAP_HOST, IMAP_USERNAME and IMAP_PASSWORD"
            raise ValueError(msg)
        return self


class LlmSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="LLM_")

    provider: LlmProvider = LlmProvider.NONE
    api_key: SecretStr | None = None
    model: str | None = None
    base_url: HttpUrl | None = None
    timeout_seconds: PositiveFloat = 30.0

    @model_validator(mode="after")
    def _openai_compatible_needs_endpoint(self) -> LlmSettings:
        if self.provider is LlmProvider.OPENAI_COMPATIBLE and (
            self.base_url is None or not self.model
        ):
            msg = "LLM_PROVIDER=openai_compatible requires LLM_BASE_URL and LLM_MODEL"
            raise ValueError(msg)
        return self

    @property
    def openai_compatible_enabled(self) -> bool:
        """True when the OpenAI-compatible extractor is configured (the validator
        guarantees ``base_url`` and ``model`` are then set)."""
        return self.provider is LlmProvider.OPENAI_COMPATIBLE


class GeocodingSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="GEOCODING_")

    provider: GeocodingProvider = GeocodingProvider.NONE
    base_url: HttpUrl | None = None
    api_key: SecretStr | None = None
    user_agent: str | None = None


class ReverseImageSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="REVERSE_IMAGE_")

    provider: ReverseImageProvider = ReverseImageProvider.NONE
    api_key: SecretStr | None = None


class ImageSettings(_Group):
    """Listing photo fetching and perceptual hashing (``furnest.dedup.phash``).

    Photos are downloaded into memory, hashed and thrown away; nothing is stored but
    the hash. Requests use the ``SOURCES_`` user agent, proxy and robots.txt setting.
    """

    model_config = SettingsConfigDict(env_prefix="IMAGES_")

    enabled: bool = True
    # How often the worker hashes photos that don't have a hash yet.
    interval_minutes: PositiveInt = 10
    # Photo URLs per run, and only the first ``per_listing`` photos of each listing.
    batch_size: PositiveInt = 50
    per_listing: PositiveInt = 10
    # A larger response, or a decoded image with more pixels, is rejected unread.
    max_bytes: PositiveInt = 10_000_000
    max_pixels: PositiveInt = 40_000_000
    timeout_seconds: PositiveInt = 15
    # Per image host, enforced by a token bucket.
    requests_per_minute: PositiveFloat = 30.0


class GroupingSettings(_Group):
    """Cross-source grouping of listings into properties (``furnest.dedup.grouping``).

    A listing joins an existing property when its weighted similarity to one of that
    property's listings reaches ``match_threshold``. The score is a weighted mean of
    the signals both listings have (address, distance, photos, title, description),
    and needs at least ``min_signals`` of them. A different bedroom count or unit
    always keeps two listings apart. Disabled, every listing gets its own property.
    """

    model_config = SettingsConfigDict(env_prefix="GROUPING_")

    enabled: bool = True
    # Listings further apart than this are never the same unit; also the radius
    # searched for candidate properties.
    max_distance_m: PositiveFloat = 75.0
    match_threshold: float = Field(default=0.75, gt=0, le=1)
    min_signals: PositiveInt = 2
    # Photos whose pHashes are at most this many bits apart count as the same photo.
    phash_distance: PositiveInt = 8
    # Candidate properties compared per listing, nearest first.
    max_candidates: PositiveInt = 25
    # Relative signal weights; 0 turns a signal off.
    weight_address: NonNegativeFloat = 3.0
    weight_distance: NonNegativeFloat = 1.0
    weight_photos: NonNegativeFloat = 3.0
    weight_title: NonNegativeFloat = 1.0
    weight_description: NonNegativeFloat = 1.0


# Adapter names: lower-case identifiers, also used as keys in SOURCES_ADAPTERS.
ADAPTER_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"


class AdapterOverrides(BaseModel):
    """One adapter's entry in ``SOURCES_ADAPTERS``. Unset fields use the ``SOURCES_*`` default.

    ``enabled`` is special: when present it pins the adapter on or off, overriding
    the dashboard toggle (see ``furnest.sources.config``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool | None = None
    interval_minutes: PositiveInt | None = None
    jitter_seconds: Annotated[int, Field(ge=0)] | None = None
    requests_per_minute: PositiveFloat | None = None
    user_agent: Annotated[str, Field(min_length=1)] | None = None
    proxy_url: AnyUrl | None = None
    api_key: SecretStr | None = None
    request_timeout_seconds: PositiveInt | None = None
    run_timeout_seconds: PositiveInt | None = None
    respect_robots: bool | None = None
    max_retries: Annotated[int, Field(ge=0)] | None = None
    # Where to search, in the source's own terms (e.g. "Springfield, IL"). Only for
    # sources that search by place name rather than by the criteria's areas.
    search_locations: (
        tuple[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)], ...] | None
    ) = None
    max_results: PositiveInt | None = None


class SourcesSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="SOURCES_")

    user_agent: str = "furnest/0 (+https://example.com/furnest)"
    default_interval_minutes: PositiveInt = 60
    jitter_seconds: Annotated[int, Field(ge=0)] = 300
    request_timeout_seconds: PositiveInt = 30
    # A whole run of one source (search, store, match) is abandoned after this long.
    run_timeout_seconds: PositiveInt = 900
    # Polite default request budget per adapter, enforced by a token bucket.
    requests_per_minute: PositiveFloat = 6.0
    proxy_url: AnyUrl | None = None
    respect_robots: bool = True
    # Retries after the first attempt for 429s, 5xx responses and transport errors.
    max_retries: Annotated[int, Field(ge=0)] = 3
    # Upper bound on listings one run asks for, for sources that page or bill per result.
    max_results: PositiveInt = 100
    browser_enabled: bool = False
    # A listing is marked removed once it has been missing from this many successful
    # runs of its source *and* unseen for this many hours.
    remove_after_missed_runs: PositiveInt = 3
    remove_after_hours: PositiveInt = 24
    # Per-adapter overrides as JSON, keyed by adapter name.
    adapters: dict[
        Annotated[str, StringConstraints(pattern=ADAPTER_NAME_PATTERN)], AdapterOverrides
    ] = Field(default_factory=dict)


class PipelineSettings(_Group):
    model_config = SettingsConfigDict(env_prefix="PIPELINE_")

    # How long the Snooze button hides a property from the default views.
    snooze_hours: PositiveInt = 72
    # How often the worker looks for expired snoozes to bring back.
    unsnooze_interval_minutes: PositiveInt = 15


class Settings(BaseModel):
    """All environment settings, one attribute per group."""

    model_config = ConfigDict(frozen=True)

    app: AppSettings = Field(default_factory=AppSettings)
    db: DbSettings = Field(default_factory=DbSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    sentry: SentrySettings = Field(default_factory=SentrySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    discord: DiscordSettings = Field(default_factory=DiscordSettings)
    smtp: SmtpSettings = Field(default_factory=SmtpSettings)
    imap: ImapSettings = Field(default_factory=ImapSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    geocoding: GeocodingSettings = Field(default_factory=GeocodingSettings)
    reverse_image: ReverseImageSettings = Field(default_factory=ReverseImageSettings)
    images: ImageSettings = Field(default_factory=ImageSettings)
    grouping: GroupingSettings = Field(default_factory=GroupingSettings)
    sources: SourcesSettings = Field(default_factory=SourcesSettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)

    @property
    def database_url(self) -> str:
        """``DB_URL`` if set, else a SQLite file inside ``DATA_DIR``."""
        if self.db.url is not None:
            return self.db.url
        return f"sqlite:///{self.app.data_dir / 'furnest.db'}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process. Tests call ``get_settings.cache_clear()``."""
    return Settings()


def env_var_names() -> list[str]:
    """Every env var furnest reads, in declaration order (used to check ``.env.example``)."""
    names: list[str] = []
    for group_field in Settings.model_fields.values():
        group = group_field.annotation
        if not (isinstance(group, type) and issubclass(group, _Group)):
            msg = f"Settings field {group_field!r} is not an env group"
            raise TypeError(msg)
        prefix = group.model_config.get("env_prefix", "")
        names.extend(f"{prefix}{name}".upper() for name in group.model_fields)
    return names


# --- Runtime overrides -------------------------------------------------------


class SettingsStore(Protocol):
    """Key/value storage for runtime-editable settings. Values are plain JSON."""

    def get(self, key: str) -> JsonValue | None: ...

    def set(self, key: str, value: JsonValue) -> None: ...

    def delete(self, key: str) -> None: ...


class InMemorySettingsStore:
    """Dict-backed :class:`SettingsStore`, for tests and as a reference implementation."""

    def __init__(self, initial: dict[str, JsonValue] | None = None) -> None:
        self._values: dict[str, JsonValue] = dict(initial or {})

    def get(self, key: str) -> JsonValue | None:
        return self._values.get(key)

    def set(self, key: str, value: JsonValue) -> None:
        self._values[key] = value

    def delete(self, key: str) -> None:
        self._values.pop(key, None)


@dataclass(frozen=True)
class RuntimeSetting[T]:
    """A setting that the dashboard may override, backed by one env field.

    ``group`` and ``field`` name the env field that supplies the default (and
    that wins when the env var is explicitly set). ``type_`` validates stored
    values.
    """

    key: str
    group: str
    field: str
    type_: type[T]

    @property
    def adapter(self) -> TypeAdapter[T]:
        return TypeAdapter(self.type_)


INQUIRY_MODE = RuntimeSetting("inquiry_mode", "app", "inquiry_mode", InquiryMode)

RUNTIME_SETTINGS: dict[str, RuntimeSetting[Any]] = {
    setting.key: setting for setting in (INQUIRY_MODE,)
}


class RuntimeSettings:
    """Resolves runtime-editable settings: explicit env > stored value > env default."""

    def __init__(self, settings: Settings, store: SettingsStore) -> None:
        self._settings = settings
        self._store = store

    def get[T](self, setting: RuntimeSetting[T]) -> T:
        group: BaseSettings = getattr(self._settings, setting.group)
        env_value: T = getattr(group, setting.field)
        if self.env_pinned(setting):
            return env_value

        stored = self._store.get(setting.key)
        if stored is None:
            return env_value
        try:
            return setting.adapter.validate_python(stored)
        except ValidationError:
            logger.warning(
                "ignoring invalid stored value for setting %r; using the env default",
                setting.key,
            )
            return env_value

    def set[T](self, setting: RuntimeSetting[T], value: T) -> None:
        """Validate and store ``value``. Has no effect while the env var is pinned."""
        validated = setting.adapter.validate_python(value)
        self._store.set(setting.key, setting.adapter.dump_python(validated, mode="json"))

    def reset[T](self, setting: RuntimeSetting[T]) -> None:
        """Drop the stored override so the env default applies again."""
        self._store.delete(setting.key)

    def env_pinned[T](self, setting: RuntimeSetting[T]) -> bool:
        """True when the env var is explicitly set, so the dashboard cannot override it."""
        group: BaseSettings = getattr(self._settings, setting.group)
        return setting.field in group.model_fields_set
