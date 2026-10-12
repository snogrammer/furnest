# Furnished Finder fixtures

JSON returned by the Apify actor `memo23/furnishedfinder-scraper-cheerio`, which the
`furnished_finder` adapter runs (see `docs/adapters.md`).

- `dataset_items.json`: six rows. Five come from live Denver runs on 2026-10-12 (#216
  part 2 and #221), with `moreDetails` on. One run was the plain search, and the other
  added `?max-price=1500`. In order:
  1. an apartment whose landlord company reads as a management business (`petsAllowed`,
     a cleaning fee, `IN_UNIT` laundry, `state` spelled out as the actor sometimes does);
  2. a house available tomorrow (`isAvailableNow` false, `availableOn` and
     "Available: Oct. 13, 2026"), with `petsNotAllowed` and a one-word company;
  3. a house with a deposit, a pet deposit, 2.5 baths and a 90-day minimum stay;
  4. a house with `In Complex W/D` laundry, `washeranddryeronpremises`, no fees and
     no neighbourhood overview;
  5. a room (`propertyTypeClass` `room`, `Room - Condo`) whose landlord company is an
     employer, which isn't an owner-type hint;
  6. a copy of a live row with no `id` or `listingId`, which is skipped.

  The raw datasets went through `scripts/capture_fixture.py`, but its name redaction
  also replaced structural fields (`houseRulesGraphql[].name`, `landlordCompany`), so
  the rows were rebuilt from a whitelist of the fields the adapter reads, plus a few
  landlord fields kept so that a test can check they stay out of `raw`. Ids are
  counters (`100000NN`, in the URLs and photo paths too), titles, descriptions,
  overviews and company names are placeholders, coordinates are rounded to the city,
  the landlord is `Jane E` / `jane.example`, and the review is invented. Amenities,
  house rules, laundry types, prices, fees, sizes, dates and stay minimums are as
  returned.
- `dataset_empty.json`: a search with no results. The actor returns an empty dataset,
  with no error row.
- `apify_run_*.json`: the run object from `POST /v2/acts/.../runs` and
  `GET /v2/actor-runs/{id}` in each status the client handles. `tests/unit/test_sources_apify.py`
  uses these too.

The README-derived rows for `abotapi/furnishedfinder-scraper` (#24) were removed when
the adapter switched actors (#221). To refresh, record a dataset with
`scripts/capture_fixture.py furnished_finder "<dataset items URL>"` and rebuild the
rows the same way.
