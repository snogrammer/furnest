# Vendored front-end assets

Served from `/static/vendor/`. They are vendored, not loaded from a CDN, because the
CSP only allows same-origin scripts.

| File | Version | Source | SHA-256 |
|---|---|---|---|
| `htmx.min.js` | 2.0.11 | https://unpkg.com/htmx.org@2.0.11/dist/htmx.min.js | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |
| `swagger-ui/swagger-ui-bundle.js` | 5.33.1 | https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-5.33.1.tgz (`package/`) | `050bc415ee7048dcd881682678f720264e7da5e373f7461d7c58c755305255f7` |
| `swagger-ui/swagger-ui.css` | 5.33.1 | same tarball | `1ac324f7dcd27e4b9386b4bd6421271ec147e922a22c05ba24b11515e9aa6321` |
| `leaflet-1.9.4/leaflet.js` | 1.9.4 | https://registry.npmjs.org/leaflet/-/leaflet-1.9.4.tgz (`package/dist/`) | `db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a` |
| `leaflet-1.9.4/leaflet.css` | 1.9.4 | same tarball | `a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6` |
| `leaflet-1.9.4/images/layers.png` | 1.9.4 | same tarball | `1dbbe9d028e292f36fcba8f8b3a28d5e8932754fc2215b9ac69e4cdecf5107c6` |
| `leaflet-1.9.4/images/layers-2x.png` | 1.9.4 | same tarball | `066daca850d8ffbef007af00b06eac0015728dee279c51f3cb6c716df7c42edf` |
| `leaflet-1.9.4/images/marker-icon.png` | 1.9.4 | same tarball | `574c3a5cca85f4114085b6841596d62f00d7c892c7b03f28cbfa301deb1dc437` |
| `leaflet-1.9.4/images/marker-icon-2x.png` | 1.9.4 | same tarball | `00179c4c1ee830d3a108412ae0d294f55776cfeb085c60129a39aa6fc4ae2528` |
| `leaflet-1.9.4/images/marker-shadow.png` | 1.9.4 | same tarball | `264f5c640339f042dd729062cfc04c17f8ea0f29882b538e3848ed8f10edb4da` |
| `leaflet.markercluster-1.5.3/leaflet.markercluster.js` | 1.5.3 | https://registry.npmjs.org/leaflet.markercluster/-/leaflet.markercluster-1.5.3.tgz (`package/dist/`) | `1e4e1d22972a3926f48598e0caf14e3fe7049835d428a344fed4f9e3665b3508` |
| `leaflet.markercluster-1.5.3/MarkerCluster.css` | 1.5.3 | same tarball | `614dea0a98ff3f4ead74f04918f6b1d1b9ba435c25b5fc23b21a394d1e3e4d87` |
| `leaflet.markercluster-1.5.3/MarkerCluster.Default.css` | 1.5.3 | same tarball | `61258232d98d64dc2a7b1e02130d67421bc5b9bda5994eef70228ff97570c170` |
| `leaflet-geoman-free-2.20.2/leaflet-geoman.min.js` | 2.20.2 | https://registry.npmjs.org/@geoman-io/leaflet-geoman-free/-/leaflet-geoman-free-2.20.2.tgz (`package/dist/`) | `20d0ba38069526be959287b8ae13234f660f92db28de86782ebcad028dda9ed3` |
| `leaflet-geoman-free-2.20.2/leaflet-geoman.css` | 2.20.2 | same tarball | `a0df8e0f6301f9f71d772b2c61913bb9094ae0735fd2223ccd1aecd871ea250c` |

Swagger UI (Apache-2.0, `swagger-ui/LICENSE`) serves `/api/docs`.

Leaflet (BSD-2-Clause), Leaflet.markercluster (MIT) and Leaflet-Geoman Free (MIT) draw the
dashboard's Map view. Each directory keeps its `LICENSE`. The minified scripts end in a
`sourceMappingURL` comment; the `.map` files are not vendored, so the files stay byte-identical
to the tarballs.

To upgrade, download the new release, update this table, and check
`shasum -a 256` of each file against it.
