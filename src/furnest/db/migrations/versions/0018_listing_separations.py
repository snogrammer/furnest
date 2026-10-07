"""listing separations: listings a person split apart, which grouping keeps apart.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "listing_separations",
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("other_listing_id", sa.Integer(), nullable=False),
        sa.Column(
            "actor",
            sa.Enum(
                "dashboard",
                "discord",
                "system",
                "api",
                name="listing_separation_actor",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("actor_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "listing_id < other_listing_id", name=op.f("ck_listing_separations_ordered")
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_listing_separations_listing_id_listings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["other_listing_id"],
            ["listings.id"],
            name=op.f("fk_listing_separations_other_listing_id_listings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "listing_id", "other_listing_id", name=op.f("pk_listing_separations")
        ),
    )
    op.create_index(
        "ix_listing_separations_other_listing_id",
        "listing_separations",
        ["other_listing_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_listing_separations_other_listing_id", table_name="listing_separations")
    op.drop_table("listing_separations")
