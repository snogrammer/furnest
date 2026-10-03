# furnest

A self-hosted monitor for furnished rental listings. furnest watches several listing sources, filters and scores what it finds against your criteria, posts matches to Discord with action buttons, drafts inquiry emails, and tracks every property through a review pipeline in a small web dashboard.

> **Status:** early development. Nothing runs end-to-end yet. Full docs, a configuration reference and a scraping/ToS disclaimer will be added before the first release.

## Development

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 itself if you don't have it.

```sh
uv sync                       # create .venv and install dependencies
uv run furnest --help         # web | worker | migrate | healthcheck
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
- `GET /readyz`: readiness. Returns `200` when the database is reachable, migrations are at head and a worker heartbeat is newer than `WORKER_HEARTBEAT_MAX_AGE_SECONDS`, and `503` otherwise. The JSON body reports each check. With no worker running it reports `worker: no heartbeat`.

Pages send a strict self-only Content Security Policy. htmx is vendored under `src/furnest/web/static/vendor/`, so there is no CDN and no JS build step.

### Worker process

`furnest worker` runs the background work. It refuses to start until the database is at the migration head, so run `furnest migrate` first. It does three things:

- **Heartbeat:** it writes a row every `WORKER_HEARTBEAT_INTERVAL_SECONDS`, which is what `/readyz` checks.
- **Scheduled jobs:** these run on an interval with jitter. Before each run the worker takes a lease in `job_locks`, so a job never overlaps with itself, even across several workers or a restart. If a worker dies, its lease expires.
- **Job queue:** the web process adds rows to `jobs` and `WORKER_CONCURRENCY` consumer threads run them. Each run has a timeout (`WORKER_JOB_TIMEOUT_SECONDS`, or a per-handler value). A failed or timed-out run is retried after `min(WORKER_RETRY_MAX_SECONDS, WORKER_RETRY_BASE_SECONDS × 2^(attempt−1))`, jittered, until the job's `max_attempts` runs out and it is marked `dead`. Every failure is logged and sent to Sentry, tagged with the job id and type. If a worker dies mid-job, another worker reclaims the job once its lease expires.

On `SIGTERM` or `SIGINT` the worker stops taking new work and waits up to `WORKER_SHUTDOWN_TIMEOUT_SECONDS` for running jobs to finish. A queued job still running after that goes back to the queue without using up an attempt. A second signal skips the wait. Set the container's stop grace period (e.g. compose `stop_grace_period: 30s`) above the shutdown timeout. Docker's default is only 10s.

Job handlers must be idempotent and should stop when `ctx.cancelled` is set. Python can't kill a thread, so a timed-out job is told to stop, and its retry may start before the old run has fully exited.

### Dashboard auth

One token protects everything except the health probes, static files and the login page. `furnest web` refuses to start unless `AUTH_TOKEN` and `AUTH_SESSION_SECRET` are each set to a random value of at least 16 characters (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).

- **Browser:** sign in at `/login` with the token. You get a signed, HttpOnly, SameSite=Lax session cookie that lasts `AUTH_SESSION_MAX_AGE_SECONDS` (7 days by default). It is marked Secure when `AUTH_COOKIE_SECURE=true` (the default) or the request arrived over HTTPS. Sessions are stateless, so logging out only clears that browser's cookie. To sign out every browser, rotate `AUTH_TOKEN` or `AUTH_SESSION_SECRET`.
- **API/scripts:** send `Authorization: Bearer <AUTH_TOKEN>`. Without valid credentials an API request gets `401`, and a browser page load is redirected to `/login`.
- **CSRF:** every form post and htmx request carries a token tied to a signed cookie. Requests authenticated with Bearer are exempt.
- **Login rate limit:** after `AUTH_LOGIN_MAX_ATTEMPTS` failed logins from one IP within `AUTH_LOGIN_WINDOW_SECONDS`, `/login` answers `429` with `Retry-After` until the window slides. Behind a reverse proxy, list its address(es) in `WEB_TRUSTED_PROXIES` so the real client IP and scheme come from `X-Forwarded-For`/`X-Forwarded-Proto`. Otherwise those headers are ignored.

## Docker deployment

One image runs every role. The command picks it: `web` (default), `worker` or `migrate`.

```sh
docker build -t furnest .                                  # ~280 MB
docker build -t furnest:browser --build-arg WITH_PLAYWRIGHT=true .  # + Chromium for JS-heavy sources
docker run --rm -v furnest-data:/data --env-file .env furnest migrate
```

The container runs as uid 10001 and keeps all state in the `/data` volume. It sets `WEB_HOST=0.0.0.0`, `WEB_PORT=8000`, `DATA_DIR=/data` and `LOG_FORMAT=json`.

[`docker-compose.yml`](docker-compose.yml) is a Portainer-ready stack. Copy `.env.example` to `stack.env` next to it (or set the variables in Portainer), then choose the image with `FURNEST_IMAGE` / `FURNEST_TAG`:

```sh
cp .env.example stack.env   # set AUTH_TOKEN, AUTH_SESSION_SECRET, BASE_URL, ...
FURNEST_IMAGE=registry.example.com/furnest FURNEST_TAG=v0.1.0 docker compose up -d
```

`migrate` runs once and exits, then `web` and `worker` start. Each has a healthcheck (`furnest healthcheck web|worker`), memory limit and restart policy. The worker gets a 30s stop grace period, so running jobs can finish on redeploy. Alternatively, set `DB_AUTO_MIGRATE=true` to have `web` migrate on start.

Put a TLS reverse proxy in front. [`deploy/Caddyfile.example`](deploy/Caddyfile.example) is a starting point. `/discord/interactions` must be reachable from the internet, but you can restrict the dashboard to trusted networks. Set `WEB_TRUSTED_PROXIES` to the proxy's address.

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every pull request to `main`, on pushes to `main` and on `v*` tags. GitHub Actions and Gitea Actions both run it unchanged:

- **lint:** `ruff check`, `ruff format --check`, `mypy`
- **test:** `pytest` with coverage. Below `fail_under` in `pyproject.toml` the job fails. Tests can't open network sockets ([pytest-socket](https://github.com/miketheman/pytest-socket)), so a test that would hit a live site fails instead.
- **migrations:** migration round-trips plus `alembic check`
- **docker-build:** builds the image without pushing it, checks it runs as non-root, smoke-tests `migrate`/`web`/`worker` and validates `docker-compose.yml`

Run the same gates locally with `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`.

[`.github/workflows/publish.yml`](.github/workflows/publish.yml) runs after CI passes on `main`, and on `v*` tags. It builds the image for each platform in `vars.IMAGE_PLATFORMS` (default `linux/amd64`; other platforms build under QEMU) and pushes `:<sha>` as a multi-arch index, plus `:latest` from main or `:<tag>` from a tag. On main it then redeploys the Portainer stack and waits until `furnest-web` and `furnest-worker` are healthy on the new image. Everything environment-specific comes from repository or organisation settings:

| Kind | Name | Purpose |
|---|---|---|
| var | `REGISTRY` | registry host. Unset means nothing is published. |
| var | `IMAGE` | image path, e.g. `org/furnest` |
| var | `IMAGE_PLATFORMS` | e.g. `linux/amd64 linux/arm64` |
| var | `PORTAINER_URL`, `PORTAINER_STACK_ID`, `PORTAINER_ENDPOINT_ID` | redeploy target. Unset means no redeploy. |
| secret | `REGISTRY_USERNAME`, `REGISTRY_PASSWORD` | registry login |
| secret | `PORTAINER_API_KEY` | Portainer API access |

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.

## License

[MIT](LICENSE)
