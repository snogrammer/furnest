# Source adapters

How furnest gets listings from each source, what was considered, and how to configure
it. The adapter contract, config resolution and the HTTP layer are covered in
[ARCHITECTURE.md](ARCHITECTURE.md#key-mechanics).

Every adapter is configured through its entry in `SOURCES_ADAPTERS` (JSON keyed by
adapter name), which falls back to the `SOURCES_*` defaults:

| Key | Meaning |
| --- | --- |
| `enabled` | Pins the source on or off, overriding the dashboard toggle. |
| `interval_minutes`, `jitter_seconds`, `run_timeout_seconds` | Scheduling (see [ARCHITECTURE.md](ARCHITECTURE.md)). |
| `requests_per_minute`, `max_retries`, `request_timeout_seconds`, `user_agent`, `proxy_url`, `respect_robots` | The HTTP layer. |
| `max_results` | The most listings one run asks for, for sources that page or bill per result. |
| `search_locations` | Per source only. Place names, for sources that search by city instead of by the criteria's areas. |
| `api_key` | Per source only. Adapters whose capabilities say `requires_api_key` stay disabled without one, and the dashboard says why. |

## Furnished Finder (`furnished_finder`)

**Access:** the Apify data provider `abotapi/furnishedfinder-scraper`. Needs an Apify
API token. Off by default.

```env
SOURCES_ADAPTERS={"furnished_finder": {"api_key": "your-apify-token", "search_locations": ["Springfield, IL"], "interval_minutes": 360, "max_results": 50}}
```

### Access options considered (October 2026)

| Option | Finding |
| --- | --- |
| Official API or feed | None is published. `robots.txt` disallows `/api/`, which is the site's private backend. |
| Direct HTTP scraping | Every search (`/housing/us--<state>--<city>`) and property (`/property/<id>_<unit>`) page answers plain HTTP with a Cloudflare 403. Only the homepage loads, and it holds no listing data. |
| Browser scraping | A real, interactive browser is shown Cloudflare's "verify you are human" check before any search page. A headless browser would fare no better, and furnest doesn't try to get past bot checks (no captcha solving, no challenge-bypass proxies such as FlareSolverr). |
| Third-party data provider | Several Apify actors scrape Furnished Finder and return JSON. The provider handles access, and furnest makes an authenticated API call. **Chosen.** |

Among the Apify actors, `abotapi/furnishedfinder-scraper` was picked because its
output is documented field by field. Each item has a stable id, URL, city/state,
coordinates, price, details and availability. Its input also takes the filters
criteria have (city, price range, minimum beds and baths, entire unit vs room), which
keeps billed runs small. Others were ruled out: one returns no listing id or URL,
one has a 1-star rating, and the rest have only a couple of users. If this actor
disappears, swapping in another means changing `ACTOR` and the mapping in
`sources/adapters/furnished_finder/parse.py`.

### How a run works

1. Criteria become the actor's input: `search_locations`, then the monthly price range
   in USD (whole dollars, rounded outwards), minimum beds and baths (baths rounded
   down), and `entire_unit` when rooms are denied or not allowed (`room` when only
   rooms are allowed). Details are fetched, reviews are not. `maxListings` is
   `max_results`.
2. `sources/apify.py` starts the run, long-polls it (one request a minute at most), and
   reads the dataset. Apify's own run timeout is `run_timeout_seconds` less 60s, so
   Apify stops the actor before the worker gives up on the run. A run still going at
   the deadline is aborted. The token is sent as a `Bearer` header and is redacted from
   logs and Sentry. robots.txt isn't consulted, because this is an authenticated API.
3. Each item is mapped to a `NormalizedListing` (see below). An item that can't be
   mapped is logged and skipped.

### Degrading

- No `api_key`: the source is disabled with a reason, and never scheduled.
- No `search_locations`: each run fails with a message saying so.
- A bad token (401), a forbidden actor (403, recorded as blocked), or a run that ends
  `FAILED`, `TIMED-OUT` or `ABORTED`: the run fails with the reason and nothing is
  marked missing.
- The actor's "nothing was scanned" row, `{"error": true}`: the run fails. The actor
  emits this both for a search with no matches and for one Furnished Finder refused,
  and treating a refusal as "no listings" would mark every stored listing missing.
  The cost is that a city with genuinely no matches fails each run. Remove it from
  `search_locations` if that happens.

### Field mapping

| Actor field | `NormalizedListing` |
| --- | --- |
| `id` (else `propertyId_unitId`) | `source_id` |
| `url` (else `https://www.furnishedfinder.com/property/<id>`) | `url` |
| `name` | `title` |
| `description`, `spaceDescription`, `neighborhoodDescription` | `description`, in that order |
| `priceValue` + `priceCurrency` (else `priceFormatted` parsed) | `price`, monthly |
| `city`, `state` | `address.city`, `address.region`; country `US` |
| `latitude`, `longitude` | `lat`, `lng` (both or neither) |
| `bedroomCount`, `bathroomCount` | `beds` (0 is a studio), `baths` (nearest half) |
| `squareFootage` | `sqft` (0 means unknown) |
| `propertyTypeClass` = `room`, else `propertyType` | `property_type` |
| `amenities`, `laundryType`, `utilitiesIncluded` | `amenities`, as words (`airConditioning` → "air conditioning") for the keyword extractor. `petsLiveOnThisProperty` is dropped, because it describes the landlord's pets. |
| `isAvailableNow`, `availableOnDate` | `available_from`: today if available now, else the parsed date |
| `minimumStayDays` | `min_lease_months`, rounded up from 30-day months |
| `photos` | `images` |
| everything except `reviews` | `raw`. Reviews carry reviewers' names. |

Every listing is `furnished`. `contact` is `platform_only`, because Furnished Finder
keeps landlord contact details behind its own messaging.

### Cost

At the prices published in October 2026 (free tier), a run costs about $0.08 to
start plus about $0.0025 per listing with details. Fifty listings every six hours
comes to about $0.85 a day, while 100 listings hourly would be about $8 a day.

### Limitations

The fixtures in `tests/fixtures/furnished_finder/` were written from the actor's
published schema and example, not captured from a live run. The first live run
should be checked against the mapping above. To replace them with a real run's
dataset, use `scripts/capture_fixture.py` with the dataset URL
([CONTRIBUTING.md](../CONTRIBUTING.md#refreshing-fixtures)).

## Candidates

The sources below are planned or proposed and haven't been researched yet. Each
adapter's issue records its access options here when it's built, as above.

| Source | Status |
| --- | --- |
| Furnished Finder | Built (above): Apify data provider. |
| Zillow (furnished rentals) | Planned (#45). |
| Airbnb (monthly stays) | Planned (#46). |
| Craigslist (housing, furnished) | Planned (#47). |
| Blueground, Zumper, Apartments.com (furnished filter), HotPads, Kopa, Landing | Stub and document. |
