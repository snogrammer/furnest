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
  sources/        base (SourceAdapter), registry, http, browser (optional),
                  adapters/<name>/                                          (P3, P7)
  dedup/          address, geo, phash, text, grouping                       (P7)
  pipeline/       property state machine, audit events                      (P4)
  notify/         Discord signature, client, embeds, interactions           (P5)
  email/          smtp, templates, imap                                     (P6)
  enrichment/     geocoding, reverse_image, alternatives, risk              (P8)
  worker/         scheduler, jobs, locks, heartbeat, queue                  (#8)
  web/            app, auth, csrf, ratelimit, headers, routes/,
                  templates/, static/ (vendored htmx)                        (#6, #7)
tests/
  unit/           parsers, matching, scoring, dedup, state machine
  integration/    API routes, auth, Discord signatures + interactions, email
  migrations/     upgrade/downgrade round-trips against an empty DB
  fixtures/<adapter>/  recorded HTML/JSON per adapter (sanitized)
Dockerfile, docker-compose.yml   one image, Portainer-ready stack        (#9)
deploy/Caddyfile.example
docs/
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
| `sources` | Per-adapter enablement, interval, health counters, last error | P3 |
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

**Money and rent periods.** Prices are integer minor units (cents) plus a currency code from a small registry in `domain/money.py`; USD is the default and other currencies are added by registering them. A `Price` keeps the amount (or range) and period the source quoted, and matching compares its monthly equivalent: nightly × 365/12, weekly × 52/12, monthly × 1. The factors are exact fractions and the result is rounded half-up once, to a whole cent. An unknown price is `None`, never 0. `NormalizedListing` (`domain/listing.py`) is the frozen shape every adapter returns; unknown fields stay `None`, and raw property-type labels are mapped onto `PropertyType` with unrecognised ones becoming `other`.

**Criteria versions.** The search profile is a frozen `Criteria` (`domain/criteria.py`). `CriteriaStore` (`db/criteria_store.py`) appends every save to `criteria_versions` as version `max + 1`, and never edits or deletes a version. Activating a version, new or old, clears the previous flag in the same transaction. A partial unique index (`WHERE is_active = 1`) makes a second active row impossible, and the first version saved is always active, so a non-empty history has exactly one. `diff(a, b)` lists the top-level fields that changed. Collections are stored sorted and de-duplicated, so equal profiles compare and serialize identically.

**Location matching.** `matching/location.py` checks a listing against the criteria's areas (haversine distance ≤ radius, in each area's own mi/km unit), ZIP codes (ZIP+4 compares as its 5-digit ZIP) and neighborhoods (case- and whitespace-insensitive). Any one match qualifies. A criterion the listing has no data for is skipped and noted as "location unknown". The result is `match`, `no_match` (everything was checked), `unknown` (nothing matched but something couldn't be checked), or `not_configured`, with a reason per check. The hard filters decide what `unknown` means. Addresses are parsed through the `AddressParser` protocol (`domain/address.py`) into a `ParsedAddress` with USPS-style normalization and a `canonical` string for dedup. `dedup/address.py` implements it for the US with `usaddress`, and `PassthroughAddressParser` handles other countries and input that won't tag.

**Hard filters.** `matching/filters.py` checks a listing against each hard constraint in the criteria: location, monthly price range, property type allow and deny, minimum beds, baths and sqft, furnished, move-in window, lease range, must-have amenities, and keyword allow and deny on title and description. Each rule (`FilterRule`) returns `pass`, `fail` or `unknown` with a human-readable reason. Rules the criteria don't configure are left out. Unknown means the listing lacks the data, for example no price, no bed count, or an amenity it never mentions. Unknown passes unless the rule is listed in the criteria's `unknown_fails`, so that choice is versioned with the profile. A quoted price range passes if it overlaps the wanted range, and a price in another currency is unknown. An unrecognized property type is unknown unless `other` is named explicitly. A listing available before the move-in window passes. A must-have that the amenity result rules out fails, while one that is only unmentioned is unknown. Keywords match case-insensitively on word boundaries. `evaluate_hard_filters(listing, criteria, amenities=..., parser=...)` is pure and returns every rule's verdict plus `passed`.

**Amenity extraction.** `domain/amenities.py` holds the one tag vocabulary (`Amenity`, e.g. `in_unit_laundry`, `pets_allowed`, `ac`). It also holds the `AmenityExtractor` protocol, whose `AmenityResult` keeps `present`, `absent` (explicitly ruled out, e.g. "no pets") and `unrecognized` adapter labels apart. A tag in neither set is unknown. `matching/amenities/` implements the protocol deterministically. `vocabulary.py` maps regex phrases to tags, and `keyword.py` scans clauses split at punctuation, commas and "but". A negator up to three words before a phrase, or right after it ("not included"), flips it, so negation never crosses a clause. Within a source, absent beats present. The adapter's own labels override the free text. "Cats ok" implies pets are allowed, and "no pets" rules out cats and dogs that weren't stated. `matching/amenities/llm.py` adds an optional LLM pass, which is off unless `LLM_PROVIDER=openai_compatible` with `LLM_BASE_URL` and `LLM_MODEL` set. It calls any OpenAI-compatible `/chat/completions` endpoint, so a local Ollama works. The reply must match a strict JSON schema (`present`/`absent` lists of tags). The keyword result always stands, and LLM verdicts only fill tags it left unknown. Pets are reconciled again after the merge. A transport error, timeout, non-2xx status, malformed reply or unknown tag is logged without listing text, and the keyword result is used alone. Successful LLM verdicts are cached in `amenity_extractions` (`db/amenity_cache.py`), keyed by a SHA-256 of prompt version, model, title, description and adapter labels. An unchanged listing never calls the model twice, and failures are not cached. `build_amenity_extractor(settings.llm, cache)` picks the extractor.

**Property state machine.** Status changes only through `pipeline`, which checks the transition against an explicit allow-list and writes an `audit_events` row in the same transaction.

```
new → suggested → approved | denied
approved → inquiry_drafted → inquiry_sent → in_progress → responded | touring | applied → won | lost | archived
any active state → unavailable (listing disappeared)
```

**Discord.** furnest uses an HTTP interactions endpoint (Ed25519-verified), not a gateway bot. The web process must reply within 3 seconds, so it sends a deferred response, records the interaction ID for idempotency, applies the state change, and enqueues slow work (email sending, enrichment) for the worker. The worker then edits the original message to show the new state.

**Scheduling and locking.** Each source has its own interval plus random jitter. Before a run the worker takes a DB lease in `job_locks`, so overlapping runs of the same source are impossible even across restarts. Leases expire if the worker dies. A source that fails N runs in a row raises a Discord alert.

**Job queue and timeouts.** `jobs` rows go `queued → running → succeeded`. A failed run goes to `failed`, with `run_after` pushed back by exponential backoff, and to `dead` once its attempts run out. A claim counts the attempt and leases the row (`locked_by`/`locked_until`) for longer than the job's timeout. Rows whose lease has expired are reclaimed, and `complete`/`fail` only change a row the caller still holds. Each run gets its own daemon thread. A timeout stops the wait and sets `ctx.cancelled`, but Python can't stop the thread itself, so handlers are idempotent and cooperative. On shutdown the worker drains for up to `WORKER_SHUTDOWN_TIMEOUT_SECONDS`, then puts unfinished jobs back on the queue without counting the attempt.

**Configuration.** Infrastructure and secrets come from environment variables (pydantic-settings, documented in `.env.example`). User preferences edited in the dashboard (criteria, templates, toggles) live in the DB. Env vars always win for anything defined in both places.

**Safety.** Scraped HTML is never rendered raw: it is sanitized with nh3 or reduced to text. Pages send a strict self-only CSP. htmx is vendored and configured not to inject inline styles or `eval` code, so the CSP needs no `unsafe-*` exceptions. Logs redact secrets. The container runs as a non-root user. The dashboard is protected by a single token (signed HttpOnly SameSite cookie or `Authorization: Bearer`, constant-time comparison, CSRF on forms, rate-limited login).

## Conventions

- `uv` for dependencies, with `uv.lock` committed. `ruff` for lint and format, `mypy --strict`, `pytest` with coverage. All four must pass before merge.
- Tests make no live network calls. Adapters are tested against recorded fixtures.
- Every schema change ships with an Alembic migration and a migration test.
- Every new setting is documented in `.env.example`.
- Optional integrations (Sentry, LLM extractor, reverse image search, IMAP, Prometheus, Playwright) do nothing when unconfigured.
- Work is tracked one issue per PR: branch `issue-<n>-<slug>`, PR body says `Closes #<n>`.
