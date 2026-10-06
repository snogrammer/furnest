"""Source health alerts in the Discord channel.

When a source reaches ``DISCORD_ALERT_AFTER_FAILURES`` failed runs in a row, the
source store queues a :data:`~furnest.db.source_store.SOURCE_ALERT_JOB` with
``event: "failing"``; the first success after that queues ``event: "recovered"``
(see :mod:`furnest.db.source_store`). The worker runs :class:`SourceAlertPost`,
which renders the payload with :func:`source_alert_message` and posts it to
``DISCORD_CHANNEL_ID``.

The payload carries what was true when the outcome was recorded (the failure count
and the last error), so a late or retried job still describes that moment.

* Discord not configured: the job logs and does nothing.
* Rate limits and other Discord errors raise, and the queue retries the job.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from furnest.config import Settings
from furnest.db.source_store import FAILING, RECOVERED, SOURCE_ALERT_JOB
from furnest.logging import get_logger
from furnest.notify.discord import DiscordClient, discord_client
from furnest.notify.embeds import JsonObject, escape_markdown, truncate

if TYPE_CHECKING:
    from furnest.worker.execution import JobContext

__all__ = ["SOURCE_ALERT_JOB", "SourceAlertPost", "source_alert_message"]

log = get_logger(__name__)

FAILING_COLOR = 0xDC2626
RECOVERED_COLOR = 0x16A34A
# The error goes in a code block; keep it to a readable size.
MAX_ERROR_CHARS = 1000


def source_alert_message(
    *, display_name: str, event: str, failures: int, error: str | None, base_url: str
) -> JsonObject:
    """The create-message payload for a source starting (``failing``) or stopping
    (``recovered``) failing.
    """
    name = escape_markdown(display_name)
    if event == FAILING:
        title = f"Source failing: {display_name}"
        description = f"{name} has failed {failures} runs in a row."
        color = FAILING_COLOR
    elif event == RECOVERED:
        title = f"Source recovered: {display_name}"
        description = f"{name} ran successfully again after {_runs(failures)} failing."
        color = RECOVERED_COLOR
    else:
        msg = f"unknown source alert event {event!r}"
        raise ValueError(msg)
    if error:
        # Backticks would end the code block early.
        snippet = truncate(error, MAX_ERROR_CHARS).replace("`", "'")
        description += f"\n```\n{snippet}\n```"
    description += f"\n[Source health]({base_url.rstrip('/')}/sources)"
    embed: JsonObject = {
        "title": truncate(title, "title"),
        "description": truncate(description, "description"),
        "color": color,
        "footer": {"text": "furnest · sources"},
    }
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


class SourceAlertPost:
    """The :data:`SOURCE_ALERT_JOB` handler.

    ``client_factory`` makes a :class:`DiscordClient` for each job, or returns
    ``None`` when Discord isn't configured. Tests pass one with a mock transport.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        source_name: Callable[[str], str] = str,
        client_factory: Callable[[], DiscordClient | None] | None = None,
    ) -> None:
        self._base_url = str(settings.app.base_url)
        self._channel_id = settings.discord.channel_id
        self._source_name = source_name
        self._client_factory = client_factory or (lambda: discord_client(settings))

    def __call__(self, ctx: JobContext) -> None:
        source = str(ctx.payload["source"])
        event = str(ctx.payload["event"])
        client = self._client_factory()
        if client is None or self._channel_id is None:
            log.info("source_alert_skipped", source=source, alert=event, reason="not configured")
            return
        error = ctx.payload.get("error")
        payload = source_alert_message(
            display_name=self._source_name(source),
            event=event,
            failures=int(ctx.payload["failures"]),
            error=None if error is None else str(error),
            base_url=self._base_url,
        )
        with client:
            message = client.create_message(self._channel_id, payload)
        log.info("source_alert_posted", source=source, alert=event, message_id=message.id)


def _runs(count: int) -> str:
    return f"{count} run" if count == 1 else f"{count} runs"
