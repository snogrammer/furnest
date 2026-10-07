"""The ``furnest`` command line: one image, three entry points.

Production runs the same container image with a different command:

* ``furnest web``     serves the dashboard, JSON API and Discord interactions.
* ``furnest worker``  runs scheduled source scrapes and drains the job queue.
* ``furnest migrate`` applies Alembic migrations once and exits.

``furnest healthcheck web|worker`` is the containers' health probe.
"""

from enum import StrEnum
from http import HTTPStatus
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


def _bootstrap(component: str) -> "Settings":
    """Load settings, configure redacted logging and (if configured) Sentry.

    Every command calls this first.
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
    import uvicorn

    from furnest.web.app import create_app
    from furnest.web.auth import AuthConfigError

    settings = _bootstrap("web")
    try:
        web_app = create_app(settings)
    except AuthConfigError as exc:
        typer.echo(f"furnest web: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    uvicorn.run(
        web_app,
        host=settings.web.host,
        port=settings.web.port,
        # No uvicorn logging config: its loggers propagate to the root handler set up
        # by _bootstrap, so access and error logs are redacted JSON like everything else.
        log_config=None,
        server_header=False,
        # Forwarded headers are interpreted inside the app, from WEB_TRUSTED_PROXIES.
        proxy_headers=False,
    )


@app.command()
def worker() -> None:
    """Run scheduled source scrapes and background jobs."""
    from furnest.db.amenity_cache import SqlAmenityCache
    from furnest.db.engine import create_engine_from_settings
    from furnest.db.migrate import migration_status
    from furnest.db.session import make_session_factory
    from furnest.email.imap import schedule_imap_poll
    from furnest.matching.amenities.llm import build_amenity_extractor
    from furnest.notify.digest import schedule_digest
    from furnest.pipeline.images import schedule_image_hashing
    from furnest.pipeline.ingest import ListingIngestor
    from furnest.pipeline.properties import PropertyPipeline, schedule_unsnooze
    from furnest.pipeline.schedule import register_run_now, schedule_sources
    from furnest.sources.config import SourceConfigError
    from furnest.sources.registry import discover
    from furnest.worker.execution import default_registry
    from furnest.worker.runtime import Worker

    settings = _bootstrap("worker")
    engine = create_engine_from_settings(settings)
    try:
        status = migration_status(engine)
        if not status.up_to_date:
            typer.echo(
                f"furnest worker: database at revision {status.current or 'none'}, "
                f"code expects {status.head}; run `furnest migrate` first.",
                err=True,
            )
            raise typer.Exit(code=1)
        factory = make_session_factory(engine)
        pipeline = PropertyPipeline(factory, settings=settings.pipeline, grouping=settings.grouping)
        try:
            sources = discover()
        except SourceConfigError as exc:
            typer.echo(f"furnest worker: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        jobs = default_registry(
            factory, pipeline, settings=settings, source_name=sources.display_name
        )
        worker = Worker(settings, engine, jobs)
        ingestor = ListingIngestor(
            factory,
            extractor=build_amenity_extractor(settings.llm, SqlAmenityCache(factory)),
            settings=settings.sources,
            record=pipeline.record,
            on_removed=pipeline.removed,
            on_relisted=pipeline.relisted,
        )
        schedule_unsnooze(worker, pipeline, settings.pipeline)
        schedule_digest(worker, factory, settings)
        schedule_imap_poll(worker, factory, settings)
        schedule_image_hashing(worker, factory, settings, on_hashed=pipeline.regroup)
        alert_after = settings.discord.alert_after_failures
        try:
            schedule_sources(
                worker, sources, settings.sources, factory, ingestor, alert_after=alert_after
            )
            register_run_now(
                jobs, sources, settings.sources, factory, ingestor, alert_after=alert_after
            )
        except SourceConfigError as exc:
            typer.echo(f"furnest worker: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        worker.run()
    finally:
        engine.dispose()


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


class Probe(StrEnum):
    web = "web"
    worker = "worker"


@app.command()
def healthcheck(
    probe: Annotated[Probe, typer.Argument(help="Which process to probe.")],
) -> None:
    """Exit 0 if the process is healthy, 1 if not. Used as the container healthcheck.

    ``web`` asks the local server's ``/healthz``. ``worker`` checks that the newest
    heartbeat is younger than ``WORKER_HEARTBEAT_MAX_AGE_SECONDS``. It runs every few
    seconds, so it skips logging and Sentry setup.
    """
    from furnest.config import get_settings

    settings = get_settings()
    ok, detail = _probe_web(settings) if probe is Probe.web else _probe_worker(settings)
    typer.echo(f"furnest healthcheck {probe}: {detail}", err=not ok)
    if not ok:
        raise typer.Exit(code=1)


def _probe_web(settings: "Settings") -> tuple[bool, str]:
    import urllib.error
    import urllib.request

    # Always loopback: the probe runs inside the web container, and WEB_HOST may be 0.0.0.0.
    url = f"http://127.0.0.1:{settings.web.port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except OSError as exc:
        return False, f"{url} unreachable ({exc})"
    return status == HTTPStatus.OK, f"{url} answered {status}"


def _probe_worker(settings: "Settings") -> tuple[bool, str]:
    from datetime import timedelta

    from furnest.db.engine import create_engine_from_settings
    from furnest.web.health import check_worker

    engine = create_engine_from_settings(settings)
    try:
        result = check_worker(engine, timedelta(seconds=settings.worker.heartbeat_max_age_seconds))
    finally:
        engine.dispose()
    return result.ok, f"heartbeat {result.detail}"
