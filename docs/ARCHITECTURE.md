# Architecture

furnest watches furnished-rental listing sources, matches what it finds against one set of search criteria, posts matches to a Discord channel, sends inquiry emails, and tracks each property through a review pipeline in a web dashboard.

This document is the reference design. Each part is built by a tracked issue, and this file changes along with the code.

## Guiding decisions

- **One versioned criteria profile, one Discord channel.** The app serves a single searcher. Criteria are versioned so that matches can always be explained against the version that produced them.
- **US-first, but not US-only.** Address parsing (usaddress) and currency (USD) sit behind interfaces, so other locales can be added later without touching the matching code.
- **No personal data in the repo.** Domains, emails, names and locations come from environment variables or DB-stored settings. Examples use `example.com` and placeholder values.
- **Prefer official APIs and feeds, fall back to scraping.** Scraping is polite (low volume, robots.txt where feasible) and degrades gracefully: a blocked source records a failure and never crashes the worker.

## Process model

One container image runs three commands, all sharing a SQLite database on the `/data` volume:

| Command | Role |
|---|---|
| `furnest web` | FastAPI app: dashboard (Jinja2 + HTMX, no JS build step), Bearer JSON API, `POST /discord/interactions`, `/healthz`, `/readyz`, optional `/metrics`. |
| `furnest worker` | Scheduler: one job per enabled source on its own interval with jitter. Also drains the DB job queue (inquiry sends, enrichment, criteria re-runs, Discord message sync) and writes a heartbeat. |
| `furnest migrate` | Applies Alembic migrations once and exits. Web can also auto-migrate on start. |

`furnest healthcheck web|worker` is each container's health probe: `web` asks its own `/healthz`, `worker` checks that the newest heartbeat is fresh. `docker-compose.yml` runs `migrate` as a one-shot service that must exit 0 before `web` and `worker` start (both refuse an out-of-date schema). The image runs as uid 10001, with the venv root-owned and read-only. A build arg adds Playwright and Chromium for sources that need a browser.

SQLite runs in WAL mode with `busy_timeout` and foreign keys on, so web and worker can share it safely. The worker does all slow or network-bound work. The web process only reads, writes small rows, and enqueues jobs.

```
             ┌──────────────┐  enqueue jobs   ┌──────────────┐
 Browser ───▶│  furnest web │────────────────▶│ furnest      │──▶ listing sources
 Discord ───▶│  (FastAPI)   │◀── SQLite WAL ─▶│ worker       │──▶ SMTP / IMAP
             └──────────────┘    on /data     │ (scheduler)  │──▶ Discord REST, geocoder
                                              └──────────────┘
```

## Directory layout

```
src/furnest/
  cli.py          furnest web | worker | migrate | healthcheck
  config.py       pydantic-settings; env → DB settings precedence           (#3)
  logging.py      structured JSON logs with secret redaction                (#5)
  observability.py Sentry init (no-op without SENTRY_DSN), release/env tags (#5)
  db/             engine (WAL, busy_timeout, FK), models/, migrations/      (#4)
  domain/         NormalizedListing, Criteria, money, rent periods, geo,
                  ParsedAddress + AddressParser interface                   (P2)
  matching/       hard filters, scoring, amenities/ (keyword, optional LLM) (P2)
  sources/        base (SourceAdapter), config, registry, http, browser
                  (optional), adapters/<name>/                               (P3, P7)
  dedup/          address, geo, phash, text, grouping                       (P7)
  pipeline/       property state machine, audit events                      (P4)
  notify/         Discord signature, client, embeds, interactions           (P5)
  email/          smtp, templates, imap                                     (P6)
  enrichment/     geocoding, reverse_image, alternatives, risk              (P8)
  worker/         scheduler, jobs, locks, heartbeat, queue                  (#8)
  web/            app, auth, csrf, ratelimit, headers, routes/,
                  templates/, static/ (vendored htmx, brand/ icons)         (#6, #7, #78)
tests/
  unit/           parsers, matching, scoring, dedup, state machine
  integration/    API routes, auth, Discord signatures + interactions, email
  migrations/     upgrade/downgrade round-trips against an empty DB
  fixtures/<adapter>/  recorded HTML/JSON per adapter (sanitized)
scripts/capture_fixture.py  records a fixture (manual only; sources/capture.py,
                  sources/sanitize.py; see CONTRIBUTING.md)                  (#25)
Dockerfile, docker-compose.yml   one image, Portainer-ready stack        (#9)
deploy/Caddyfile.example
docs/             ARCHITECTURE, adapters, BRAND (+ brand/ source artwork)
.github/workflows/ci.yml   (GitHub Actions syntax, portable to Gitea Actions)
```

