"""The ``furnest`` command line: one image, three entry points.

Production runs the same container image with a different command:

* ``furnest web``     serves the dashboard, JSON API and Discord interactions.
* ``furnest worker``  runs scheduled source scrapes and drains the job queue.
* ``furnest migrate`` applies Alembic migrations once and exits.

Each command is a stub until its owning issue lands. A stub exits non-zero so
that a container started on an unfinished command fails loudly instead of
looking healthy.
"""

from typing import Annotated

import typer

from furnest import __version__

app = typer.Typer(
    name="furnest",
    help="Self-hosted furnished-rental monitor.",
    no_args_is_help=True,
    add_completion=False,
)


def _not_implemented(command: str, issue: int) -> None:
    typer.echo(f"furnest {command}: not implemented yet (see #{issue}).", err=True)
    raise typer.Exit(code=1)


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
    _not_implemented("migrate", 4)
