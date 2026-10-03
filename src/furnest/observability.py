"""Sentry error reporting for the web and worker processes.

Sentry is optional: without ``SENTRY_DSN`` nothing is initialised and every helper
here still works (job failures are logged either way). When enabled, events are
tagged with the release (``furnest@<version>``), environment and component, never
include default PII, and pass through the same ``Redactor`` as the logs before
they leave the process.
"""

from typing import Any, cast

import sentry_sdk
from sentry_sdk.integrations.logging import ignore_logger
from sentry_sdk.types import Event, Hint

from furnest import __version__
from furnest.config import Settings
from furnest.logging import Redactor, get_logger, secret_values

# Job failures are reported explicitly by ``capture_job_failure`` with tags; this
# logger is excluded from Sentry's logging integration so they are not sent twice.
_JOB_LOGGER = "furnest.jobs"

_job_log = get_logger(_JOB_LOGGER)


def init_sentry(component: str, settings: Settings, *, redactor: Redactor | None = None) -> bool:
    """Initialise Sentry for ``component`` ("web" or "worker").

    Returns ``False`` and does nothing when ``SENTRY_DSN`` is unset.
    """
    if settings.sentry.dsn is None:
        return False
    scrub = redactor or Redactor(secret_values(settings))

    def before_send(event: Event, _hint: Hint) -> Event:
        return cast(Event, scrub.redact(event))

    def before_breadcrumb(crumb: dict[str, Any], _hint: Hint) -> dict[str, Any]:
        return cast(dict[str, Any], scrub.redact(crumb))

    sentry_sdk.init(
        dsn=settings.sentry.dsn.get_secret_value(),
        release=f"furnest@{__version__}",
        environment=settings.sentry.environment or settings.app.environment.value,
        traces_sample_rate=settings.sentry.traces_sample_rate,
        send_default_pii=False,
        before_send=before_send,
        before_breadcrumb=before_breadcrumb,
    )
    sentry_sdk.get_global_scope().set_tag("component", component)
    ignore_logger(_JOB_LOGGER)
    return True


def capture_job_failure(
    exc: BaseException,
    *,
    job_id: int | str,
    job_type: str,
    source: str | None = None,
) -> str | None:
    """Log a failed job and report it to Sentry with job and source tags.

    Returns the Sentry event id, or ``None`` when Sentry is disabled.
    """
    _job_log.error("job_failed", job_id=job_id, job_type=job_type, source=source, exc_info=exc)
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("job_id", str(job_id))
        scope.set_tag("job_type", job_type)
        if source is not None:
            scope.set_tag("source", source)
        return sentry_sdk.capture_exception(exc)
