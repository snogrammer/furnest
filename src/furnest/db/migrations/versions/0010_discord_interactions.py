"""discord_interactions: interaction ids already handled (idempotency).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discord_interactions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("type", sa.Integer(), nullable=False),
        sa.Column("custom_id", sa.String(length=100), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discord_interactions")),
    )
    op.create_index(
        op.f("ix_discord_interactions_received_at"),
        "discord_interactions",
        ["received_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_discord_interactions_received_at"), table_name="discord_interactions")
    op.drop_table("discord_interactions")
