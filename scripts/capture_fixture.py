"""Record a live page as a sanitized test fixture for one adapter.

Manual use only: it refuses to run when ``CI`` is set. See CONTRIBUTING.md
("Refreshing fixtures") and :mod:`furnest.sources.capture`.

    uv run python scripts/capture_fixture.py <adapter> <url> [--name F] [--redact NAME]...
"""

from pathlib import Path
from typing import Annotated

import typer

from furnest.config import Settings
from furnest.logging import configure_logging, secret_values
from furnest.sources.capture import CaptureError, capture_fixture
from furnest.sources.config import SourceConfigError
from furnest.sources.errors import SourceBlockedError, SourceError

app = typer.Typer(add_completion=False)


@app.command()
def main(
    adapter: Annotated[str, typer.Argument(help="Adapter name, e.g. furnished_finder.")],
    url: Annotated[str, typer.Argument(help="Page or API URL to record.")],
    name: Annotated[
        str | None,
        typer.Option(help="File name to write. Default: derived from the URL path."),
    ] = None,
    redact: Annotated[
        list[str] | None,
        typer.Option(help="A name to replace wherever it appears. Repeatable."),
    ] = None,
    force: Annotated[bool, typer.Option(help="Replace an existing fixture.")] = False,
    out_dir: Annotated[Path, typer.Option(help="Fixtures root.")] = Path("tests/fixtures"),
) -> None:
    """Fetch URL as ADAPTER would, sanitize it, and write it under the fixtures root."""
    settings = Settings()
    configure_logging(settings.logging, secrets=secret_values(settings))
    try:
        result = capture_fixture(
            adapter,
            url,
            settings=settings.sources,
            out_dir=out_dir,
            name=name,
            redact=redact or (),
            force=force,
        )
    except SourceBlockedError as exc:
        typer.echo(f"blocked ({exc.reason}): {exc}. Nothing was written.", err=True)
        typer.echo("Don't work around it; see docs/adapters.md.", err=True)
        raise typer.Exit(code=1) from exc
    except (CaptureError, SourceConfigError, SourceError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    replaced = ", ".join(f"{n} {kind}" for kind, n in result.counts.items()) or "nothing"
    typer.echo(f"wrote {result.path} ({result.content_type or 'no content type'})")
    typer.echo(f"replaced: {replaced}")
    typer.echo("Read the diff before committing: names in free text are only caught with --redact.")


if __name__ == "__main__":
    app()