Dependencies point inward. `domain` imports nothing from the rest of the app. `matching`, `dedup` and `pipeline` depend on `domain`. `sources`, `notify`, `email` and `enrichment` are I/O edges. `web` and `worker` sit on top and wire everything together.

## Data model

Each phase adds its tables in its own Alembic migration.

| Table | Purpose | Phase |
|---|---|---|
| `settings` | DB-stored, dashboard-editable settings (key/value, typed) | P1 |
| `jobs` | Web → worker job queue (kind, payload, status, attempts, run_after) | P1 |
| `job_locks` | Lease locks with expiry, so a source never runs concurrently with itself | P1 |
| `worker_heartbeat` | Last-seen timestamp read by `/readyz` | P1 |
| `criteria_versions` | Immutable criteria snapshots; one is active | P2 |
| `sources` | Per-adapter dashboard toggle (NULL = not set), health counters, last error, why it can't run (`disabled_reason`) | P3 |
| `source_runs` | One row per scrape run: timings, counts, outcome | P3 |
| `listings` | Normalized listing, unique on `(source, source_id)`, FK to property, raw payload JSON, first/last seen | P3 |
| `price_history` | Price + period observations per listing | P3 |
| `properties` | A physical unit: status, canonical address/geo, score + match breakdown, criteria version, `snoozed_until`, `manual_contact_required`, Discord message reference | P4 |
| `notes` | Markdown notes per property (deny reasons included) | P4 |
| `audit_events` | Every status transition and notable action, with actor (`dashboard`, `discord`, `system`) | P4 |
| `discord_interactions` | Interaction IDs already processed (idempotency) | P5 |
| `email_templates` | Editable inquiry templates and signature | P6 |
| `emails` | Every sent or drafted email with its rendered body | P6 |
| `listing_images` | Image URLs and perceptual hashes | P7 |
| `enrichment_results` | Alternative listings, reverse-image hits, geocodes | P8 |
| `risk_flags` | Scam and risk signals shown on the property page | P8 |

## Key mechanics

**Listing flow.** worker → adapter `search(criteria)` → `NormalizedListing`s → upsert (exact dedup on `(source, source_id)`, price history, `last_seen`) → group into a `Property` → match (hard filters pass/fail + 0–100 score with a breakdown) → `suggested` → Discord notification job. Listings no longer seen move their property to `unavailable`.

**Ingestion.** `ListingIngestor` (`pipeline/ingest.py`) runs one adapter once, and the scheduler (`pipeline/schedule.py`) calls it. Without an active criteria profile it does nothing. Otherwise it opens a `source_runs` row (`running`), calls `search` with the active criteria and passes the results to `SqlListingStore` (`db/listing_store.py`). Listings are deduplicated exactly on `(source, source_id)`, and a repeat within one batch keeps the last. A new listing is stored with its photos (`listing_images`) and first price. A listing seen before gets a new `last_seen`, and its data and photos are rewritten only when its content hash changes: a SHA-256 of the listing's JSON with amenities sorted, ignoring `first_seen`, `last_seen` and `raw`. `first_seen` never moves. `price_history` gets a row whenever the price (amount, range, currency, period) differs from the last recorded one, so it holds changes, not sightings. A listing that loses its price keeps the last known one. Every active listing of the source that the run didn't return gets `missed_runs + 1`, and is marked `removed_at` once it has missed `SOURCES_REMOVE_AFTER_MISSED_RUNS` runs *and* gone unseen for `SOURCES_REMOVE_AFTER_HOURS`, so neither one short search nor a long outage removes anything alone. Seeing it again clears both (relisted). The run row is then finished as `success` with found, new, updated (changed or relisted) and removed counts. If the search or the store raises, it is finished as `failed` with the error, nothing is marked missing, and the exception propagates to the scheduler. After the write commits, which keeps slow LLM calls off SQLite's single writer, each new, changed or relisted listing has its amenities extracted (stored on the row) and goes through `match_listing`. The result goes to a sink, which discards it until #28 connects properties. A listing whose extraction or matching fails is logged and skipped. The full `NormalizedListing` is kept as JSON in `listings.data`, so it reads back exactly. `listings.property_id` points at the listing's property (`properties`, set to NULL if the property is deleted).

