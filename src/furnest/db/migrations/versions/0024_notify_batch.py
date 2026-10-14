"""Batched Discord posts: properties.notify_requested_at and discord_batches.

New suggestions now go out together in one list message rather than one message
each (#229). ``notify_requested_at`` marks a property waiting for the next batch;
``discord_batches`` remembers which properties each list message shows.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("properties", sa.Column("notify_requested_at", sa.DateTime(), nullable=True))
    op.create_index(
        op.f("ix_properties_notify_requested_at"),
        "properties",
        ["notify_requested_at"],
        unique=False,
    )
    op.create_table(
        "discord_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.String(length=32), nullable=False),
        sa.Column("message_id", sa.String(length=32), nullable=False),
        sa.Column("property_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discord_batches")),
        sa.UniqueConstraint("message_id", name=op.f("uq_discord_batches_message_id")),
    )


def downgrade() -> None:
    op.drop_table("discord_batches")
    op.drop_index(op.f("ix_properties_notify_requested_at"), table_name="properties")
    with op.batch_alter_table("properties") as batch_op:
        batch_op.drop_column("notify_requested_at")
