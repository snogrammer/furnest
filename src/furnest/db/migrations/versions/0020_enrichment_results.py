"""enrichment_results: alternatives found for an approved property.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "enrichment_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("monthly_price_minor", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("cheaper", sa.Boolean(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["properties.id"],
            name=op.f("fk_enrichment_results_property_id_properties"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_enrichment_results")),
        sa.UniqueConstraint(
            "property_id",
            "kind",
            "source",
            "source_id",
            name=op.f("uq_enrichment_results_property_id"),
        ),
    )
    op.create_index(
        op.f("ix_enrichment_results_property_id"),
        "enrichment_results",
        ["property_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_enrichment_results_property_id"), table_name="enrichment_results")
    op.drop_table("enrichment_results")
