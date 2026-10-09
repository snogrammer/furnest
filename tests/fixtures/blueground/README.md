# Blueground fixtures

City pages the `blueground` adapter reads (see `docs/adapters.md`), captured live on
2026-10-09 from `/furnished-apartments-chicago-usa` and cut down by hand to four homes
each:

- `city_unpriced.html`: the page without dates, where every `rent.amount` is 0 and the
  state reports 649 homes in all.
- `city_priced.html`: the page with `checkIn`/`checkOut`, where each home has its
  monthly rent for the stay.

Each holds the JSON-LD `Apartment` list and a pared-down `window.__INITIAL_STATE__`
(the home rows, the state from the city's ancestry, the total). Photos were cut to
five per home, coordinates moved into the placeholder area (Springfield, IL), and the sales phone number removed
from the JSON-LD; the page's CSRF and analytics keys were dropped with the rest of the
state. Cases a live page rarely shows (a 404 city, a missing state, a second page)
are built inline in `tests/unit/test_blueground.py`.
