"""The ``furnest`` command line: one image, three entry points.

Production runs the same container image with a different command:

* ``furnest web``     serves the dashboard, JSON API and Discord interactions.
* ``furnest worker``  runs scheduled source scrapes and drains the job queue.
* ``furnest migrate`` applies Alembic migrations once and exits.

``web`` and ``worker`` are stubs until their owning issues land. A stub exits non-zero so
that a container started on an unfinished command fails loudly instead of
looking healthy.
"""

from typing import TYPE_CHECKING, Annotated

import typer

from furnest import __version__

if TYPE_CHECKING:
    from furnest.config import Settings

app = typer.Typer(
    name="furnest",
    help="Self-hosted furnished-rental monitor.",
    no_args_is_help=True,
    add_completion=False,
)


def _not_implemented(command: str, issue: int) -> None:
    typer.echo(f"furnest {command}: not implemented yet (see #{issue}).", err=True)
    raise typer.Exit(code=1)


def _bootstrap(component: str) -> "Settings":
    """Load settings, configure redacted logging and (if configured) Sentry.

    Every long-running command calls this first; ``web`` (#6) and ``worker`` (#8)
    will when they stop being stubs.
    """
    from furnest.config import get_settings
    from furnest.logging import bind_context, configure_logging, secret_values
    from furnest.observability import init_sentry

    settings = get_settings()
    redactor = configure_logging(settings.logging, secrets=secret_values(settings))
    init_sentry(component, settings, redactor=redactor)
    bind_context(component=component)
    return settings


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"furnest {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Self-hosted furnished-rental monitor."""


@app.command()
def web() -> None:
    """Serve the dashboard, JSON API and Discord interactions endpoint."""
    _not_implemented("web", 6)


@app.command()
def worker() -> None:
    """Run scheduled source scrapes and background jobs."""
    _not_implemented("worker", 8)


@app.command()
def migrate() -> None:
    """Apply database migrations and exit."""
    from furnest.db.engine import create_engine_from_settings
    from furnest.db.migrate import migration_status, upgrade
    from furnest.logging import get_logger

    settings = _bootstrap("migrate")
    engine = create_engine_from_settings(settings)
    try:
        upgrade(engine)
        status = migration_status(engine)
    finally:
        engine.dispose()
    get_logger(__name__).info("migrations_applied", revision=status.current, head=status.head)
    typer.echo(f"furnest migrate: database at revision {status.current} (head {status.head}).")
