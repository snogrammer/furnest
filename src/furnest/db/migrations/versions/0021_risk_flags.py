"""risk_flags: scam and risk signals raised on a property.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "risk_flags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["properties.id"],
            name=op.f("fk_risk_flags_property_id_properties"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_flags")),
        sa.UniqueConstraint("property_id", "kind", name=op.f("uq_risk_flags_property_id")),
    )
    op.create_index(op.f("ix_risk_flags_property_id"), "risk_flags", ["property_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_risk_flags_property_id"), table_name="risk_flags")
    op.drop_table("risk_flags")
