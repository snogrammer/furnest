"""property grouping: merged properties, match source and listing address keys.

Adds ``properties.merged_into_id`` and ``properties.match_listing_id``, and
``listings.address_key``, ``listings.street_key`` and a ``(lat, lng)`` index for
finding candidate properties.

Until now every property had exactly one listing, and its ``address`` is that
listing's canonical address, so the backfill copies it to ``address_key`` when it
starts with a house number (an address without one is never a grouping key).
``street_key`` needs the parser and is filled in the next time the listing is
matched. ``match_listing_id`` is the property's one listing.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("listings") as batch_op:
        batch_op.add_column(sa.Column("address_key", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("street_key", sa.Text(), nullable=True))
        batch_op.create_index(batch_op.f("ix_listings_address_key"), ["address_key"], unique=False)
        batch_op.create_index(batch_op.f("ix_listings_street_key"), ["street_key"], unique=False)
        batch_op.create_index("ix_listings_lat_lng", ["lat", "lng"], unique=False)
    with op.batch_alter_table("properties") as batch_op:
        batch_op.add_column(sa.Column("match_listing_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("merged_into_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_properties_merged_into_id"), ["merged_into_id"], unique=False
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_properties_merged_into_id_properties"),
            "properties",
            ["merged_into_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.execute(
        """
        UPDATE listings SET address_key = (
            SELECT properties.address FROM properties
            WHERE properties.id = listings.property_id
              AND (properties.address GLOB '[0-9]*' OR properties.address LIKE 'po box %')
        )
        WHERE property_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE properties SET match_listing_id = (
            SELECT listings.id FROM listings
            WHERE listings.property_id = properties.id
            ORDER BY listings.removed_at IS NOT NULL, listings.id
            LIMIT 1
        )
        WHERE match_result IS NOT NULL
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("properties") as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_properties_merged_into_id_properties"), type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_properties_merged_into_id"))
        batch_op.drop_column("merged_into_id")
        batch_op.drop_column("match_listing_id")
    with op.batch_alter_table("listings") as batch_op:
        batch_op.drop_index("ix_listings_lat_lng")
        batch_op.drop_index(batch_op.f("ix_listings_street_key"))
        batch_op.drop_index(batch_op.f("ix_listings_address_key"))
        batch_op.drop_column("street_key")
        batch_op.drop_column("address_key")
