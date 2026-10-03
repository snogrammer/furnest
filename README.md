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

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.

## License

[MIT](LICENSE)
