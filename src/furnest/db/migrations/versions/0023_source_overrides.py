"""sources.overrides: the per-source settings the dashboard saves.

Search locations, interval and max results, as a JSON object with only the keys
that were set (``furnest.config.SourceOverrides``). NULL means the dashboard set
nothing, so ``SOURCES_ADAPTERS`` and the ``SOURCES_*`` defaults apply.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("overrides", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sources") as batch_op:
        batch_op.drop_column("overrides")
