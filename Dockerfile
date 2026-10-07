# syntax=docker/dockerfile:1.7
# One image, three roles: `furnest web` (the default), `furnest worker` and
# `furnest migrate`. Pick one with the container command, e.g.
# `docker run <image> worker`.
#
# Build args:
#   WITH_PLAYWRIGHT=true  also install Playwright + Chromium for sources that need
#                         a real browser (adds several hundred MB; default false)
#   GIT_SHA               commit recorded in the image's revision label

ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3

FROM ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d AS uv

# ── build: resolve the locked, non-editable venv ─────────────────────────────
FROM ${PYTHON_IMAGE} AS build

COPY --from=uv /uv /usr/local/bin/uv

# No bytecode compilation: it bloats the image, and long-running processes only
# pay the compile once at startup.
ENV UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

ARG WITH_PLAYWRIGHT=false

WORKDIR /src

# Dependency layer: rebuilt only when the lock changes.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN extras=""; [ "$WITH_PLAYWRIGHT" = "true" ] && extras="--extra browser"; \
    uv sync --frozen --no-dev --no-install-project $extras

# Project layer, installed non-editable so the venv is self-contained.
COPY src/ ./src/
RUN extras=""; [ "$WITH_PLAYWRIGHT" = "true" ] && extras="--extra browser"; \
    uv sync --frozen --no-dev --no-editable $extras \
    && find /app/.venv -depth -type d -name __pycache__ -exec rm -rf {} +

# ── runtime: slim Python, no build tooling ───────────────────────────────────
FROM ${PYTHON_IMAGE} AS runtime

ARG WITH_PLAYWRIGHT=false
ARG GIT_SHA=dev

LABEL org.opencontainers.image.title="furnest" \
      org.opencontainers.image.description="Self-hosted furnished-rental monitor" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${GIT_SHA}"

# /data starts out owned by furnest, so a new named volume copies that ownership.
# A bind mount doesn't, which is what the entrypoint is for.
RUN groupadd --gid 10001 furnest \
    && useradd --uid 10001 --gid furnest --create-home --home-dir /home/furnest \
        --shell /usr/sbin/nologin furnest \
    && install -d -o furnest -g furnest -m 0750 /data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    DATA_DIR=/data \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8000 \
    LOG_FORMAT=json

# The venv stays root-owned, so the app user cannot modify its own code.
COPY --from=build /app/.venv /app/.venv

# Chromium (headless shell) and its system libraries, only when asked for. Installed as root into
# a shared path the app user can read.
RUN if [ "$WITH_PLAYWRIGHT" = "true" ]; then \
        playwright install --with-deps --only-shell chromium \
        && rm -rf /var/lib/apt/lists/*; \
    fi

COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/furnest-entrypoint

# No USER: furnest-entrypoint starts as root only to take ownership of /data (a
# bind mount Docker created is root-owned), then runs furnest as uid 10001.
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8000

# Probes the web process. Compose overrides this for the worker
# (`furnest-entrypoint healthcheck worker`); `migrate` exits before it matters.
# Through the entrypoint so the probe runs as uid 10001: the worker probe opens
# the database, and as root it could leave root-owned WAL files behind.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["furnest-entrypoint", "healthcheck", "web"]

ENTRYPOINT ["furnest-entrypoint"]
CMD ["web"]
