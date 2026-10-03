# furnest

A self-hosted monitor for furnished rental listings. furnest watches several listing sources, filters and scores what it finds against your criteria, posts matches to Discord with action buttons, drafts inquiry emails, and tracks every property through a review pipeline in a small web dashboard.

> **Status:** early development. Nothing runs end-to-end yet. Full docs, a configuration reference and a scraping/ToS disclaimer will be added before the first release.

## Development

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 itself if you don't have it.

```sh
uv sync                       # create .venv and install dependencies
uv run furnest --help         # web | worker | migrate
uv run pytest                 # tests + coverage
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pre-commit install     # optional: run the same checks on every commit
```

Schema changes ship as Alembic migrations in `src/furnest/db/migrations/versions/`:

```sh
export DB_URL=sqlite:///./dev.db
uv run furnest migrate                                    # upgrade to head
uv run alembic revision --autogenerate -m "add listings"  # then review the generated file
uv run alembic check                                      # models and migrations agree
```

## Configuration

furnest is configured through environment variables, or a `.env` file in the working directory. [`.env.example`](.env.example) documents every setting. Copy it to `.env` and fill in what you use. Optional integrations (Sentry, LLM, geocoding, reverse image search, IMAP) stay off until configured.

A few preferences, such as `INQUIRY_MODE`, can also be changed from the dashboard. An env var that is explicitly set always wins over the dashboard value.

### Logging and Sentry

Every process logs to stderr, one JSON object per line by default. Set `LOG_FORMAT=console` for human-readable output and `LOG_LEVEL` to control verbosity. Secrets are redacted before anything is written: configured tokens and passwords, `SecretStr` values, `Authorization`/cookie headers, bearer tokens and credentials embedded in URLs. When `SENTRY_DSN` is set, errors go to Sentry, tagged with release, environment and component (`web`/`worker`), without default PII and after the same redaction.

### Web process

`furnest web` serves the dashboard on `WEB_HOST:WEB_PORT` (default `127.0.0.1:8000`; set `WEB_HOST=0.0.0.0` in a container). With `DB_AUTO_MIGRATE=true` it applies migrations on start. Otherwise run `furnest migrate` first.

Two unauthenticated endpoints are for orchestrators and uptime checks:

- `GET /healthz`: liveness. Always `200 {"status": "ok"}` while the process is serving. It does not touch the database.
- `GET /readyz`: readiness. Returns `200` when the database is reachable, migrations are at head and a worker heartbeat is newer than `WORKER_HEARTBEAT_MAX_AGE_SECONDS`, and `503` otherwise. The JSON body reports each check. Until a worker runs (#8), `/readyz` reports `worker: no heartbeat`.

Pages send a strict self-only Content Security Policy. htmx is vendored under `src/furnest/web/static/vendor/`, so there is no CDN and no JS build step.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.

## License

[MIT](LICENSE)
