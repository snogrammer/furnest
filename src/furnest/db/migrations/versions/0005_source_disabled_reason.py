"""sources.disabled_reason: why the worker can't run an adapter.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("disabled_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sources") as batch_op:
        batch_op.drop_column("disabled_reason")
