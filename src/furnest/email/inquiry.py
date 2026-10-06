"""The ``inquiry.create`` job: draft (and maybe send) an inquiry email for a property.

The Discord Inquire button (and later the dashboard) approves the property and
queues this job with ``property_id``, ``actor`` and ``actor_detail`` in the payload.
The real handler (#42) renders the draft, moves the property from ``approved`` to
``inquiry_drafted``, and then sends it or leaves it for review, depending on
``INQUIRY_MODE``. Until #42 lands, :func:`inquiry_not_implemented` only logs, so
queued jobs finish instead of failing for want of a handler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from furnest.logging import get_logger

if TYPE_CHECKING:
    from furnest.worker.execution import JobContext

log = get_logger(__name__)

INQUIRY_CREATE = "inquiry.create"


def inquiry_not_implemented(ctx: JobContext) -> None:
    """Placeholder :data:`INQUIRY_CREATE` handler until the inquiry flow exists (#42)."""
    log.info("inquiry_skipped", property_id=ctx.payload.get("property_id"))
