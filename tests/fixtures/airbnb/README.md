# Airbnb fixtures

JSON returned by the Apify actor `tri_angle/airbnb-scraper`, which the `airbnb`
adapter runs (see `docs/adapters.md`).

- `dataset_items.json`: dataset items from a search over a 31-night stay. It covers
  an entire place with a full price breakdown and a "Not included" amenity group, a
  private room quoted per night with a shared bath, a studio condo outside the US
  quoted per month with odd values (no title, a foreign URL, half the coordinates),
  and an item with no `id` (skipped).
- `dataset_empty.json`: a search with no results.
- `apify_run_*.json`: the run object from `POST /v2/acts/.../runs` and
  `GET /v2/actor-runs/{id}`.

These were written from the actor's published README and output example, not
captured from a live run, so the `price.breakDown` of a monthly stay and the `images`
shape in particular are a best reading of the docs. Every place, coordinate, id and
name is a placeholder (`Exampleville`, `Jane Example`), and the host and co-host
blocks are there to check that they are dropped from `raw`. If a live run shows a
field mapped wrongly, replace the item with a sanitized copy of the real one and keep
the placeholders. `scripts/capture_fixture.py airbnb <dataset items URL>` records and
sanitizes a dataset (see CONTRIBUTING.md, "Refreshing fixtures").