**Money and rent periods.** Prices are integer minor units (cents) plus a currency code from a small registry in `domain/money.py`; USD is the default and other currencies are added by registering them. A `Price` keeps the amount (or range) and period the source quoted, and matching compares its monthly equivalent: nightly × 365/12, weekly × 52/12, monthly × 1. The factors are exact fractions and the result is rounded half-up once, to a whole cent. An unknown price is `None`, never 0. `NormalizedListing` (`domain/listing.py`) is the frozen shape every adapter returns; unknown fields stay `None`, and raw property-type labels are mapped onto `PropertyType` with unrecognised ones becoming `other`.

**Criteria versions.** The search profile is a frozen `Criteria` (`domain/criteria.py`). `CriteriaStore` (`db/criteria_store.py`) appends every save to `criteria_versions` as version `max + 1`, and never edits or deletes a version. Activating a version, new or old, clears the previous flag in the same transaction. A partial unique index (`WHERE is_active = 1`) makes a second active row impossible, and the first version saved is always active, so a non-empty history has exactly one. `diff(a, b)` lists the top-level fields that changed. Collections are stored sorted and de-duplicated, so equal profiles compare and serialize identically.

**Criteria editor.** `/criteria` edits the active profile. `web/criteria_form.py` maps the flat form onto `Criteria`. Blank inputs mean not set. Prices are typed in major units with one currency. ZIP codes, neighborhoods and keywords are one per line or comma. Property types, must-haves and `unknown_fails` are checkbox groups. Areas and nice-to-have weights are repeated `areas-<i>-*` / `nice-<i>-*` rows, which htmx adds with an out-of-band "add" button and a remove checkbox drops, so no client script is needed. Validation errors come back as 422 with a message beside each field (htmx is configured to swap 409/422). A valid save appends a version through `CriteriaStore`, and a save that changes nothing is reported, not stored. `/criteria/history` lists versions newest first with each one's diff against the one before it, and can activate any of them. "Re-run criteria" queues a `rerun_criteria` job pinned to the active version. Its handler (`pipeline/rerun.py`) runs `match_listing` on every stored listing and hands each result to a sink. Its listing source stays empty until #28 connects it to `SqlListingStore.active()` and the property store.

