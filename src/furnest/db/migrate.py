"""Run Alembic migrations from code, and report where a database stands.

Migrations run on an open connection from a furnest engine, so they get the same
pragmas and locking as the rest of the app (see :mod:`furnest.db.engine`).
:func:`migration_status` is what ``/readyz`` uses to refuse traffic until the
schema is at head.
"""

from dataclasses import dataclass

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text

SCRIPT_LOCATION = "furnest.db:migrations"
VERSION_TABLE = "alembic_version"


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    return config


# The connection is handed over before it begins a transaction: env.py turns foreign
# keys off first (SQLite ignores that inside a transaction), then begins and commits.
def upgrade(engine: Engine, revision: str = "head") -> None:
    config = alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def downgrade(engine: Engine, revision: str) -> None:
    config = alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    # Read alembic_version directly: MigrationContext.configure logs two INFO lines
    # per call, which would flood the logs from a polled /readyz. The history is
    # linear, so the table holds at most one row.
    with engine.connect() as connection:
        if not inspect(connection).has_table(VERSION_TABLE):
            return None
        revision: str | None = connection.execute(
            text(f"SELECT version_num FROM {VERSION_TABLE}")  # noqa: S608 - constant
        ).scalar_one_or_none()
        return revision


@dataclass(frozen=True)
class MigrationStatus:
    current: str | None
    head: str | None

    @property
    def up_to_date(self) -> bool:
        return self.current == self.head


def migration_status(engine: Engine) -> MigrationStatus:
    return MigrationStatus(current=current_revision(engine), head=head_revision())
