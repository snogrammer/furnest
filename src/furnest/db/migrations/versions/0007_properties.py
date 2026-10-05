"""properties, audit_events, and the listings.property_id foreign key.

Adding the foreign key rebuilds ``listings``. :mod:`furnest.db.migrate` runs
migrations with foreign keys off, so dropping the old table doesn't cascade into
``listing_images`` and ``price_history``.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = (
    "new",
    "suggested",
    "approved",
    "denied",
    "inquiry_drafted",
    "inquiry_sent",
    "in_progress",
    "responded",
    "touring",
    "applied",
    "won",
    "lost",
    "archived",
    "unavailable",
)


def _status(name: str) -> sa.Enum:
    return sa.Enum(*STATUSES, name=name, native_enum=False, create_constraint=True, length=32)


def upgrade() -> None:
    op.create_table(
        "properties",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", _status("property_status"), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("match_result", sa.JSON(), nullable=True),
        sa.Column("criteria_version_id", sa.Integer(), nullable=True),
        sa.Column("snoozed_until", sa.DateTime(), nullable=True),
        sa.Column("manual_contact_required", sa.Boolean(), nullable=False),
        sa.Column("discord_channel_id", sa.String(length=32), nullable=True),
        sa.Column("discord_message_id", sa.String(length=32), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["criteria_version_id"],
            ["criteria_versions.id"],
            name=op.f("fk_properties_criteria_version_id_criteria_versions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_properties")),
    )
    op.create_index(op.f("ix_properties_status"), "properties", ["status"], unique=False)
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=False),
        sa.Column("from_status", _status("audit_event_from_status"), nullable=True),
        sa.Column("to_status", _status("audit_event_to_status"), nullable=False),
        sa.Column(
            "actor",
            sa.Enum(
                "dashboard",
                "discord",
                "system",
                name="audit_event_actor",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("actor_detail", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["properties.id"],
            name=op.f("fk_audit_events_property_id_properties"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        "ix_audit_events_property_id_created_at",
        "audit_events",
        ["property_id", "created_at"],
        unique=False,
    )
    with op.batch_alter_table("listings") as batch_op:
        batch_op.create_foreign_key(
            batch_op.f("fk_listings_property_id_properties"),
            "properties",
            ["property_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("listings") as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_listings_property_id_properties"), type_="foreignkey"
        )
    op.drop_index("ix_audit_events_property_id_created_at", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index(op.f("ix_properties_status"), table_name="properties")
    op.drop_table("properties")