**Location matching.** `matching/location.py` checks a listing against the criteria's areas (haversine distance ≤ radius, in each area's own mi/km unit), ZIP codes (ZIP+4 compares as its 5-digit ZIP) and neighborhoods (case- and whitespace-insensitive). Any one match qualifies. A criterion the listing has no data for is skipped and noted as "location unknown". The result is `match`, `no_match` (everything was checked), `unknown` (nothing matched but something couldn't be checked), or `not_configured`, with a reason per check. The hard filters decide what `unknown` means. Addresses are parsed through the `AddressParser` protocol (`domain/address.py`) into a `ParsedAddress` with USPS-style normalization and a `canonical` string for dedup. `dedup/address.py` implements it for the US with `usaddress`, and `PassthroughAddressParser` handles other countries and input that won't tag.

**Hard filters.** `matching/filters.py` checks a listing against each hard constraint in the criteria: location, monthly price range, property type allow and deny, minimum beds, baths and sqft, furnished, move-in window, lease range, must-have amenities, and keyword allow and deny on title and description. Each rule (`FilterRule`) returns `pass`, `fail` or `unknown` with a human-readable reason. Rules the criteria don't configure are left out. Unknown means the listing lacks the data, for example no price, no bed count, or an amenity it never mentions. Unknown passes unless the rule is listed in the criteria's `unknown_fails`, so that choice is versioned with the profile. A quoted price range passes if it overlaps the wanted range, and a price in another currency is unknown. An unrecognized property type is unknown unless `other` is named explicitly. A listing available before the move-in window passes. A must-have that the amenity result rules out fails, while one that is only unmentioned is unknown. Keywords match case-insensitively on word boundaries. `evaluate_hard_filters(listing, criteria, amenities=..., parser=...)` is pure and returns every rule's verdict plus `passed`.

**Scoring.** `matching/scoring.py` ranks listings that pass the hard filters. Every scoring component has a weight in the criteria and earns a fraction of it from 0 to 1. A nice-to-have amenity earns its full weight when the listing states it, and nothing when it is ruled out or never mentioned. Two fit components are opt-in. Price fit (`price_fit_weight`, which needs `price_max`) earns more the lower the monthly price sits in the wanted range, using the midpoint of a quoted range and measuring from zero when there is no minimum. Extra beds (`extra_beds_weight`, which needs `min_beds`) earns half for one bed above the minimum and the full weight for two or more. The score is the earned share of the total weight, scaled to 0–100 and rounded half-up, so only the ratio between weights matters. Each breakdown item (label, points, max points, reason) uses the same 0–100 scale, so the items add up to the score. Criteria with nothing to score give every listing 100. `match_listing(listing, criteria, criteria_version=...)` runs the filters and scoring together and returns a `MatchResult` (passed, score, breakdown, every rule verdict, criteria version). Failed listings are scored too, but only passing ones are ranked. The result is a pydantic model that round-trips through JSON, and the property row (#27) stores it with the criteria version that produced it.

**Amenity extraction.** `domain/amenities.py` holds the one tag vocabulary (`Amenity`, e.g. `in_unit_laundry`, `pets_allowed`, `ac`). It also holds the `AmenityExtractor` protocol, whose `AmenityResult` keeps `present`, `absent` (explicitly ruled out, e.g. "no pets") and `unrecognized` adapter labels apart. A tag in neither set is unknown. `matching/amenities/` implements the protocol deterministically. `vocabulary.py` maps regex phrases to tags, and `keyword.py` scans clauses split at punctuation, commas and "but". A negator up to three words before a phrase, or right after it ("not included"), flips it, so negation never crosses a clause. Within a source, absent beats present. The adapter's own labels override the free text. "Cats ok" implies pets are allowed, and "no pets" rules out cats and dogs that weren't stated. `matching/amenities/llm.py` adds an optional LLM pass, which is off unless `LLM_PROVIDER=openai_compatible` with `LLM_BASE_URL` and `LLM_MODEL` set. It calls any OpenAI-compatible `/chat/completions` endpoint, so a local Ollama works. The reply must match a strict JSON schema (`present`/`absent` lists of tags). The keyword result always stands, and LLM verdicts only fill tags it left unknown. Pets are reconciled again after the merge. A transport error, timeout, non-2xx status, malformed reply or unknown tag is logged without listing text, and the keyword result is used alone. Successful LLM verdicts are cached in `amenity_extractions` (`db/amenity_cache.py`), keyed by a SHA-256 of prompt version, model, title, description and adapter labels. An unchanged listing never calls the model twice, and failures are not cached. `build_amenity_extractor(settings.llm, cache)` picks the extractor.

**Sources.** An adapter subclasses `SourceAdapter` (`sources/base.py`). It declares `name`, `display_name`, `capabilities` (needs a browser, has an API, supports geo search, provides contact details, requires an API key) and `default_enabled`. It implements `search(criteria)`, which returns `NormalizedListing`s, and optionally `fetch_detail(listing)`. Adapters are synchronous because they run in worker threads. `discover()` (`sources/registry.py`) imports every module under `sources/adapters/` and registers each concrete subclass into a `SourceRegistry`. Adding a source is just adding a module. `resolve_config` (`sources/config.py`) builds each adapter's `AdapterConfig`, falling back field by field from its entry in the `SOURCES_ADAPTERS` JSON to the global `SOURCES_*` defaults (interval, jitter, requests per minute, user agent, proxy, timeouts, max results; the API key and search locations are per adapter only). `enabled` has the same precedence as runtime settings. An `enabled` set in `SOURCES_ADAPTERS` pins the source. Otherwise the dashboard toggle stored in `sources.enabled` decides, and if that was never set the adapter's `default_enabled` applies. Naming an unknown adapter in `SOURCES_ADAPTERS` raises `SourceConfigError` instead of being ignored. An adapter whose capabilities say `requires_api_key` is disabled, with a `disabled_reason`, until its entry has an `api_key`. Per-adapter keys are redacted from logs and Sentry like every other secret. `search_locations` (place names, per adapter) and `max_results` serve sources that search by city or bill per result. Sources that can't be fetched directly go through a data provider: `sources/apify.py` starts an Apify actor run, long-polls it, and reads its dataset, with the token in a header. `docs/adapters.md` records each source's access options and field mapping. `SqlSourceStore` (`db/source_store.py`) creates a `sources` row per registered adapter and records each run's success or failure (consecutive failures, last error truncated to 2000 chars, listings found).

**Source HTTP layer.** Adapters make requests through `SourceHttpClient` (`sources/http.py`), built from their `AdapterConfig`, which supplies the timeout, user agent and proxy. Each request goes through four steps. First, a robots.txt check (`sources/robots.py`), unless `respect_robots` is off. The file is cached per origin for 24 hours. A 4xx means no rules. A 5xx or unreachable file raises `SourceError` and is not cached, as RFC 9309 says. A disallowed URL raises `SourceBlockedError` before anything is sent. Second, every attempt, retries and robots.txt fetches included, takes a token from the adapter's `TokenBucket` (`sources/ratelimit.py`, `requests_per_minute`, no bursts, thread-safe). Third, a 429, a 5xx, a timeout or a transport error is retried up to `max_retries` times, with exponential backoff and full jitter (up to `2**n` seconds, capped at 60s). A `Retry-After` header replaces the backoff, and one over five minutes ends the attempt instead. Fourth, the response is classified. A 403, or an HTML 2xx page with a captcha or challenge marker in its first 64 KB, raises `SourceBlockedError` (adapters can add their own markers). Any other 4xx, or retries running out, raises `SourceError`. Both errors carry the URL and the status. `respect_robots` and `max_retries` follow the same `SOURCES_*` → `SOURCES_ADAPTERS` fallback as the other per-adapter settings.

**Browser layer (optional).** Sources that only render with JavaScript set `Capabilities.needs_browser` and load pages through `SourceBrowser` (`sources/browser.py`), headless Chromium driven by Playwright. Playwright is the `browser` extra and is imported only when a browser starts, so the app runs without it. `browser_unavailable_reason` checks `SOURCES_BROWSER_ENABLED` and whether Playwright is installed, without launching anything. When either is missing, `SourceRegistry.configured` disables every `needs_browser` adapter, even one pinned on in `SOURCES_ADAPTERS`, and puts the reason in `AdapterConfig.disabled_reason`. The worker stores it in `sources.disabled_reason` (`SqlSourceStore.set_disabled_reasons`). A missing Chromium binary raises `BrowserUnavailableError` at launch. Each browser context takes the adapter's user agent, proxy and timeout, and aborts image, font and media requests before they are sent. `SourceBrowser.render(url)` shares the adapter's `SourceHttpClient`, so robots.txt rules and the token bucket are the same as over HTTP: one token per navigation, while subresources the page loads itself are not counted. A 403 or a captcha marker raises `SourceBlockedError`, and Playwright errors and timeouts raise `SourceError`.

**Property state machine.** `domain/status.py` holds `PropertyStatus`, the actors (`dashboard`, `discord`, `system`) and `TRANSITIONS`, the full allow-list:

```
new → suggested → approved | denied
approved → inquiry_drafted → inquiry_sent → in_progress → responded | touring | applied → won | lost | archived
any active state → unavailable (listing disappeared) → back to that state
```

Beyond that path, `archived` is reachable from every state except itself and is terminal. Any state before the inquiry is sent can be `denied`, and `denied → approved` undoes a denial. `inquiry_drafted → approved` throws a draft away. `inquiry_sent → responded` covers a reply detected before anything else happened, and `lost` is reachable from any state after the inquiry is sent. Only the `system` actor moves a property into `unavailable`. Coming back from it, the property must go to the state it left, read from the latest audit event into `unavailable`, so a relisted property keeps the human decision that was made on it. People can still archive it. Moving to the same state is an error. `SqlPropertyStore.transition` (`db/property_store.py`) is the only status writer: in one transaction it checks the move with `check_transition` and writes the status, `status_changed_at` and an `audit_events` row (from, to, actor, actor detail, payload). The transaction takes the write lock up front, so of two concurrent clicks the second sees the first one's result and is rejected. Until P7 groups listings, `ensure_for_listing` gives each listing its own property in `new`, with the listing's geo and canonical address, and the creation is the first audit event (`from_status` NULL). Wiring ingestion and re-runs to it is #28.

**Migrations and foreign keys.** SQLite can't alter most of a table in place, so Alembic rebuilds it, and dropping the old table with foreign keys on would cascade into every table pointing at it (adding `listings.property_id`'s foreign key would have emptied `listing_images` and `price_history`). Migrations therefore run as one transaction on a connection with foreign keys off (`foreign_keys_off` in `db/engine.py`), finish with `PRAGMA foreign_key_check`, and turn enforcement back on before the connection returns to the pool.

**Discord.** furnest uses an HTTP interactions endpoint (Ed25519-verified), not a gateway bot. The web process must reply within 3 seconds, so it sends a deferred response, records the interaction ID for idempotency, applies the state change, and enqueues slow work (email sending, enrichment) for the worker. The worker then edits the original message to show the new state.

**Scheduling and locking.** At startup `schedule_sources` (`pipeline/schedule.py`) ensures a `sources` row for every adapter, stores each `disabled_reason`, and registers one scheduled job, `source:<name>`, per source that can run. Each job uses that source's interval plus random jitter and its run timeout (`SOURCES_RUN_TIMEOUT_SECONDS`, overridable per adapter). Before a run the worker takes the DB lease `scheduled:source:<name>` in `job_locks`, so overlapping runs of the same source are impossible, even across workers and restarts. Leases expire if the worker dies. Every source has its own job and its own daemon thread per run, so a slow, stuck or failing source never holds up the others. Each run resolves the dashboard toggle again and skips a source that is now disabled, which is why a source toggled off is still scheduled: turning it back on takes effect without a restart. Sources pinned off in `SOURCES_ADAPTERS` or unable to run are not scheduled. A successful run resets `consecutive_failures` and stores `listings_found`. An error or a timeout bumps `consecutive_failures`, stores `last_error` and goes to Sentry tagged with `source`. When a run times out, `ctx.cancelled` is set, and the ingestor throws away a search that returns after that instead of storing it late. A source that fails N runs in a row raises a Discord alert (#38).

**Source health page.** `/sources` (`web/routes/sources.py`, view-model in `web/source_health.py`) lists every registered adapter, including ones with no `sources` row yet. For each it shows a status (ok, failing, never run, disabled, unavailable), last run and last success, listings found, consecutive failures, the number of failed `source_runs`, and the last error. Errors are autoescaped and cut to 160 characters, with the full text in a `<details>`. Under the table are each source's 10 most recent runs. The web process resolves `enabled` the same way the scheduler does, but it never probes for a browser. It shows the `disabled_reason` the worker stored instead. The enable toggle writes `sources.enabled`, and the scheduler picks the change up on the next run. A source pinned in `SOURCES_ADAPTERS` or unable to run answers 409. "Run now" queues one `run_source` job (`max_attempts=1`), unless one for that source is already queued or running. Its handler, `RunSourceNow`, takes the same `scheduled:source:<name>` lease as the scheduled job. If the source is already running, the job does nothing, because that run is the one the user asked for. Otherwise it runs the source through `SourceRun` and records the outcome on the `sources` row like a scheduled run.

**Job queue and timeouts.** `jobs` rows go `queued → running → succeeded`. A failed run goes to `failed`, with `run_after` pushed back by exponential backoff, and to `dead` once its attempts run out. A claim counts the attempt and leases the row (`locked_by`/`locked_until`) for longer than the job's timeout. Rows whose lease has expired are reclaimed, and `complete`/`fail` only change a row the caller still holds. Each run gets its own daemon thread. A timeout stops the wait and sets `ctx.cancelled`, but Python can't stop the thread itself, so handlers are idempotent and cooperative. On shutdown the worker drains for up to `WORKER_SHUTDOWN_TIMEOUT_SECONDS`, then puts unfinished jobs back on the queue without counting the attempt.

**Configuration.** Infrastructure and secrets come from environment variables (pydantic-settings, documented in `.env.example`). User preferences edited in the dashboard (criteria, templates, toggles) live in the DB. Env vars always win for anything defined in both places.

**Safety.** Scraped HTML is never rendered raw: it is sanitized with nh3 or reduced to text. Pages send a strict self-only CSP, so the brand icons (`web/static/brand/`, see [BRAND.md](BRAND.md)) are served locally and fonts are the system stack. `/favicon.ico` is a public route at the root, because browsers request it there before anyone signs in. htmx is vendored and configured not to inject inline styles or `eval` code, so the CSP needs no `unsafe-*` exceptions. Logs redact secrets. The container runs as a non-root user. The dashboard is protected by a single token (signed HttpOnly SameSite cookie or `Authorization: Bearer`, constant-time comparison, CSRF on forms, rate-limited login).

## Conventions

- `uv` for dependencies, with `uv.lock` committed. `ruff` for lint and format, `mypy --strict`, `pytest` with coverage. All four must pass before merge.
- Tests make no live network calls. Adapters are tested against recorded fixtures, which are sanitized of personal data and refreshed by hand with `scripts/capture_fixture.py` (see [CONTRIBUTING.md](../CONTRIBUTING.md#refreshing-fixtures)).
- Every schema change ships with an Alembic migration and a migration test.
- Every new setting is documented in `.env.example`.
- Optional integrations (Sentry, LLM extractor, reverse image search, IMAP, Prometheus, Playwright) do nothing when unconfigured.
- Work is tracked one issue per PR: branch `issue-<n>-<slug>`, PR body says `Closes #<n>`.
