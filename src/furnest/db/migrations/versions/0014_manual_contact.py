"""manual contact: inquiries without SMTP, and the flag backfilled.

A listing with no email address is contacted by hand through its source
(``furnest.email.inquiry``). Its inquiry is drafted and marked as sent whether or
not SMTP is set up, so ``emails.from_address`` may be NULL: on a draft made with no
From address configured, and on such a draft once it was marked as sent. SQLite
can't alter the column in place, so the table is rebuilt (batch mode).

``properties.manual_contact_required`` existed since 0007 but nothing set it; it is
backfilled here from the listings, and the pipeline keeps it current from now on.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _from_nullable(nullable: bool) -> None:
    with op.batch_alter_table("emails") as batch_op:
        batch_op.alter_column(
            "from_address", existing_type=sa.String(length=320), nullable=nullable
        )


def upgrade() -> None:
    _from_nullable(True)
    op.execute(
        "UPDATE properties SET manual_contact_required = NOT EXISTS ("
        " SELECT 1 FROM listings WHERE listings.property_id = properties.id"
        " AND json_extract(listings.data, '$.contact.email') IS NOT NULL)"
    )


def downgrade() -> None:
    # Without a From address these emails have nothing to be; the flag stays.
    op.execute("DELETE FROM emails WHERE from_address IS NULL")
    _from_nullable(False)
