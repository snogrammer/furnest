"""Run Alembic migrations from code, and report where a database stands.

Migrations run on an open connection from a furnest engine, so they get the same
pragmas and locking as the rest of the app (see :mod:`furnest.db.engine`).
:func:`migration_status` is what ``/readyz`` uses to refuse traffic until the
schema is at head.
"""

from dataclasses import dataclass

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

SCRIPT_LOCATION = "furnest.db:migrations"


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    return config


def upgrade(engine: Engine, revision: str = "head") -> None:
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def downgrade(engine: Engine, revision: str) -> None:
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


@dataclass(frozen=True)
class MigrationStatus:
    current: str | None
    head: str | None

    @property
    def up_to_date(self) -> bool:
        return self.current == self.head


def migration_status(engine: Engine) -> MigrationStatus:
    return MigrationStatus(current=current_revision(engine), head=head_revision())
