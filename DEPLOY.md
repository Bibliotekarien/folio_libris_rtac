# Deploying to bibliotekarien-vps (behind the shared `edge` Caddy)

This service runs as its own `docker compose` stack and is fronted by the
**shared `edge` Caddy container** (the same proxy that serves room40, matomo,
etc.). Caddy does **not** reach the app over a host port — it joins this stack's
`proxy` network and reverse-proxies to the `rtac` container by name:

```
Internet ──▶ edge-caddy (:80/:443, TLS)
                 │  reverse_proxy http://rtac:5000   (on folio_libris_rtac_proxy)
                 ▼
              rtac container (uvicorn :5000, --proxy-headers)
```

The app trusts `X-Forwarded-*` (uvicorn runs with `--proxy-headers
--forwarded-allow-ips "*"`), so client IPs and `https` are preserved.

## How the pieces fit

- **`docker-compose.yml` (this repo)** defines the `rtac` service on a `proxy`
  network. Docker names that network `folio_libris_rtac_proxy` (project name +
  `_proxy`). The `name:` at the top of the file keeps that stable.
- **`edge/docker-compose.yml`** lists `folio_libris_rtac_proxy` as an `external`
  network and attaches the `caddy` service to it.
- **`edge/Caddyfile`** has the `rtac.bibliotekarien.se` site block that proxies
  to `http://rtac:5000`.

## First-time setup

1. DNS: point `rtac.bibliotekarien.se` at the server's public IP. Ports 80 and
   443 must be open (Caddy needs them for the Let's Encrypt cert).

2. Get the code (HTTPS — the repo is public, no SSH key needed):

       git clone https://github.com/fontanka16/folio_libris_rtac.git
       cd folio_libris_rtac

3. Create the per-library settings (gitignored, live only on the server):

       mkdir -p libraries/<sigel>
       cp libraries/example.settings.json libraries/<sigel>/settings.json
       # edit: FOLIO connection (okapi_url, tenant_id, username, password) +
       # identifier-type UUIDs. Repeat per sigel.

4. Start this stack **first** (it creates the `folio_libris_rtac_proxy`
   network that Caddy joins as external):

       docker compose up --build -d
       docker compose ps          # should show "rtac" healthy after ~15s

5. Wire it into the edge proxy. The two edge changes are already in the `edge`
   repo (the external network + the Caddyfile site block); apply them by
   recreating Caddy:

       cd ../edge          # or wherever the edge stack lives on the server
       docker compose up -d --force-recreate caddy

   Caddy then provisions the TLS cert and starts serving
   `https://rtac.bibliotekarien.se`.

## Updating

One command — pull, rebuild, recreate, and wait until the container is healthy
(fails with a non-zero exit and recent logs if anything breaks):

       cd folio_libris_rtac
       scripts/deploy.sh

Useful flags: `--validate` also checks each library's backend API(s) via
`/validate-folio-connection` after the deploy; `--no-pull` deploys the current
working tree as-is; `--logs` follows the log afterwards; `--help` lists the
rest. docker runs via `sudo` by default (`DOCKER=docker` to override).

The script just wraps the manual steps, if you prefer to run them yourself:

       cd folio_libris_rtac
       git pull
       docker compose up --build -d
       docker compose logs -f rtac

Editing a `libraries/<sigel>/settings.json` takes effect without a rebuild
(settings are read per request); `docker compose restart rtac` is harmless if
you prefer.

## Metrics (optional)

To let the central Prometheus (monitoring-vm) scrape the app, enable the
metrics overlay by putting two lines in `.env` next to `docker-compose.yml`:

    COMPOSE_FILE=docker-compose.yml:docker-compose.metrics.yml
    METRICS_BIND_IP=10.0.0.4

and running `docker compose up -d` (or `scripts/deploy.sh`). This publishes
port 9105 on the **private network address only** — never through Caddy. Going
through `.env` rather than `-f` flags means every later plain
`docker compose`/`deploy.sh` run keeps the overlay instead of silently
dropping the metrics port.

Verify from monitoring-vm: `curl http://10.0.0.4:9105/metrics`.

The scrape target (`job_name: rtac` → `10.0.0.4:9105`) lives in the
**observability-platform** repo. Deploy order matters: enable the overlay
here **first**, then the scrape target — a dead target pages within minutes
(TargetNere).

## Verify

    docker compose ps                                                  # rtac healthy
    curl -I https://rtac.bibliotekarien.se/                            # 200, text/html (landing page)
    curl   https://rtac.bibliotekarien.se/<sigel>/validate-folio-connection
    curl  "https://rtac.bibliotekarien.se/<sigel>/rtac?ISBN=9789100000000"

## Notes / gotchas

- **Order matters:** the `rtac` stack must be up before `edge` Caddy is
  (re)created, otherwise the external `folio_libris_rtac_proxy` network doesn't
  exist yet and Caddy fails to start.
- `docker compose ps` is **project-scoped** — run it inside this repo to see
  `rtac`. To see every container/stack on the host use `docker ps -a` and
  `docker compose ls`.
- `libraries/` is gitignored; the credential files exist only on the server and
  are mounted read-only into the container.
- The localhost publish (`127.0.0.1:5000`) in `docker-compose.yml` is only for
  host-side debugging; Caddy reaches the app over the docker network, not that
  port. Remove it if you don't want the app on the host at all.
- The container has CPU/memory limits (`deploy.resources.limits`) so a request
  flood can't exhaust the host — in line with the DDoS/availability threat model.
