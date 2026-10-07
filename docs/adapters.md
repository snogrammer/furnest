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

## Zillow (`zillow`)

**Access:** the Apify data provider `igolaizola/zillow-scraper-ppe`, with Zillow's
"Furnished" filter on. Needs an Apify API token (the same one Furnished Finder uses
works). Off by default.

```env
SOURCES_ADAPTERS={"zillow": {"api_key": "your-apify-token", "search_locations": ["Springfield, IL"], "interval_minutes": 360, "max_results": 50}}
```

### Access options considered (October 2026)

| Option | Finding |
| --- | --- |
| Official API or feed | None for searching rentals. Zillow's partner APIs (Bridge) carry MLS and valuation data for approved partners, and its rental feeds only take listings in. `robots.txt` disallows `/api/` and `?searchQueryState=` search URLs. |
| Direct HTTP scraping | `/homes/for_rent/` and `/<city>/rentals/` answer plain HTTP with a 403 and a PerimeterX "Access to this page has been denied" captcha page. |
| Browser scraping | A browser gets the same PerimeterX "press & hold" challenge. Getting past it is bot-check bypassing, which furnest doesn't do (as for Furnished Finder). |
| Third-party data provider | Many Apify actors scrape Zillow. The provider handles access, and furnest makes an authenticated API call. **Chosen.** |

Among the Apify actors, `igolaizola/zillow-scraper-ppe` was picked because it
searches by place name (like `search_locations`) and takes Zillow's rent filters as
input, including **Furnished**, entire place vs room, beds, baths and move-in date.
It is widely used (about 190 users a month, 5 stars) and cheap. Others were ruled
out: the most used ones (`maxcopell/zillow-scraper`, `api-ninja/zillow-search-scraper`)
take only raw search URLs, which robots.txt disallows and which would mean encoding
Zillow's private query format. The dedicated rentals actor is rated 2.3 and also
URL-only, and the rest have few users. If this actor disappears, swapping in another
means changing `ACTOR` and the mapping in `sources/adapters/zillow/parse.py`.

### How a run works

1. Criteria become the actor's input: `operation: rent`, `search_locations`,
   `furnished: true`, `space` (`entirePlace` when rooms are denied or not allowed,
   `roomForRent` when only rooms are allowed), minimum beds (when above 0) and baths
   (rounded down), and `moveInDate` from the latest move-in date when it's in the
   future. Details are fetched. `maxItems` is `max_results`. **Price isn't sent:**
   the actor documents its rent price filter as "per week" while its own rent
   example uses monthly figures, and a wrong guess would silently drop matches, so
   matching applies the price range.
2. `ApifyAdapter` (`sources/apify.py`) runs the actor exactly as for Furnished Finder:
   long-polled, Apify's timeout 60s inside ours, token in a header, no robots.txt.
3. Each item is mapped (see below). An item that can't be mapped is logged and
   skipped.

### Degrading

- No `api_key`: the source is disabled with a reason, and never scheduled.
- No `search_locations`: each run fails with a message saying so.
- A bad token, a forbidden actor, or a run that ends `FAILED`, `TIMED-OUT` or
  `ABORTED` (which is how this actor reports a refused search): the run fails with the
  reason and nothing is marked missing.
- An empty dataset is a successful run with no listings. This actor has no "nothing
  was scanned" row, unlike Furnished Finder's.

### Field mapping

A rent search returns `property` items (one home) and `propertyGroup` items (an
apartment building, with `unitsGroup` summarising its units by bedroom count). A
building becomes one listing per bedroom group, `<zpid or lotId>-<beds>bd`, priced at
that group's lowest rent and sharing the building's address, coordinates, photos and
URL. Groups without a bedroom count are skipped.

