# Vendored front-end assets

Served from `/static/vendor/`. They are vendored, not loaded from a CDN, because the
CSP only allows same-origin scripts.

| File | Version | Source | SHA-256 |
|---|---|---|---|
| `htmx.min.js` | 2.0.11 | https://unpkg.com/htmx.org@2.0.11/dist/htmx.min.js | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |
| `swagger-ui/swagger-ui-bundle.js` | 5.33.1 | https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-5.33.1.tgz (`package/`) | `050bc415ee7048dcd881682678f720264e7da5e373f7461d7c58c755305255f7` |
| `swagger-ui/swagger-ui.css` | 5.33.1 | same tarball | `1ac324f7dcd27e4b9386b4bd6421271ec147e922a22c05ba24b11515e9aa6321` |

Swagger UI (Apache-2.0, `swagger-ui/LICENSE`) serves `/api/docs`.

To upgrade, download the new release, update this table, and check
`shasum -a 256` of each file against it.
