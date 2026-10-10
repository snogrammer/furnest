# Zumper fixtures

JSON returned by the Apify actor `memo23/zumper-cheerio`, which the `zumper` adapter
runs (see `docs/adapters.md`).

- `dataset_items.json`: dataset items written from the actor's published README
  (output sample and field list), covering both page variants (`entity.data` and
  `activeListings[0]`), a flat item, a furnished listing with a management company, an
  agent listing, a listing with unknown values, an unfurnished listing (skipped) and an
  item with no `listing_id` (skipped). Every place, id, name and phone is a placeholder
  (`Exampleville, XX`, `Jane Example`, `555-0100`). The agent, brokerage, phone and
  email fields are there to check that they are dropped from `raw`.
- `dataset_empty.json`: a search with no results.
- `apify_run_*.json`: the run object from `POST /v2/acts/.../runs` and
  `GET /v2/actor-runs/{id}`.

There is **no sanitized live capture yet**: the work was done without an Apify token.
Field names beyond those in the README sample (`available_on`, `lease_terms`, `phone`,
`url`) are best guesses, and Zumper's numeric amenity ids are not decoded. When a live
run is available, record it with `scripts/capture_fixture.py zumper <dataset items
URL>` as `dataset_live.json` and fix the mapping to match it.
