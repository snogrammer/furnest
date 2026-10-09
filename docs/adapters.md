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
| `search_locations` | Per source only. Place names, for sources that search by city instead of by the criteria's areas. For Craigslist, site names (`sfbay`). |
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
| (nothing: the actor has no landlord data) | `owner_type`, from the title and description alone (`furnest.domain.owner_type`) |

Every listing is `furnished`. `contact` is `platform_only`, because Furnished Finder
keeps landlord contact details behind its own messaging.

### Cost

At the prices published in October 2026 (free tier), a run costs about $0.08 to
start plus about $0.0025 per listing with details. Fifty listings every six hours
comes to about $0.85 a day, while 100 listings hourly would be about $8 a day.

### Limitations

The fixtures in `tests/fixtures/furnished_finder/` were written from the actor's
published schema and example, not captured from a live run. A live capture on
2026-10-07 (#111) failed three times in a row with "No resolvable locations or URLs
were provided": the actor's log showed its geocoder rejecting requests ("Switched to
the backup connection pool after repeated rejections", then "No coordinates for
location"), for both `"Austin, TX"` and the slug `us--tx--austin`. That is the actor's
side, not the input, and **each failed run was still billed the $0.08 start**. Try
again later, check the first successful run against the mapping above, and replace
the fixtures with its dataset using `scripts/capture_fixture.py`
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
| `buildingName`, else `title` (live building items name the building here), else `address.streetAddress`, else "N bd in City" | `title` |
| `_details.description` | `description` |
| `price.value` (buildings: `unitsGroup[].minPrice`) | `price`, monthly USD |
| `address.streetAddress`, `.city`, `.state`, `.zipcode` | `address.line1`, `city`, `region`, `postal_code`; country `US` |
| `location.latitude`, `.longitude` | `lat`, `lng` (both or neither) |
| `bedrooms`, `bathrooms` | `beds` (0 is a studio), `baths` (nearest half) |
| `livingArea` | `sqft` (0 means unknown) |
| `isRoomForRent` on the item, its `rental` block or the unit group, else `propertyType` (`singleFamily`, `condo`…) or `homeType` | `property_type`; a 0-bed apartment is a studio |
| `_details.resoFacts` lists (`appliances`, `cooling`, `heating`, `laundryFeatures`, `parkingFeatures`, `interiorFeatures`, `communityFeatures`, `exteriorFeatures`, `otherFacts`) | `amenities`, lower-cased. A laundry feature that doesn't say laundry gets the word added ("In Unit" → "in unit laundry"). |
| `availabilityDate` (in `_details`, `resoFacts` or the item) | `available_from`; "Now" is today. Live runs send `resoFacts.availabilityDate` as epoch milliseconds (midnight UTC). |
| `_details.resoFacts.leaseTerm` | `min_lease_months` when it names months ("6 months") |
| `media.allPropertyPhotos.highResolution`, else `media.propertyPhotoLinks.highResolutionLink` | `images` |
| everything except `propertyDisplayRules`, `attributionInfo`, `listingAgent`, `contact`; of `_details` only `description` and `resoFacts` | `raw`. The dropped blocks name agents and brokers. The rest of `_details` (price history, nearby homes, schools) is bulky and not about the rental. |
| `_type` `propertyGroup`, `rental.isBuildToRent` or a `rental.marketingTreatments` entry starting `multifamily`/`paidMultifamily` → property manager; a broker, MLS or agent name present in `propertyDisplayRules`, `attributionInfo` or `listingAgent` → agent; a `singleFamilyPaid`/`feedConnect*` treatment → property manager; else the title and description | `owner_type`. Only the type is kept: the attribution blocks are checked for presence and still stay out of `raw`. `groupType` isn't used, because every item says `apartmentComplex`. |

Every listing is `furnished`, because the search asked for it. `contact` is
`platform_only`, because Zillow routes renters through its own contact form.

### Cost

At the prices published in October 2026 (free tier), a run costs $0.0005 to start
(per GB of memory) plus about $0.003 per listing: $0.0009 for the result and $0.002
for its detail fetch. Fifty listings every six hours comes to about $0.60 a day,
while 100 listings hourly would be about $7 a day. A live run of 10 listings on
2026-10-07 cost $0.03.

### Limitations

`tests/fixtures/zillow/dataset_live.json` is a sanitized, trimmed capture of a live
run (2026-10-07, #111). It showed three gaps in the README-based mapping, now fixed:
building names are in `title`, not `buildingName`; availability dates are epoch
milliseconds; and `isRoomForRent` sits under `rental` and on each unit group. Live
building items carry no baths or square footage per unit group, so those stay
unknown. `dataset_items.json` is still written from the README, for the edge cases.
Zillow's furnished filter relies
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
| `price.breakDown.totalBeforeTaxes.price`, else `price.price` (else `price.amount`) per `price.qualifier` | `price`, USD (see below) |
| `locationSubtitle` ("City, State, United States"), else the place in `subDescription.title` | `address.city`, `region`; outside the US only the city |
| `coordinates.latitude`, `.longitude` | `lat`, `lng` (both or neither) |
| `subDescription.items` ("Studio", "2 bedrooms", "1.5 shared baths", "Half-bath") | `beds` (0 is a studio), `baths` (nearest half) |
| `roomType` ("Private room", "Shared room", "Hotel room"), else the kind in `subDescription.title` ("Entire condo", "rental unit" → apartment) | `property_type`; a 0-bedroom apartment, condo or unknown is a studio |
| `amenities[].values[]` with `available: true`; the "Not included" group as `no <name>` | `amenities`, lower-cased. "no air conditioning" makes the keyword extractor mark AC absent. |
| the window's check-in | `available_from` |
| `images[].imageUrl`, else `thumbnail` | `images` |
| everything except `host`, `coHosts`, `androidLink`, `iosLink` | `raw`. The dropped blocks name people. |
| `host.ratingCount` ≥ 1000 or two or more `coHosts` → property manager; any other `host` → individual; no `host` → the title and description | `owner_type`. Only the type is kept; the host block still stays out of `raw`. The Superhost badge isn't used: it says nothing about whether the host is a person. |

**Price.** When the item has the stay's total before taxes, the listing's price is
that total spread over a month, `total x (365/12) / nights`, quoted monthly. The
monthly discount, cleaning fee and service fee are all in it, so it's what a renter
would pay. Without a total, the quoted rate is kept with its period ("$95 night" is
nightly, "$2,100 month" monthly; no qualifier means nightly) and `Price.monthly`
converts it with the usual factors. Taxes are never included.

Live month-long searches (2026-10-07) came back without `totalBeforeTaxes`. They
quote `price.price` with `qualifier: "monthly"` ("$4,336 monthly, originally
$5,555"), which is Airbnb's own monthly price for the stay after its discounts, so
that is what the listing gets. `breakDown.total` is left alone because it isn't
consistent: sometimes it is the undiscounted price and sometimes the discounted
price plus fees.

Every listing is `furnished` (Airbnb stays are), and `contact` is `platform_only`,
because hosts are reached through Airbnb's messages.

### Cost

At the prices published in October 2026 (free tier), a run costs $0.0001 to start
plus $0.004 per listing ($0.0005 to $0.002 on paid Apify plans). Fifty listings every
six hours comes to about $0.80 a day, while 100 listings hourly would be about $10 a
day. A live run of 10 listings on 2026-10-07 cost $0.04.

### Limitations

`tests/fixtures/airbnb/dataset_live.json` is a sanitized, trimmed capture of a live
run (2026-10-07, #111). It showed that prices come in `price.price` (not `amount`)
and that only `locationSubtitle` names the country, and both are now mapped. The
`images` shape matched. `dataset_items.json` is still written from the README, for
the edge cases. Airbnb gives no street address
or square footage, and coordinates are approximate until booking, so location
matching leans on the city and the area check. The actor has no entire-place vs room
filter, so room-only or no-room criteria are applied by matching. A search returns at
most about 240 results per place.

## Craigslist (`craigslist`)

**Access:** direct HTTP through `SourceHttpClient`, so robots.txt, the rate limit,
retries and block detection all apply. No API key, no cost. Off by default.
`search_locations` holds Craigslist **site names**, the subdomain of each region's
site (`sfbay` for `sfbay.craigslist.org`), not place names.

```env
SOURCES_ADAPTERS={"craigslist": {"enabled": true, "search_locations": ["sfbay"], "interval_minutes": 120, "max_results": 50}}
```

### Access options considered (October 2026)

| Option | Finding |
| --- | --- |
| Official API or feed | No API. The per-search RSS feeds (`?format=rss`) answer 403 "Your request has been blocked". |
| Direct HTTP scraping | Search (`/search/area/<site>?cat=apa&is_furnished=1`) and posting (`/view/d/<slug>/<token>`) pages answer plain HTTP with a 200 and no bot check. `robots.txt` allows both and disallows only `/reply`, `/fb/`, `/flag`, `/suggest`, `/mf`, `/mailflag` and `/eaf`. The results are in the page twice: a static list (URL, title, price, neighbourhood) and a JSON-LD ItemList (coordinates, beds, baths, city). **Chosen.** |
| Reply relay | The poster's relay email is only shown by `/reply/...`, which robots.txt disallows and an hCaptcha guards. Not fetched. |
| Third-party data provider | Apify actors exist, but nothing here is blocked, so a paid provider isn't needed. |

Craigslist's terms of use forbid automated access in general, even though its
robots.txt allows these pages. The adapter stays polite: off by default, newest-first
searches only, the global `SOURCES_REQUESTS_PER_MINUTE` (6) unless overridden, each
posting fetched once per worker process, and a stop at the first block.

### How a run works

1. Criteria pick the **categories**: apartments/housing (`apa`) and
   sublets/temporary (`sub`), plus rooms/shared (`roo`) unless rooms are ruled out
   (only `roo` when only rooms are allowed).
2. Each site is searched in each category, newest first, with Craigslist's furnished
   filter, the monthly price range in USD (whole dollars, rounded outwards), and
   minimum beds and baths (baths rounded down; not sent for rooms). One request per
   site and category.
3. Rows are taken from the pages in turn, so a busy category doesn't crowd out the
   others. Repeats are dropped, up to `max_results`.
4. Each row is completed from its posting page. Postings are cached in the worker
   process (up to 5,000 for 7 days), so a run fetches only postings it hasn't seen.
   Price, title and coordinates come from the search row, fresh every run.
5. Postings fetched per run are capped at what fits in 80% of
   `run_timeout_seconds` at `requests_per_minute`, less the search requests: about
   70 at the defaults. Rows past the cap are returned from the search row alone and
   completed on a later run.

### Degrading

- No `search_locations`, or one that isn't a site name: each run fails with a
  message saying so.
- robots.txt disallowing a search, a 403, or a captcha or "blocked" page: the run
  fails as blocked and nothing is marked missing.
- A search page without the results list (a new layout, say): the run fails rather
  than reading as "no listings", which would mark everything missing.
- A posting that answers 404 or 410, or says it was deleted, flagged or expired: the
  row is dropped.
- A posting that fails any other way: the row is kept from the search page alone,
  logged as `craigslist.detail_failed` or `craigslist.detail_unreadable`, and retried
  next run.

### Field mapping

| Page | `NormalizedListing` |
| --- | --- |
| last part of the post URL (`/view/d/<slug>/<token>`) | `source_id` |
| post URL | `url` |
| row title | `title` |
| `#postingbody` text, without the print-only QR block | `description` |
| row price, with the `rent period` chip (daily, weekly, monthly; none is monthly) | `price`, USD |
| posting JSON-LD `address`, else the search JSON-LD | `address` (street, city, state, ZIP) |
| posting JSON-LD `latitude`/`longitude`, else the search JSON-LD | `lat`, `lng` (both or neither) |
| `2BR / 1Ba` chip, else JSON-LD `numberOfBedrooms`/`numberOfBathroomsTotal` | `beds`, `baths` |
| `750ft2` chip | `sqft` |
| `private room`/`shared room` chip or the `roo` category; else the housing chip (`apartment`, `house`, `townhouse`, `condo`, `cottage/cabin`, `in-law`, ...); else the JSON-LD `@type` | `property_type`; a 0-bedroom apartment is a studio |
| the other chips (`w/d in unit`, `laundry in bldg`, `cats are OK - purrr`, `no smoking`, `carport`, ...) | `amenities`, lower-cased; "laundry in bldg" and "w/d hookups" are reworded so the keyword extractor reads them right |
| `available now` / `available oct 15` chip | `available_from`. "Now" is the posting date (so the listing doesn't change every run). A month and day take the posting's year, or the next year if that would be over 60 days before the posting. |
| gallery photos (`_600x450.jpg`, up to 20) | `images` |
| an email address written in the description | `contact.email`; otherwise `contact.platform_only` |
| category, price as shown, neighbourhood, both JSON-LD blocks, the chips, post id, posting date | `raw` |
| a `broker fee` chip → agent; an `application fee` chip → property manager; else the title and body (`furnest.domain.owner_type`) | `owner_type` |

Every listing is `furnished`, since the search asks for it.

### Limitations

- No relay email, so most listings go down the manual-contact path ("Contact on
  Craigslist").
- A search page holds about 350 rows and the adapter reads only the first page of
  each, newest first.
- Location isn't sent: Craigslist searches a whole site, and matching applies the
  criteria's areas, ZIPs and neighbourhoods.
- Craigslist filters on the price as posted, whatever the rent period, so a weekly
  price below the minimum can come through. Matching compares monthly amounts.
- The posting cache lives in the worker process. After a restart every posting is
  fetched again, so the first run may complete only part of them.
- The fixtures (`tests/fixtures/craigslist/`) are sanitized live captures from
  2026-10-07. A layout change shows up as a failed run, not as an empty one.

## Landing (`landing`)

**Access:** Landing's official public API through `SourceHttpClient`, so robots.txt,
the rate limit, retries and block detection apply. No API key, no cost. Off by
default. `search_locations` holds Landing **market or display-city slugs** (`austin`,
`round-rock-tx`), as `GET /api/public/markets` lists them; each is checked against
that list at the start of a run.

```env
SOURCES_ADAPTERS={"landing": {"enabled": true, "search_locations": ["austin"], "interval_minutes": 360, "max_results": 50}}
```

### Access options considered (October 2026)

| Option | Finding |
| --- | --- |
| Official API | A read-only REST API with no auth under `/api/public` (OpenAPI 3.1 at `/api/public/openapi.json`) and an MCP server. robots.txt allows `/api/public` and `llms.txt` invites its use, asking callers to rate-limit bulk requests. **Chosen.** |
| HTML pages | robots.txt disallows the search pages (`/apartments/search*`, `/cities/`, `*start_date*`) and the terms forbid spiders. Not fetched. |

### How a run works

1. `GET /api/public/markets` once; configured slugs not in it fail the run.
2. `GET /api/public/search?market=<slug>&check_in=&check_out=&bedrooms=&max_price=`
   per slug. The stay runs from the earliest move-in (today if none, at most a year
   out) for the minimum lease, or a month, so every result is free for all of it.
   `bedrooms` is the criteria's minimum; `max_price` its top price in USD (whole
   dollars, rounded up), which Landing compares with the all-in price for the stay.
3. Rows are taken from the searches in turn, repeats dropped, up to `max_results`.
4. Each row is completed from `homes/{slug}` and `homes/{slug}/quote` for the stay.
   They are cached in the worker process per home and stay (up to 5,000 for a day),
   so a run fetches only what it hasn't seen.
5. Homes completed per run are capped at what fits in 80% of `run_timeout_seconds` at
   `requests_per_minute`, two requests a home. The rest come from the search row alone.

### Degrading

- No `search_locations`, a malformed slug, or one Landing doesn't list: the run fails
  with a message saying so.
- A response that isn't the expected shape: the run fails rather than reading as "no
  listings", which would mark everything missing. A 403 or robots.txt block fails it as
  blocked.
- A home that answers 404 or 410, or whose quote says `available: false`: dropped.
- A home page or quote that fails any other way: the row is kept from the search row
  and retried next run (`landing.detail_failed`, `landing.detail_unreadable`).

### Field mapping

| API field | `NormalizedListing` |
| --- | --- |
| `slug` | `source_id` |
| `home_url` (dates pre-filled) | `url` |
| `name` | `title` |
| quote `display_price_with_discount`, else the row's (monthly unless `is_price_nightly`; all fees but taxes) | `price`, USD |
| `address` ("street, city, ST ZIP"), `city`, `state` | `address` |
| `latitude`, `longitude` | `lat`, `lng` |
| `bedrooms` (0 is a studio), `bathrooms`, `square_feet` | `beds`, `baths`, `sqft` |
| `available_from`, no earlier than the check-in | `available_from` |
| `minimum_nightly_stay` of 28 or more | `min_lease_months` (1) |
| home `property.amenities`, `has_parking`, `laundry_type`, `pet_policy` | `amenities`, lower-cased |
| home `photos`, else quote `media`, else the row's `media`, else `hero_image_url` (up to 20) | `images` |
| the search row, home and quote | `raw` |

Every listing is `furnished`, `owner_type` property manager and `contact.platform_only`:
reservations are made on Landing.

### Limitations

- Search has no paging: only the first 25 homes of each slug come back. Name display
  cities to go deeper.
- The API has no description text, so keyword matching sees the amenities only.
- The cache lives in the worker process; after a restart homes are fetched again.
- The fixtures (`tests/fixtures/landing/`) are trimmed live captures from 2026-10-09.

## Blueground (`blueground`)

**Access:** direct HTTP through `SourceHttpClient`, so robots.txt, the rate limit,
retries and block detection all apply. No API key, no cost. Off by default.
`search_locations` holds Blueground **city slugs**, the middle of the city page's
address (`chicago` for `/furnished-apartments-chicago-usa`; `new-york`).

```env
SOURCES_ADAPTERS={"blueground": {"enabled": true, "search_locations": ["chicago"], "interval_minutes": 360, "max_results": 50}}
```

### Access options considered (October 2026)

| Option | Finding |
| --- | --- |
| Official API or feed | None is published. |
| Direct HTTP scraping | City pages answer plain HTTP with a 200 and no bot check. `robots.txt` allows them and disallows only `/book`, `/users`, `/payments` and similar account and checkout paths. Each page holds a JSON-LD list of `Apartment` items (beds, baths, floor size, coordinates, one photo, street address) and a `window.__INITIAL_STATE__` with the full rows. **Chosen.** |
| Third-party data provider | Nothing is blocked, so a paid provider isn't needed. |

Blueground's terms forbid "any automated program ... to scan, copy, index" its
content, even though robots.txt allows these pages. The adapter stays polite: off by
default, one request per page of 18 homes, the global `SOURCES_REQUESTS_PER_MINUTE`
(6) unless overridden, and a stop at the first block. Turning it on is your call.

### How a run works

1. The stay is the minimum lease (or a month, never under Blueground's 30 nights)
   from the earliest move-in or today.
2. Each city page is requested with `?checkIn=&checkOut=`. Without dates every price
   is 0; with them the page lists only homes free for the stay, each with its monthly
   rent for those dates. Later pages add `&page=<n>`.
3. Pages are taken from the cities in turn (every city's first page, then every
   second page, and so on) until `max_results` homes are in, a city has no more, or
   the request budget is spent: what fits in 80% of `run_timeout_seconds` at
   `requests_per_minute`, though every city gets its first page.
4. Rows come from the state; JSON-LD fills in beds, baths, floor size, coordinates
   and the photo when a row lacks them.

### Degrading

- No `search_locations`, or one that isn't a slug: each run fails with a message
  saying so.
- An unknown city (404), robots.txt disallowing the page, or a 403 or block page: the
  run fails and nothing is marked missing.
- A page without `__INITIAL_STATE__` or its home list (a new layout, say): the run
  fails rather than reading as "no listings". A page whose list is present but empty
  is "no listings".
- A row without a usable path or name is skipped and logged as
  `blueground.row_skipped`.

### Field mapping

| Page | `NormalizedListing` |
| --- | --- |
| last part of `path` (`p/furnished-apartments/chi-819`) | `source_id` (`chi-819`); `url` is the site plus `path` |
| `name` | `title` |
| `rent.amount` for the searched stay (0 means none) | `price`, monthly USD; none rather than 0 |
| `name` without the `building` prefix, `address.city`, the state from the city's ancestry, `address.level2` | `address` (street, city, state, neighbourhood; no ZIP) |
| `address.lat`/`lng`, else JSON-LD `latitude`/`longitude` | `lat`, `lng` |
| `bedrooms` (0 is a studio), `bathrooms`, `lotSize` (sq ft); else the JSON-LD equivalents | `beds`, `baths`, `sqft`, `property_type` |
| `availableFrom` | `available_from` |
| `utilitiesIncludedInRent` and photo captions (in-unit laundry, gym, pool, balcony, ...) | `amenities` |
| `photos` (the home's, then the building's, up to 20), else the JSON-LD photo | `images` |

Every listing is `furnished`, a `property_manager`'s, with `min_lease_months` 1 and a
`platform_only` contact: booking happens on Blueground.

### Known limits

- Bedrooms, price and baths aren't sent: the page's filter parameters aren't
  documented, and matching applies the criteria anyway. A city with hundreds of homes
  may fill `max_results` before reaching ones that match.
- The rent is the monthly figure for the searched stay (it differs from the
  `baseRent` list price), so it changes with the window.
- Listings carry no description beyond a generated one, and no ZIP.
- The fixtures (`tests/fixtures/blueground/`) are sanitized live captures from
  2026-10-09. A layout change shows up as a failed run, not as an empty one.

## Data providers and self-hosted alternatives (#111)

Furnished Finder, Zillow and Airbnb all go through Apify, which costs money, so #111
looked for free self-hosted tools to use instead.

**The constraint.** These three sources are behind a provider for a reason: Furnished
Finder shows a Cloudflare Turnstile check, Zillow a PerimeterX "press & hold" check,
and Airbnb's robots.txt disallows its search. furnest doesn't get past bot checks or
ignore robots.txt itself (decided on #24). A self-hosted scraper runs on our own
machine and fetches the sites from it, so it hits the same blocks. What the Apify
actors charge for is mostly the access itself (their proxies and anti-bot handling),
and self-hosting doesn't replace that.

| Tool | What it is | License | Hosting and API | For FF / Zillow / Airbnb |
| --- | --- | --- | --- | --- |
| [Maxun](https://github.com/getmaxun/maxun) | No-code web data platform. You record a "robot" in a browser, or describe one in plain language, and it runs on a schedule. | AGPL-3.0 | Docker Compose (several services). Has a REST API to run robots and read their data, plus an SDK, CLI and MCP server. | No. It fetches from our host. No anti-bot handling beyond a real browser; proxies are bring-your-own. |
| [Crawlee](https://crawlee.dev) | Apify's open-source crawling library (Node and Python), which most Apify actors are built on. | Apache-2.0 | A library: we would write and run the crawler. No hosted API. | No, same reason. Running a Store actor's code locally still fetches from our host, without Apify Proxy. |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | Playwright crawler that turns pages into LLM-ready markdown or structured data. | Apache-2.0 | A library, or a Docker server with a REST API. | No, same reason. |
| [Firecrawl](https://github.com/firecrawl/firecrawl) (self-hosted) | Scrape and crawl API that returns markdown or JSON. | AGPL-3.0 | Docker Compose with a REST API. The hosted service's anti-bot layer isn't part of the self-hosted build. | No, same reason. |
| [Scrapy](https://scrapy.org) + scrapy-playwright | The standard Python scraping framework. | BSD-3 | A library. | No, same reason. furnest's own `sources/http.py` and `sources/browser.py` already cover this ground. |

**Outcome.** Apify stays the backend for these three sources. Its free plan includes
$5 of usage a month (it doesn't roll over), and the next section shows how to stay
inside that. The search left something useful behind: provider-backed adapters no
longer depend on Apify directly.

**The provider seam.** `sources/provider.py` defines `DataProvider`, a backend with a
`name` and `run(http, job, run_input, *, token, timeout_seconds, clock) -> items`. It
also defines `ProviderAdapter`, the adapter half that used to be `ApifyAdapter`: it
checks the `api_key` and `search_locations`, keeps the provider's timeout 60s inside
the run's, turns off robots.txt for the API call, and hands the items and the input
to the subclass. `ApifyProvider` (`sources/apify.py`) is the only backend today, and
`ApifyAdapter` is just `ProviderAdapter` on Apify. A self-hosted backend would be
another `DataProvider`, for example a `MaxunProvider` whose `run` starts a robot
through Maxun's API and reads its rows. It would also need a base-URL setting, added
when the first one lands. A source that can be fetched politely and uses it subclasses
`ProviderAdapter` with its own `build_input` and `parse_items`, because each job has
its own input and output shape. For such sources, compare it against a plain HTTP or
browser adapter, which needs no extra service. A provider earns its keep when it
saves writing a parser.

### Staying inside Apify's free credit

Approximate cost of one run of `max_results` 20 at October 2026 free-tier prices,
checked against the live runs on 2026-10-07:

| Source | One run | Daily for a month | Every 2 days for a month |
| --- | --- | --- | --- |
| Zillow | $0.06 ($0.0005 start, $0.0029 a listing) | $1.80 | $0.90 |
| Airbnb | $0.08 ($0.004 a listing) | $2.40 | $1.20 |
| Furnished Finder | $0.13 ($0.08 start, $0.0025 a listing) | $3.90 | $1.95 |

All three daily comes to about $8 a month, over the $5 credit. Two sources daily fit,
and so do all three at `"interval_minutes": 2880` (about $4). Note that Furnished
Finder bills its start fee even when the run fails. The account's usage is at
`https://console.apify.com/billing`.

## Candidates

Sources that are built, researched, or ruled out. The research for #48 was done on
2026-10-07 with plain HTTP and a polite user agent, the same way as for the built
adapters, and furnest doesn't get past bot checks (see Furnished Finder above).

The researched sources are registered as **stubs** in
`sources/adapters/stubs.py`. A stub has a `sources` row and a row on `/sources`, but
it is always disabled with the reason "not built yet". Pinning it on in
`SOURCES_ADAPTERS` or giving it an `api_key` doesn't change that (`implemented =
False`, see `sources/stub.py`). Its `capabilities` describe the planned route, and
its docstring holds the TODO. To build one, delete its class from `stubs.py`, add
`sources/adapters/<name>/`, and move its row here to Built with a section above.

| Source | Status | Access method | API | robots.txt / terms | Difficulty |
| --- | --- | --- | --- | --- | --- |
| Furnished Finder | Built (above) | Apify data provider | None | Cloudflare Turnstile on every search page | Done |
| Zillow (furnished rentals) | Built (above) | Apify data provider | Partner only (MLS, valuations) | PerimeterX "press & hold" check | Done |
| Airbnb (monthly stays) | Built (above) | Apify data provider | None | robots.txt disallows search; terms forbid scraping | Done |
| Craigslist (housing, furnished) | Built (above) | Direct HTTP | None (RSS blocked) | robots.txt allows `/search` and `/view`; terms forbid automated access | Done |
| Landing (`landing`) | Built (above) | Its official API, through `SourceHttpClient` | **Yes:** a public read-only REST API with no auth | robots.txt allows `/api/public` and disallows the HTML search pages; the terms forbid spiders on the site, so the API only | Done |
| Blueground (`blueground`) | Built (above) | Direct HTTP, through `SourceHttpClient` | None public | City pages (`/furnished-apartments-<city>-usa`) answer 200 with no bot check, and robots.txt allows them (it disallows `/book`, `/users`, `/payments` and similar). The terms forbid automated scanning, so it stays off by default with a caveat, like Craigslist | Done |
| Zumper (`zumper`) | Stub | Data provider (Apify, e.g. `memo23/zumper-cheerio`, `benthepythondev/zumper-rental-scraper`) | Partner listing feeds only (listings in, not search) | Every page, including robots.txt and the terms, is behind a JavaScript "Client Challenge" | Medium: compare the actors' furnished filters and output |
| Apartments.com (`apartments_com`) | Stub | Data provider (Apify, e.g. `epctex/apartments-scraper`, about 1,000 users) | None | Akamai answers 403 to every request, robots.txt and the terms page included. CoStar is known to enforce its terms against scrapers | Medium: check that the actor's furnished filter maps onto criteria |
| HotPads (`hotpads`) | Stub | Data provider (Apify, e.g. `benthepythondev/hotpads-rental-scraper`) | None | 403 with the same PerimeterX check as Zillow. robots.txt allows search pages, disallows `/api/` and `?lat=`/`?lon=` searches, and defers to Zillow's terms | Medium. Much of its inventory is Zillow's, so measure what it adds after dedup first |
| Kopa | Shut down (2026), not stubbed | — | — | The site is a farewell notice and robots.txt is `Disallow: /` | — |
