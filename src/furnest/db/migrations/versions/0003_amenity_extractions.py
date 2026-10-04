"""amenity_extractions: LLM amenity verdicts cached by listing-content hash.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "amenity_extractions",
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("present", sa.JSON(), nullable=False),
        sa.Column("absent", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("content_hash", name=op.f("pk_amenity_extractions")),
    )


def downgrade() -> None:
    op.drop_table("amenity_extractions")
