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
  cli.py          furnest web | worker | migrate
  config.py       pydantic-settings; env → DB settings precedence           (#3)
  logging.py      structured JSON logs with secret redaction                (#5)
  observability.py Sentry init (no-op without SENTRY_DSN), release/env tags (#5)
  db/             engine (WAL, busy_timeout, FK), models/, migrations/      (#4)
  domain/         NormalizedListing, Criteria, money and rent periods       (P2)
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
