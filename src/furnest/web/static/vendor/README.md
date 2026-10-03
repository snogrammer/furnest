# Vendored front-end assets

Served from `/static/vendor/`. They are vendored, not loaded from a CDN, because the
CSP only allows same-origin scripts.

| File | Version | Source | SHA-256 |
|---|---|---|---|
| `htmx.min.js` | 2.0.11 | https://unpkg.com/htmx.org@2.0.11/dist/htmx.min.js | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |

To upgrade, download the new release, update this table, and check
`shasum -a 256 htmx.min.js` against it.