| Actor field | `NormalizedListing` |
| --- | --- |
| `zpid` (buildings: `zpid` else `lotId`, plus `-<beds>bd`) | `source_id` |
| `url`, else `hdpUrl`, else `https://www.zillow.com/homedetails/<zpid>_zpid/` | `url` (a building without a URL is skipped) |
| `buildingName`, else `address.streetAddress`, else "N bd in City" | `title` |
| `_details.description` | `description` |
| `price.value` (buildings: `unitsGroup[].minPrice`) | `price`, monthly USD |
| `address.streetAddress`, `.city`, `.state`, `.zipcode` | `address.line1`, `city`, `region`, `postal_code`; country `US` |
| `location.latitude`, `.longitude` | `lat`, `lng` (both or neither) |
| `bedrooms`, `bathrooms` | `beds` (0 is a studio), `baths` (nearest half) |
| `livingArea` | `sqft` (0 means unknown) |
| `isRoomForRent`, else `propertyType` (`singleFamily`, `condo`…) or `homeType` | `property_type`; a 0-bed apartment is a studio |
| `_details.resoFacts` lists (`appliances`, `cooling`, `heating`, `laundryFeatures`, `parkingFeatures`, `interiorFeatures`, `communityFeatures`, `exteriorFeatures`, `otherFacts`) | `amenities`, lower-cased. A laundry feature that doesn't say laundry gets the word added ("In Unit" → "in unit laundry"). |
| `availabilityDate` (in `_details`, `resoFacts` or the item) | `available_from`; "Now" is today |
| `_details.resoFacts.leaseTerm` | `min_lease_months` when it names months ("6 months") |
| `media.allPropertyPhotos.highResolution`, else `media.propertyPhotoLinks.highResolutionLink` | `images` |
| everything except `propertyDisplayRules`, `attributionInfo`, `listingAgent`, `contact`; of `_details` only `description` and `resoFacts` | `raw`. The dropped blocks name agents and brokers. The rest of `_details` (price history, nearby homes, schools) is bulky and not about the rental. |

Every listing is `furnished`, because the search asked for it. `contact` is
`platform_only`, because Zillow routes renters through its own contact form.

### Cost

At the prices published in October 2026 (free tier), a run costs $0.0005 to start
(per GB of memory) plus about $0.003 per listing: $0.0009 for the result and $0.002
for its detail fetch. Fifty listings every six hours comes to about $0.60 a day,
while 100 listings hourly would be about $7 a day.

### Limitations

The fixtures in `tests/fixtures/zillow/` were written from the actor's README and
dataset view, not captured from a live run. The `_details` facts, availability date,
lease term and `unitsGroup` shapes are the least certain, so check the first live run
against the mapping above, and replace the fixtures with a captured dataset
(`scripts/capture_fixture.py zillow <dataset URL>`). Zillow's furnished filter relies
on what listers tick, so some furnished places are missed and the odd unfurnished one
gets through. Matching's keyword checks on the description still apply.

## Airbnb (`airbnb`)

**Access:** the Apify data provider `tri_angle/airbnb-scraper`, searching a stay of a
month or more (Airbnb's monthly-stay search). Needs an Apify API token (the same one
the other Apify sources use works). Off by default.

```env
SOURCES_ADAPTERS={"airbnb": {"api_key": "your-apify-token", "search_locations": ["Springfield, IL"], "interval_minutes": 360, "max_results": 50}}
```

### Access options considered (October 2026)

| Option | Finding |
| --- | --- |
| Official API or feed | None for searching stays. Airbnb's APIs are for approved software partners that manage hosts' listings. |
| Direct HTTP scraping | `/s/<place>/homes` answers plain HTTP with a 200 and the results as embedded JSON, but `robots.txt` disallows `/s/*/*` (every search page) and `/api/v3/...StaysQuery` for all crawlers, and Airbnb's terms forbid scraping. `SourceHttpClient` respects robots.txt, so this is out. |
| Browser scraping | The same pages, so the same robots.txt rules. |
| Third-party data provider | Many Apify actors scrape Airbnb. The provider handles access, and furnest makes an authenticated API call. **Chosen.** |

Among the Apify actors, `tri_angle/airbnb-scraper` was picked because it is by far
the most used (about 790 users a month, over 900,000 runs, rated 4.5) and searches by
place name (like `search_locations`) with check-in and check-out dates, bedrooms and
bathrooms as input. None of the actors looked at (`curious_coder/airbnb-scraper`,
`automation-lab/airbnb-listing`, `cirkit/airbnb-search-scraper`) has a "monthly
stays" switch, and all take dates the same way. If this actor disappears, swapping in
another means changing `ACTOR` and the mapping in `sources/adapters/airbnb/parse.py`.

### How a run works

