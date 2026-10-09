# Deployment

furnest ships as one Docker image that runs every role (`web`, `worker`, `migrate`). State lives in SQLite under `/data`, so a single host with a volume is enough. The full setting list is in the [README](../README.md#configuration) and [`.env.example`](../.env.example).

## Docker Compose (local or single host)

[`docker-compose.yml`](../docker-compose.yml) builds the image from your checkout.

```sh
cp .env.example .env     # set AUTH_TOKEN, AUTH_SESSION_SECRET and BASE_URL
docker compose up -d --build
```

`migrate` runs once and exits, then `web` (port 8000, bound to `127.0.0.1`) and `worker` start. The database is kept in the `furnest-data` volume. Generate secrets with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

## Portainer

[`docker-compose.portainer.yml`](../docker-compose.portainer.yml) runs the published image as a git-backed stack.

1. Create the shared proxy network once: `docker network create caddy_net`.
2. In Portainer, add a stack from this repository with compose path `docker-compose.portainer.yml`.
3. Copy [`stack.env.example`](../stack.env.example) into the stack's environment variables and set `FURNEST_IMAGE` (e.g. `registry.example.com/org/furnest:latest`), `FURNEST_DATA_PATH`, `AUTH_TOKEN`, `AUTH_SESSION_SECRET` and `BASE_URL`.
4. Deploy. Updating means pulling the new image and redeploying; the `migrate` service applies schema changes first.

The repository's `publish` workflow can push images and trigger the Portainer redeploy; see the CI section of the README for the variables and secrets it uses.

## Caddy

Let Caddy terminate TLS in front of the `web` container. [`deploy/Caddyfile.example`](../deploy/Caddyfile.example) is a starting point:

```caddyfile
furnest.example.com {
	reverse_proxy furnest-web:8000
}
```

Then, in the stack environment:

- `BASE_URL=https://furnest.example.com`
- `WEB_TRUSTED_PROXIES=<caddy_net subnet>`, as narrow as you can, so login rate limiting sees real client IPs and HTTPS is detected.

## Operations

- **Health:** `GET /healthz` (liveness) and `GET /readyz` (database, migrations and worker heartbeat). Containers also run `furnest healthcheck web|worker`.
- **Backups:** stop the stack or use `sqlite3 furnest.db ".backup backup.db"` on the file in `/data`.
- **Shutdown:** set the stop grace period above `WORKER_SHUTDOWN_TIMEOUT_SECONDS` (default 25s).
- **Browser sources:** build with `--build-arg WITH_PLAYWRIGHT=true` and set `SOURCES_BROWSER_ENABLED=true` for JS-heavy sites.
- **Integrations:** Discord setup is in [discord-setup.md](discord-setup.md); adapters are in [adapters.md](adapters.md).
