"""inbound email: replies found by IMAP polling are logged as ``received``.

Adds ``received`` to the ``email_status`` CHECK constraint. SQLite can't alter it
in place, so the table is rebuilt (batch mode).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = ("queued", "sent", "failed", "draft")
_NEW = (*_OLD, "received")
_CONSTRAINT = "ck_emails_email_status"


def _condition(statuses: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{status}'" for status in statuses) + ")"


def _replace(statuses: tuple[str, ...]) -> None:
    with op.batch_alter_table("emails") as batch_op:
        batch_op.drop_constraint(op.f(_CONSTRAINT), type_="check")
        batch_op.create_check_constraint(op.f(_CONSTRAINT), _condition(statuses))


def upgrade() -> None:
    _replace(_NEW)


def downgrade() -> None:
    # Received replies have no status to fall back to; the property keeps its state.
    op.execute("DELETE FROM emails WHERE status = 'received' OR direction = 'inbound'")
    _replace(_OLD)