1. Criteria become a **stay window**: check-in on the earliest move-in date (today if
   it's unset or past), check-out `lease_min_months` (default 1) calendar months
   later, clamped to the end of a short month. A month is never under 28 nights,
   which is what makes it a monthly-stay search: hosts who don't take stays that long
   drop out, and quotes carry the monthly discount. Everything returned is free for
   the whole window.
2. The input is `locationQueries` (`search_locations`), `checkIn`, `checkOut`, USD,
   `en-US`, `maxResults` (`max_results`), minimum bedrooms (when above 0) and
   bathrooms (rounded down), and `enrichUserProfiles: false` (host profiles cost a
   request each, bill at the higher "enriched" rate, and name people). **Price isn't
   sent:** the actor doesn't say whether its bounds are nightly or for the stay, so
   matching applies the price range.
3. `ApifyAdapter` (`sources/apify.py`) runs the actor as for the other Apify sources,
   and hands the items back with the input, so the mapping knows the window.
4. Each item is mapped (see below). An item that can't be mapped is logged and
   skipped.

### Degrading

- No `api_key`: the source is disabled with a reason, and never scheduled.
- No `search_locations`: each run fails with a message saying so.
- A bad token, a forbidden actor, or a run that ends `FAILED`, `TIMED-OUT` or
  `ABORTED`: the run fails with the reason and nothing is marked missing.
- An empty dataset is a successful run with no listings (nothing free for the stay).

### Field mapping

| Actor field | `NormalizedListing` |
| --- | --- |
| `id` | `source_id` |
| `url` (under `https://www.airbnb.com/`), else `https://www.airbnb.com/rooms/<id>` | `url` |
| `title` without the " - Apartments for Rent in … - Airbnb" suffix, else `subDescription.title` | `title` |
| `description` | `description` |
| `price.breakDown.totalBeforeTaxes.price`, else `price.amount` per `price.qualifier` | `price`, USD (see below) |
| `subDescription.title` ("Entire rental unit in City, State, United States") | `address.city`, `region`; outside the US only the city |
| `coordinates.latitude`, `.longitude` | `lat`, `lng` (both or neither) |
| `subDescription.items` ("Studio", "2 bedrooms", "1.5 shared baths", "Half-bath") | `beds` (0 is a studio), `baths` (nearest half) |
| `roomType` ("Private room", "Shared room", "Hotel room"), else the kind in `subDescription.title` ("Entire condo", "rental unit" → apartment) | `property_type`; a 0-bedroom apartment, condo or unknown is a studio |
| `amenities[].values[]` with `available: true`; the "Not included" group as `no <name>` | `amenities`, lower-cased. "no air conditioning" makes the keyword extractor mark AC absent. |
| the window's check-in | `available_from` |
| `images[].imageUrl`, else `thumbnail` | `images` |
| everything except `host`, `coHosts`, `androidLink`, `iosLink` | `raw`. The dropped blocks name people. |

**Price.** When the item has the stay's total before taxes, the listing's price is
that total spread over a month, `total x (365/12) / nights`, quoted monthly. The
monthly discount, cleaning fee and service fee are all in it, so it's what a renter
would pay. Without a total, the quoted rate is kept with its period ("$95 night" is
nightly, "$2,100 month" monthly; no qualifier means nightly) and `Price.monthly`
converts it with the usual factors. Taxes are never included.

Every listing is `furnished` (Airbnb stays are), and `contact` is `platform_only`,
because hosts are reached through Airbnb's messages.

### Cost

At the prices published in October 2026 (free tier), a run costs $0.0001 to start
plus $0.004 per listing ($0.0005 to $0.002 on paid Apify plans). Fifty listings every
six hours comes to about $0.80 a day, while 100 listings hourly would be about $10 a
day.

### Limitations

The fixtures in `tests/fixtures/airbnb/` were written from the actor's README and
output example, not captured from a live run. The price breakdown of a monthly stay
and the `images` shape are the least certain, so check the first live run against
the mapping above, and replace the fixtures with a captured dataset
(`scripts/capture_fixture.py airbnb <dataset URL>`). Airbnb gives no street address
or square footage, and coordinates are approximate until booking, so location
matching leans on the city and the area check. The actor has no entire-place vs room
filter, so room-only or no-room criteria are applied by matching. A search returns at
most about 240 results per place.

## Candidates

The sources below are planned or proposed and haven't been researched yet. Each
adapter's issue records its access options here when it's built, as above.

| Source | Status |
| --- | --- |
| Furnished Finder | Built (above): Apify data provider. |
| Zillow (furnished rentals) | Built (above): Apify data provider. |
| Airbnb (monthly stays) | Built (above): Apify data provider. |
| Craigslist (housing, furnished) | Planned (#47). |
| Blueground, Zumper, Apartments.com (furnished filter), HotPads, Kopa, Landing | Stub and document. |
