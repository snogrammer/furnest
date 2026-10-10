# Apartments.com fixtures

JSON returned by the Apify actor `epctex/apartments-scraper-api`, which the
`apartments_com` adapter runs (see `docs/adapters.md`).

- `dataset_live.json`: three buildings trimmed from a live Denver run on 2026-10-10
  (#194, start URL `/denver-co/`). In order:
  1. a building with units in `rentals`: a studio and a one-bedroom free "Now", and a
     three-bedroom free on a later date;
  2. a building with floor plans in `models` and no `rentals` (one listing at its
     rent range);
  3. an unfurnished building (skipped).

  **Two edits besides placeholders:** none of the ten live buildings in that run
  said "furnished", so building 1 has "Furnished" added to its Interior Amenities and
  building 2 has "Furnished Units Available" added under Property Information. These
  are the two signals the one furnished building seen live (in another run) carried.

  The raw dataset went through `scripts/capture_fixture.py`, and then the rows were
  rebuilt from a whitelist of fields, in the shapes and value types the actor
  returned. Ids are `apt000N`, unit keys `apt000NuN`, model ids `apt000NmN`, building
  names are `Example …`, every street is `123 Example St` in `00001`, coordinates are
  rounded to the city, phones are `(555) 010-01NN`, the leasing company is `Example
  Residential`, unit numbers, plan names, descriptions and specials are placeholders,
  and image URLs are `example-image-NN`. Amenity and fee lists are cut to their first
  few values. Neighborhoods, rent ranges, beds, baths, sizes, availability and lease
  terms are as returned. The reviews, schools and transportation blocks are
  placeholders added to check that they stay out of `raw`.
- `dataset_demo.json`: what the actor returns in demo mode (Apify's Free plan, after
  5 runs a month): placeholder items only.
- `dataset_empty.json`: a search with no results.
- `apify_run_*.json`: the run object from `POST /v2/acts/.../runs` and
  `GET /v2/actor-runs/{id}`.

The README-derived `dataset_items.json` from #184 was removed: the live rows showed
that rent is `baseRent`/`totalRent` (not `rent`), units are in `rentals` with their
own price, beds and `availableDate`, and there are no `photos` without paid extras.
To refresh, record a dataset with `scripts/capture_fixture.py apartments_com
"<dataset items URL>"` (with `api_key` set in `SOURCES_ADAPTERS`) and rebuild the rows
the same way.
