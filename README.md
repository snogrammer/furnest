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

### Dashboard auth

One token protects everything except the health probes, static files and the login page. `furnest web` refuses to start unless `AUTH_TOKEN` and `AUTH_SESSION_SECRET` are each set to a random value of at least 16 characters (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).

- **Browser:** sign in at `/login` with the token. You get a signed, HttpOnly, SameSite=Lax session cookie that lasts `AUTH_SESSION_MAX_AGE_SECONDS` (7 days by default). It is marked Secure when `AUTH_COOKIE_SECURE=true` (the default) or the request arrived over HTTPS. Sessions are stateless, so logging out only clears that browser's cookie. To sign out every browser, rotate `AUTH_TOKEN` or `AUTH_SESSION_SECRET`.
- **API/scripts:** send `Authorization: Bearer <AUTH_TOKEN>`. Without valid credentials an API request gets `401`, and a browser page load is redirected to `/login`.
- **CSRF:** every form post and htmx request carries a token tied to a signed cookie. Requests authenticated with Bearer are exempt.
- **Login rate limit:** after `AUTH_LOGIN_MAX_ATTEMPTS` failed logins from one IP within `AUTH_LOGIN_WINDOW_SECONDS`, `/login` answers `429` with `Retry-After` until the window slides. Behind a reverse proxy, list its address(es) in `WEB_TRUSTED_PROXIES` so the real client IP and scheme come from `X-Forwarded-For`/`X-Forwarded-Proto`. Otherwise those headers are ignored.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.

## License

[MIT](LICENSE)
