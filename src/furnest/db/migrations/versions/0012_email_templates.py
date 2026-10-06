"""email_templates: editable inquiry templates, seeded with ``default``.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-06
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A copy of furnest.email.templates.DEFAULT_SUBJECT/DEFAULT_BODY as of this
# revision: a migration must not change when the app's default does.
DEFAULT_SUBJECT = "Inquiry about {{ listing.title }}"
DEFAULT_BODY = """\
Hi{% if contact_name %} {{ contact_name }}{% endif %},

I came across your listing "{{ listing.title }}"
{%- if listing.price %} ({{ listing.price }}/month){% endif %} and I'm very interested.
{%- if move_in_date %} I'm looking to move in around {{ move_in_date|long_date }}
{%- if lease_months %} for {{ lease_months }} month{{ "s" if lease_months != 1 }}{% endif %}.
{%- elif lease_months %} I'm looking for a {{ lease_months }}-month stay.{% endif %}

Is it still available? I'd be glad to answer any questions or set up a viewing.

{{ listing.url }}
{% if signature %}
{{ signature }}
{% endif %}"""


def upgrade() -> None:
    table = op.create_table(
        "email_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_templates")),
        sa.UniqueConstraint("name", name=op.f("uq_email_templates_name")),
    )
    op.bulk_insert(
        table,
        [
            {
                "name": "default",
                "subject": DEFAULT_SUBJECT,
                "body": DEFAULT_BODY,
                # Stored as naive UTC, like furnest.db.base.UTCDateTime.
                "updated_at": datetime.now(UTC).replace(tzinfo=None),
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("email_templates")
