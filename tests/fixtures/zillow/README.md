# Zillow fixtures

JSON returned by the Apify actor `igolaizola/zillow-scraper-ppe`, which the `zillow`
adapter runs (see `docs/adapters.md`).

- `dataset_items.json`: dataset items from a furnished rent search. It covers a house
  with details (`_details`), an apartment with no details and odd values, an
  apartment building (`propertyGroup`) with three unit groups, one of them without a
  bedroom count, and an item with no `zpid` (skipped).
- `dataset_empty.json`: a search with no results.
- `apify_run_*.json`: the run object from `POST /v2/acts/.../runs` and
  `GET /v2/actor-runs/{id}`.

These were written from the actor's published README and dataset view, not captured
from a live run, so the `_details` and `unitsGroup` shapes in particular are a best
reading of the docs. Every place, coordinate, id and name is a placeholder
(`Exampleville, XX`, `Jane Example`), and the agent and broker blocks are there to
check that they are dropped from `raw`. If a live run shows a field mapped wrongly,
replace the item with a sanitized copy of the real one and keep the placeholders.
`scripts/capture_fixture.py zillow <dataset items URL>` records and sanitizes a
dataset (see CONTRIBUTING.md, "Refreshing fixtures").
