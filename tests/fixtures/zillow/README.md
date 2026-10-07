# Zillow fixtures

JSON returned by the Apify actor `igolaizola/zillow-scraper-ppe`, which the `zillow`
adapter runs (see `docs/adapters.md`).

- `dataset_items.json`: dataset items from a furnished rent search. It covers a house
  with details (`_details`), an apartment with no details and odd values, an
  apartment building (`propertyGroup`) with three unit groups, one of them without a
  bedroom count, and an item with no `zpid` (skipped).
- `dataset_live.json`: four items from a live run on 2026-10-07 (#111): an apartment
  and a condo (`property`) and two buildings (`propertyGroup`), from a furnished rent
  search with `maxItems` 10 that cost $0.03. They were recorded with
  `scripts/capture_fixture.py`, then trimmed and reviewed. Zpids are `1000000NN`,
  photo hashes are counters, streets and ZIP codes are `123 Example St` and `00001`
  (in URL slugs too), building names and landmarks are placeholders, descriptions are
  cut short, and map links, the parcel number and the virtual-tour link are removed.
  `_details` keeps only what the adapter reads (`description`, `resoFacts`). The
  shapes are as the actor returned them.
- `dataset_empty.json`: a search with no results.
- `apify_run_*.json`: the run object from `POST /v2/acts/.../runs` and
  `GET /v2/actor-runs/{id}`.

`dataset_items.json` and the run objects were written from the actor's published
README and dataset view, not captured from a live run, so the `_details` and
`unitsGroup` shapes in particular are a best reading of the docs. Every place,
coordinate, id and name is a placeholder (`Exampleville, XX`, `Jane Example`), and
the agent and broker blocks are there to check that they are dropped from `raw`. If a
live run shows a field mapped wrongly, replace the item with a sanitized copy of the
real one and keep the placeholders. `scripts/capture_fixture.py zillow <dataset items
URL>` records and sanitizes a dataset (see CONTRIBUTING.md, "Refreshing fixtures").