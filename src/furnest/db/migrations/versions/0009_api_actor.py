"""api actor: audit events and notes made through the JSON API.

Widens the ``actor`` CHECK constraints on ``audit_events`` and ``notes``. SQLite
can't alter a constraint in place, so both tables are rebuilt (batch mode).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = ("dashboard", "discord", "system")
_NEW = (*_OLD, "api")

# (table, constraint name as the naming convention renders it)
_TABLES = (
    ("audit_events", "ck_audit_events_audit_event_actor"),
    ("notes", "ck_notes_note_actor"),
)


def _condition(actors: tuple[str, ...]) -> str:
    return "actor IN (" + ", ".join(f"'{actor}'" for actor in actors) + ")"


def _replace(actors: tuple[str, ...]) -> None:
    for table, constraint in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(op.f(constraint), type_="check")
            batch_op.create_check_constraint(op.f(constraint), _condition(actors))


def upgrade() -> None:
    _replace(_NEW)


def downgrade() -> None:
    op.execute("DELETE FROM notes WHERE actor = 'api'")
    op.execute("DELETE FROM audit_events WHERE actor = 'api'")
    _replace(_OLD)
