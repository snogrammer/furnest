"""owner type: who is behind a property, inferred and overridable.

``properties.owner_type`` is the effective owner type (individual, property_manager,
agent, unknown) and ``owner_type_override`` is the one a person chose, if any. Existing
rows start at ``unknown``: their listings carry no owner type yet, and each is
reclassified the next time its source run sees it (the listing's content hash changes
once, so it is rewritten and re-matched, which brings the property in step).

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TYPES = ("individual", "property_manager", "agent", "unknown")


def _in(column: str) -> str:
    return f"{column} IN (" + ", ".join(f"'{kind}'" for kind in _TYPES) + ")"


def upgrade() -> None:
    with op.batch_alter_table("properties") as batch_op:
        batch_op.add_column(
            sa.Column("owner_type", sa.String(length=32), nullable=False, server_default="unknown")
        )
        batch_op.add_column(sa.Column("owner_type_override", sa.String(length=32), nullable=True))
        batch_op.create_check_constraint("property_owner_type", _in("owner_type"))
        batch_op.create_check_constraint("property_owner_type_override", _in("owner_type_override"))


def downgrade() -> None:
    with op.batch_alter_table("properties") as batch_op:
        batch_op.drop_constraint(op.f("ck_properties_property_owner_type_override"), type_="check")
        batch_op.drop_constraint(op.f("ck_properties_property_owner_type"), type_="check")
        batch_op.drop_column("owner_type_override")
        batch_op.drop_column("owner_type")
