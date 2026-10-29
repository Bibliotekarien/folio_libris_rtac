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

2. Create the service user and get the code. `/opt/rtac` and the checkout
   are owned by root and `scripts/deploy.sh` runs git and docker as root; the
   `rtac` user owns nothing and is **not** in the docker group (see
   "Root-owned checkout" below). HTTPS, the repo is public:

       sudo useradd --system --uid 1506 --home-dir /opt/rtac --shell /usr/sbin/nologin rtac
       sudo install -d -o root -g root -m 0755 /opt/rtac
       sudo git clone https://github.com/Bibliotekarien/folio_libris_rtac.git /opt/rtac/app
       sudo chmod -R go-w /opt/rtac/app

3. Create the per-library settings (gitignored, live only on the server). They
   hold FOLIO credentials, so they are not world-readable: owner root, group
   `10001` — the uid/gid the container runs as (`appuser` in the Dockerfile) —
   mode 0640:

       sudo mkdir -p /opt/rtac/app/libraries/<sigel>
       sudo cp /opt/rtac/app/libraries/example.settings.json /opt/rtac/app/libraries/<sigel>/settings.json
       # edit (sudoedit): FOLIO connection (okapi_url, tenant_id, username,
       # password) + identifier-type UUIDs. Repeat per sigel.
       sudo chown root:10001 /opt/rtac/app/libraries/*/settings.json
       sudo chmod 0640 /opt/rtac/app/libraries/*/settings.json

4. Start this stack **first** (it creates the `folio_libris_rtac_proxy`
   network that Caddy joins as external), then enable the boot unit:

       sudo /opt/rtac/app/scripts/deploy.sh      # healthy after ~15s, or it fails with logs
       sudo cp /opt/rtac/app/deploy/rtac.service /etc/systemd/system/
       sudo systemctl daemon-reload && sudo systemctl enable --now rtac.service

5. Wire it into the edge proxy. The two edge changes are already in the `edge`
   repo (the external network + the Caddyfile site block); apply them by
   recreating Caddy:

       cd ../edge          # or wherever the edge stack lives on the server
       docker compose up -d --force-recreate caddy

   Caddy then provisions the TLS cert and starts serving
   `https://rtac.bibliotekarien.se`.

## Updating

One command — fetch, show the target commit, fast-forward, rebuild, recreate,
and wait until the container is healthy (fails with a non-zero exit and recent
logs if anything breaks):

       sudo /opt/rtac/app/scripts/deploy.sh --check   # what would be deployed; changes nothing
       sudo /opt/rtac/app/scripts/deploy.sh

Useful flags: `--validate` also checks each library's backend API(s) via
`/validate-folio-connection` after the deploy; `--no-pull` deploys the current
checkout as-is; `--logs` follows the log afterwards; `--help` lists the rest,
including `GIT_REF` / `REQUIRE_SIGNED` for signed releases.

The stack is also part of bibliotekarien-platform's `make deploy-all`, which
runs every stack's `--check` and then each `deploy.sh` in turn.

Editing a `libraries/<sigel>/settings.json` takes effect without a rebuild
(settings are read per request); `docker compose restart rtac` is harmless if
you prefer.

## Root-owned checkout

`/opt/rtac` and `/opt/rtac/app` are owned by root, and `scripts/deploy.sh`
runs git and docker as root. Before this, `rtac` owned both and was in the
docker group, which gave three ways to root:

- **The docker group is root.** Anyone who can talk to the docker socket can
  start a container that mounts `/`.
- **Root ran files rtac could write:** `deploy.sh` (via sudo) and the compose
  files (`rtac.service` at boot, also through `make deploy-all`).
- **rtac owned `/opt/rtac`** and could swap out the whole checkout
  (`mv app x; mkdir app`), scripts included.

`deploy.sh` refuses to run if any of this is true again. The same change was
made in mark-rounding, teaball, grib-ranker and tOPAC; see Risk 1 in
bibliotekarien-platform `SECURITY_REVIEW.md`.

**Moving an existing server** (once; the new `deploy.sh` refuses to run
before). `chown -R root:root` resets the group on `settings.json`, so it is set
back to `10001` straight after — otherwise the container can no longer read
its settings:

    sudo chown root:root /opt/rtac
    sudo chmod 755 /opt/rtac
    sudo chown -R root:root /opt/rtac/app
    sudo chown root:10001 /opt/rtac/app/libraries/*/settings.json
    sudo chmod 0640 /opt/rtac/app/libraries/*/settings.json
    sudo git -C /opt/rtac/app pull --ff-only
    sudo chmod -R go-w /opt/rtac/app
    sudo restorecon -R /opt/rtac/app
    sudo gpasswd -d rtac docker
    sudo cp /opt/rtac/app/deploy/rtac.service /etc/systemd/system/
    sudo systemctl daemon-reload

Then check:

    id rtac                                         # no docker
    sudo /opt/rtac/app/scripts/deploy.sh --check
    sudo /opt/rtac/app/scripts/deploy.sh --no-pull --no-build --validate

The last line recreates the container and asks each library's FOLIO through
the app — it proves the container can still read every `settings.json`.

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
