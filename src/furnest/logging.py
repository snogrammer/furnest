"""Structured logging with secret redaction.

Every log line, whether it comes from furnest's own structlog loggers or from a
third-party stdlib logger (SQLAlchemy, Alembic, uvicorn), passes through one
processor chain:

    contextvars -> logger name -> level -> UTC timestamp -> exception text
    -> **redaction** -> JSON or console renderer

Redaction is the last step before rendering, so it also sees exception tracebacks
and messages that stdlib loggers built with ``%`` formatting. The ``Redactor``
removes secrets three ways:

* by key: values under keys such as ``password``, ``bot_token``, ``api_key``,
  ``Authorization`` or ``Cookie``, at any nesting depth;
* by type: pydantic ``SecretStr``/``SecretBytes`` values;
* by content: ``Bearer``/``Basic`` credentials, ``user:pass@`` in URLs,
  ``token=...`` style pairs, and the exact values of every secret furnest is
  configured with (see ``secret_values``).

The same ``Redactor`` scrubs Sentry events (``furnest.observability``).

Context such as ``request_id``, ``job_id``, ``source`` and ``component`` is bound
with ``bind_context``/``bound_context`` and appears on every line logged while it
is bound.
"""

import logging
import re
import sys
from collections.abc import Iterable, Mapping
from typing import Any, TextIO

import structlog
from pydantic import SecretBytes, SecretStr
from structlog.typing import EventDict, Processor, WrappedLogger

from furnest.config import LogFormat, LoggingSettings, Settings

REDACTED = "[REDACTED]"

# Keys whose values are always secret. Anchored at the end so that "bot_token" and
# "x-api-key" match but "input_tokens" or "token_count" do not.
_SECRET_KEY = re.compile(
    r"(passw(or)?d|secret|token|api[_-]?key|authorization|cookies?|dsn|credentials?)$",
    re.IGNORECASE,
)

# (pattern, replacement) pairs applied to every string.
_SECRET_TEXT: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization header values: "Bearer abc.def", "Basic dXNlcjpwYXNz".
    (re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), rf"\1 {REDACTED}"),
    # Credentials embedded in URLs: "smtp://user:pass@host".
    (re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", re.IGNORECASE), rf"\1{REDACTED}@"),
    # Query-string or key=value pairs: "?api_key=abc", "password=hunter2".
    (
        re.compile(
            r"\b(passw(?:or)?d|secret|token|api[_-]?key|access_token)=[^\s&\"']+",
            re.IGNORECASE,
        ),
        rf"\1={REDACTED}",
    ),
)

# Configured secrets shorter than this are not redacted by value: replacing every
# "abc" in every log line would destroy the logs without protecting anything.
_MIN_SECRET_LENGTH = 6


class Redactor:
    """Removes secrets from log events and Sentry payloads.

    ``secrets`` are exact values (tokens, passwords) to scrub wherever they appear
    in text. Usable directly as a structlog processor.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        values = {s for s in secrets if len(s) >= _MIN_SECRET_LENGTH}
        # Longest first so a secret that contains another is replaced whole.
        self._secrets = sorted(values, key=len, reverse=True)

    def __call__(self, _logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
        return {key: self.redact(value, key) for key, value in event_dict.items()}

    def redact(self, value: Any, key: object = None) -> Any:  # noqa: ANN401 - any JSON-ish value
        """Return ``value`` with secrets replaced; containers are copied, not mutated."""
        if isinstance(key, str) and value is not None and _SECRET_KEY.search(key):
            return REDACTED
        if isinstance(value, SecretStr | SecretBytes):
            return REDACTED
        if isinstance(value, str):
            return self.redact_text(value)
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, Mapping):
            return {k: self.redact(v, k) for k, v in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            return [self.redact(v) for v in value]
        # Anything else is rendered as text by the JSON renderer anyway; do it here
        # so its text is scrubbed too.
        return self.redact_text(str(value))

    def redact_text(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for pattern, replacement in _SECRET_TEXT:
            text = pattern.sub(replacement, text)
        return text


def secret_values(settings: Settings) -> list[str]:
    """The plaintext of every ``SecretStr`` setting that is set, per-adapter
    ``SOURCES_ADAPTERS`` API keys included."""
    values: list[str] = []
    for group_name in type(settings).model_fields:
        group = getattr(settings, group_name)
        for field_name in type(group).model_fields:
            value = getattr(group, field_name)
            if isinstance(value, SecretStr) and value.get_secret_value():
                values.append(value.get_secret_value())
    for overrides in settings.sources.adapters.values():
        if overrides.api_key is not None and overrides.api_key.get_secret_value():
            values.append(overrides.api_key.get_secret_value())
    return values


class _StderrHandler(logging.StreamHandler[TextIO]):
    """Writes to whatever ``sys.stderr`` is at emit time.

    Test runners and CLI harnesses swap ``sys.stderr``; binding the stream once at
    configure time would leave the handler writing to a closed file.
    """

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def configure_logging(settings: LoggingSettings, *, secrets: Iterable[str] = ()) -> Redactor:
    """Route structlog and stdlib logging through one redacting pipeline to stderr.

    Safe to call more than once: the previous furnest handler is replaced, not
    stacked. Returns the ``Redactor`` so Sentry can reuse it.
    """
    redactor = Redactor(secrets)
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redactor,
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if settings.format is LogFormat.JSON
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    handler = _StderrHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    for existing in [h for h in root.handlers if isinstance(h, _StderrHandler)]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.level.value)
    logging.captureWarnings(True)
    return redactor


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """A structlog logger; pass ``__name__``."""
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


# Context binding: ``bind_context(job_id=..., source=...)`` for the rest of the task,
# or ``with bound_context(request_id=...):`` for a block.
bind_context = structlog.contextvars.bind_contextvars
bound_context = structlog.contextvars.bound_contextvars
clear_context = structlog.contextvars.clear_contextvars
