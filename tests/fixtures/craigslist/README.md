# Craigslist fixtures

HTML pages the `craigslist` adapter reads (see `docs/adapters.md`). They were captured
live on 2026-10-07 (#47) with `scripts/capture_fixture.py craigslist <url>`, which
fetches through the adapter's own HTTP client and runs the sanitizer, and then
reviewed by hand.

- `search_apa.html`: a furnished apartments/housing search of one site, narrowed by
  price, beds and distance to 14 results. It has both the static results list and
  the JSON-LD ItemList the adapter zips together.
- `detail_apartment.html`: an apartment posting with beds/baths, square footage,
  `available now`, a `rent period` and an extra labelled chip (application fee),
  amenity chips and 14 photos.
- `detail_room.html`: a sublet whose chips say `private room` (with `apartment`) and
  `laundry in bldg`, with no street address.
- `detail_complex.html`: a managed complex's posting, with a long templated
  description.

The sanitizer rounded coordinates and replaced street addresses with `123 Example
St`. Building names, a marketing short link and an address-like title were replaced
with `Jane Example` using `--redact`. Craigslist pages carry no phone numbers or
emails (they sit behind "show contact info"). Cases a live page rarely shows (an
email in the body, a weekly rent period, a taken-down posting, a blocked page, an
empty search) are built inline in `tests/unit/test_craigslist.py`.
