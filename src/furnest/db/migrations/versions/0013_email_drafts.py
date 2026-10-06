"""email drafts: inquiry drafts waiting for review live in ``emails``.

Adds ``draft`` to the ``email_status`` CHECK constraint and lets ``to_address`` be
NULL: a draft for a listing with no email address has no recipient until someone
fills it in. SQLite can't alter either in place, so the table is rebuilt (batch mode).

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = ("queued", "sent", "failed")
_NEW = (*_OLD, "draft")
_CONSTRAINT = "ck_emails_email_status"


def _condition(statuses: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{status}'" for status in statuses) + ")"


def _replace(statuses: tuple[str, ...], *, to_nullable: bool) -> None:
    with op.batch_alter_table("emails") as batch_op:
        batch_op.drop_constraint(op.f(_CONSTRAINT), type_="check")
        batch_op.create_check_constraint(op.f(_CONSTRAINT), _condition(statuses))
        batch_op.alter_column(
            "to_address", existing_type=sa.String(length=320), nullable=to_nullable
        )


def upgrade() -> None:
    _replace(_NEW, to_nullable=True)


def downgrade() -> None:
    # Drafts were never sent; without the status they have nothing to be.
    op.execute("DELETE FROM emails WHERE status = 'draft' OR to_address IS NULL")
    _replace(_OLD, to_nullable=False)
