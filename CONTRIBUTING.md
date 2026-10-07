# Contributing

## Setup and checks

furnest uses [uv](https://docs.astral.sh/uv/). `uv sync` creates `.venv` with the dev tools. CI runs these four checks, and all of them must pass before a merge:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Tests never touch the network: `pytest-socket` makes any socket connection fail. Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#conventions) for the conventions. Work is tracked one issue per PR, on a branch named `issue-<n>-<slug>`, with `Closes #<n>` in the PR body.

## Refreshing fixtures

Adapters are tested against recorded pages in `tests/fixtures/<adapter>/`. When a source changes its markup or fields, record a fresh copy with:

```sh
uv run python scripts/capture_fixture.py <adapter> <url> [--name FILE] [--redact NAME]... [--force]
```

The script is for manual use only. It refuses to run when `CI` is set, and nothing in the test suite calls it. It does four things:

1. It fetches the URL the way the adapter does, using that adapter's settings from your environment or `.env`: user agent, proxy, rate limit, robots.txt and block detection. A blocked page (HTTP 403, a captcha, a robots.txt disallow) stops the capture and nothing is written. Don't work around a block; see [docs/adapters.md](docs/adapters.md).
2. It sanitizes the body (see `furnest.sources.sanitize`):
   - emails become `person@example.com`
   - US phone numbers become `555-555-0100`
   - street addresses become `123 Example St`
   - names become `Jane Example`
   - coordinates are rounded to two decimals

   In JSON, and in JSON embedded in HTML (`ld+json`, `__NEXT_DATA__`), the keys say which values to replace. Names found that way are replaced everywhere else in the page too.
3. It writes the result to `tests/fixtures/<adapter>/<name>`. The default name comes from the URL path. An existing file is only replaced with `--force`.
4. It prints how many values of each kind it replaced.

**Sources behind a data provider.** Furnished Finder, Zillow and Airbnb are captured from Apify. Run the actor once with a small `max_results` (in the Apify console, or by starting a run with the adapter's own `build_input`), then capture that run's dataset:

```sh
uv run python scripts/capture_fixture.py furnished_finder \
  "https://api.apify.com/v2/datasets/<dataset id>/items?clean=true&format=json" \
  --name dataset_items.json --force
```

The script takes the token from the adapter's `api_key` in `SOURCES_ADAPTERS` and sends it as a header. A `token=` in the URL is refused, so the token can't end up in shell history or logs.

**Before committing**, read the whole diff of the fixture. Pattern matching misses things:

- Names in free text (a description signed "Thanks, Rosalind") are only caught if you pass them with `--redact Rosalind`. You can pass `--redact` more than once.
- Check listing titles, photo URLs and free-text fields for anything that identifies a person or an exact address.
- Shorten the file to the items the tests need, and replace real city names with placeholders such as `Exampleville, XX` where a test doesn't depend on them.
- Listing ids, photo URLs, map and street-view links (they carry exact coordinates), parcel numbers and named landmarks in descriptions can all lead back to an exact address. Replace them with placeholders. The sanitizer catches street addresses in URL slugs (`/70-Main-St-…/`) but not the rest.
- Update the fixture folder's `README.md` with where the file came from and when.
