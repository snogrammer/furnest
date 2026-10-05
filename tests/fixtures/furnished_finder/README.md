# Furnished Finder fixtures

JSON returned by the Apify actor `abotapi/furnishedfinder-scraper`, which the
`furnished_finder` adapter runs (see `docs/adapters.md`).

- `dataset_items.json`: dataset items, one per listing. It covers an entire unit with
  details, a room with only a text price and a date to parse, a studio, and an item
  with no id (skipped).
- `dataset_error_row.json`: the actor's "nothing was scanned" row.
- `apify_run_*.json`: the run object from `POST /v2/acts/.../runs` and
  `GET /v2/actor-runs/{id}` in each status the client handles.

These were written from the actor's published output schema and example, not captured
from a live run. Every place, coordinate, id and name is a placeholder
(`Exampleville, XX`, `Jane Example`), and the review is there to check that reviewer
names are dropped from `raw`. If a live run shows a field mapped wrongly, replace the
item with a sanitized copy of the real one and keep the placeholders.
