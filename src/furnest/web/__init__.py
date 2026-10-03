"""The ``furnest web`` process: FastAPI app, dashboard templates and health endpoints.

* :mod:`furnest.web.app`: ``create_app`` factory and its lifespan (engine, auto-migrate).
* :mod:`furnest.web.headers`: security headers, including the strict self-only CSP.
* :mod:`furnest.web.health`: readiness checks behind ``/readyz``.
* :mod:`furnest.web.errors`: 404/500 pages that never show stack traces.
* :mod:`furnest.web.routes`: route modules, one router each.

Templates are Jinja2 and the only JavaScript is vendored htmx, so there is no
JS build step. Auth (#7) is layered on later.
"""
