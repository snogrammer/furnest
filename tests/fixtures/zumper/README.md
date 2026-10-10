# Zumper fixtures

JSON returned by the Apify actor `memo23/zumper-cheerio`, which the `zumper` adapter
runs (see `docs/adapters.md`).

- `dataset_live.json`: four rows trimmed from live Denver runs on 2026-10-10 (#196),
  three from the furnished search (`/apartments-for-rent/denver-co/furnished`) and
  one from the plain search. Together they cost $0.07 for 30 rows. In order:
  1. a building from a furnished-stays operator: two units whose titles say
     furnished, one without the "Furnished" tag;
  2. a building with three units, two tagged "Furnished" and one not (dropped);
  3. an unfurnished building (no listings);
  4. a single `Listing` (a room in a shared house, `property_type` 13) with its own
     `listing_location` and `listing_agents`.

  The raw dataset went through `scripts/capture_fixture.py`, but that left house
  numbers, URL slugs, ZIP codes and partner links, so the rows were rebuilt from a
  whitelist of the fields the adapter reads, with the shapes and value types as the
  actor returned them. Ids are counters (`8000000NN` buildings, `9000000NN` listings,
  `1000000NN` photos), every street is `123 Example St` in `00001` (in URL slugs too),
  building names, unit numbers, feed names, the first building's unit titles and the
  descriptions are placeholders, coordinates are rounded to the city, phones are
  `555-01NN`, and names are `Jane Example` / `Example …`. The agent and partner fields are there to check that
  they stay out of `raw`. Floorplan codes, amenity tags, `features` ids, prices,
  sizes, dates and lease minimums are as returned.
- `dataset_empty.json`: a search with no results.
- `apify_run_*.json`: the run object from `POST /v2/acts/.../runs` and
  `GET /v2/actor-runs/{id}`.

The README-derived `dataset_items.json` from #185 was removed: the live rows showed
its shape was wrong (rows are mostly buildings with units in `floorplan_listings`,
`currency_code` is a number, availability is `date_available` as `YYYY/MM/DD`, and the
lease minimum is `min_lease_days`). To refresh, record a dataset with
`scripts/capture_fixture.py zumper "<dataset items URL>"` and rebuild the rows the same way.
