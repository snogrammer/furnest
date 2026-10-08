"""geocode_cache: geocoder answers (found or not) cached by query key.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "geocode_cache",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("query", sa.JSON(), nullable=False),
        sa.Column("found", sa.Boolean(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("address", sa.JSON(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("precision", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_geocode_cache")),
    )


def downgrade() -> None:
    op.drop_table("geocode_cache")
