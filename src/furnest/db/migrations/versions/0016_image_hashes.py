"""image hashes: perceptual hash, hash error and hash time on ``listing_images``.

Adds an index on ``url``, since hashes are looked up and shared by URL.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("listing_images") as batch_op:
        batch_op.add_column(sa.Column("phash", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("hash_error", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("hashed_at", sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f("ix_listing_images_url"), ["url"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("listing_images") as batch_op:
        batch_op.drop_index(batch_op.f("ix_listing_images_url"))
        batch_op.drop_column("hashed_at")
        batch_op.drop_column("hash_error")
        batch_op.drop_column("phash")
