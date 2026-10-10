# Apartments.com fixtures

JSON returned by the Apify actor `epctex/apartments-scraper-api`, which the
`apartments_com` adapter runs (see `docs/adapters.md`).

- `dataset_items.json`: dataset items written from the actor's published README and
  output example, **not captured from a live run**. A building with a rent range, a
  bed range and a public phone ("Furnished Units Available", professionally
  managed), a private-owner studio with a contact name only, an item that never says
  "furnished" (skipped), and an item with no `id` (skipped). Every place, name,
  phone number and id is a placeholder; the reviews and contact blocks are there to
  check that they are dropped from `raw`.
- `dataset_empty.json`: a search with no results.
- `apify_run_*.json`: the run object from `POST /v2/acts/.../runs` and
  `GET /v2/actor-runs/{id}`.

There is no `dataset_live.json` yet: the author had no Apify token. Run the actor once
with `max_results` 10 and capture its dataset with `scripts/capture_fixture.py
apartments_com <dataset items URL> --name dataset_live.json` (see CONTRIBUTING.md,
"Refreshing fixtures"), then trust it over the README where they differ.
