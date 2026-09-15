# FOLIO LIBRIS RTAC

Acts as an RTAC (Real Time Availability Check) API for the Swedish union
catalog **Libris**, backed by one or more **FOLIO** tenants. The screen cast
below shows the RTAC response displayed in Libris:

![screen shot of RTAC](docs/libris_rtac.gif)

## What it does

Libris asks an external system whether a given title is available and where it
is held. This service answers that question by talking to FOLIO and returning
the XML document Libris expects. The Libris loan-status (*lånestatus*)
integration this implements is specified by the National Library of Sweden —
see [Libris lånestatus 2025 (PDF)](https://www.kb.se/download/18.53200c4319739465c5d2e7/1749808483695/Libris%20l%C3%A5nestatus%202025.pdf).

- **Multi-tenant.** Each library is identified by its *sigel* and every request
  is scoped to one, e.g. `GET /<sigel>/rtac?ISBN=...`. A sigel maps to its own
  FOLIO connection and identifier configuration under `libraries/<sigel>/`.
- **Identifier lookup.** A request carries one or more identifiers
  (`Bib_ID`, `ONR`, `ISSN`, `ISBN`). Each is translated into a CQL query against
  FOLIO's `instance-storage/instances`, using the per-library FOLIO
  *identifier-type* UUIDs configured for that parameter.
- **Availability.** For the first matching instance the service fetches holdings
  from one of three FOLIO backends, selected per library via `rtac_backend`:
  `"rtac-cache"` (mod-rtac-cache via the gateway — okapi token, includes loan
  type), `"edge"` (edge-rtac via the edge service — needs a per-library apiKey,
  includes loan type), or `"rtac"` (default; mod-rtac via the gateway —
  deprecated and without loan type). The holdings are mapped into the RTAC
  `<Item_Information>` XML document (location, call number, loan policy, status,
  due date …).
- **Always answers.** When nothing matches — or FOLIO errors — it returns a
  valid XML document with a single `Okänd` ("Unknown") placeholder item rather
  than an error, so Libris always gets a well-formed response.

### Request flow

```
Libris ──GET /<sigel>/rtac?ISBN=…──▶ this service
                                       │  1. load libraries/<sigel>/settings.json
                                       │  2. build CQL from configured identifier-type UUIDs
                                       │  3. FOLIO: find instance ──▶ /rtac/{id} for holdings
                                       ▼
Libris ◀──── <Item_Information> XML ───┘
```

## What changed since the fork

This repository began life as
[`FOLIO-FSE/folio_stats`](https://github.com/FOLIO-FSE) — a small Flask app for
collecting and displaying statistics from a FOLIO instance. Since the fork it
has been repurposed into a dedicated Libris RTAC service. The main work:

- **Repurposed the app.** Replaced the FOLIO-statistics Flask app with a focused
  **FastAPI** RTAC API (`application.py`) that serves the Libris availability
  use case.
- **Multi-tenant by sigel.** Introduced per-library configuration under
  `libraries/<sigel>/settings.json` (FOLIO connection + identifier-type UUIDs),
  with the sigel as the first path segment of every endpoint. Added the index
  (`GET /`) and a `GET /<sigel>/validate-folio-connection` health/diagnostics
  endpoint.
- **Configurable identifier mapping.** Each query parameter (`Bib_ID`, `ONR`,
  `ISSN`, `ISBN`) maps to one or more FOLIO identifier-type UUIDs, OR-joined
  into a single CQL query; an empty list disables an identifier per library.
- **Security hardening.** Added input validation that doubles as a
  **CQL-injection guard** (identifier values and configured UUIDs are
  pattern-checked before interpolation), and a **path-traversal guard** on the
  sigel (only configured sigels resolve to a settings file).
- **Resilience & DoS resistance.** Added a per-sigel cached, logged-in
  `FolioClient` with a TTL and per-sigel locking so a burst of requests triggers
  a single FOLIO login instead of one per request; automatic re-login on a
  401/403 (expired token); a global socket timeout so a hung FOLIO connection
  can't tie up a worker; and graceful error handling that still returns valid
  RTAC XML.
- **Modern tooling.** Moved dependency management to [uv](https://docs.astral.sh/uv/)
  (`pyproject.toml` / `uv.lock`) and pinned Python 3.12.
- **Containerised & deployable.** Added a non-root `Dockerfile`, a
  `docker-compose.yml` with resource limits and a health check, and
  [DEPLOY.md](DEPLOY.md) describing deployment behind a Caddy reverse proxy.
  Credentials live only in mounted, gitignored `settings.json` files — never in
  the image.
- **Test suite.** Added `pytest` coverage for the identifier/CQL builder,
  injection and path-traversal guards, the client cache and auth-retry logic,
  XML generation, settings loading, and the HTTP endpoints.

## Configuring libraries

Each library is configured in `libraries/<sigel>/settings.json`. Copy
`libraries/example.settings.json` to `libraries/<sigel>/settings.json` and fill
in the FOLIO connection and the identifier-type UUIDs. See
[libraries/README.md](libraries/README.md) for the full field reference.

Real `settings.json` files contain credentials and are gitignored; the location
of the directory can be overridden with the `LIBRARIES_PATH` environment
variable.

### FOLIO permissions

The FOLIO user needs **read-only** permissions. Assign the following 11
permissions; the full machine-readable list (with permission-set names) is in
[libraries/folio-permissions.json](libraries/folio-permissions.json).

| Permission | Purpose |
| --- | --- |
| `inventory-storage.instances.collection.get` | get instance collection from storage |
| `inventory-storage.instances.item.get` | get individual instance from storage |
| `inventory-storage.holdings.collection.get` | get holdings collection from storage |
| `inventory-storage.holdings.item.get` | get individual holdings record from storage |
| `inventory-storage.items.collection.get` | get item collection from storage |
| `inventory-storage.items.item.get` | get individual item from storage |
| `inventory-storage.loan-types.collection.get` | get loan-type collection from storage |
| `inventory-storage.loan-types.item.get` | get individual loan-type from storage |
| `inventory-storage.locations.collection.get` | get location collection |
| `inventory-storage.locations.item.get` | get individual location |
| `rtac.holdings.item.get` | Legacy RTAC (mod-rtac) — get individual holding |

## Installation

This project uses [uv](https://docs.astral.sh/uv/) for dependency management.

- Install uv (see the link above).
- Clone the repository.
- Run `uv sync` to create the virtual environment and install dependencies. uv
  will pick the Python version from `.python-version` (3.12).
- Configure one or more libraries (see [Configuring libraries](#configuring-libraries)).
- Run the app with `uv run uvicorn application:application --reload --port 5000`
  (or `uv run python application.py`).
- For local debugging, set `LOG_LEVEL=debug` to log the app's own lines and every
  outgoing FOLIO/edge HTTP request (httpx): `LOG_LEVEL=debug uv run uvicorn
  application:application --reload --port 5000`. httpx logs request lines and
  status only (not headers or bodies), so credentials are not exposed. Add
  uvicorn's `--log-level debug` too if you also want its access logs.

## Endpoints

Every endpoint is scoped to a library sigel as the first path segment.

- `GET /<sigel>/rtac?Bib_ID=<libris-id>` — the RTAC lookup. Also accepts `ONR`,
  `ISSN`, and `ISBN`. Returns an XML `<Item_Information>` response. Rate-limited
  per client IP (`RTAC_RATE_LIMIT`, default `30/minute`); a request whose
  `?token=` matches the library's `fast_track_token` is exempt, so Libris's
  registered status URL is never throttled. A throttled request still gets a
  valid (empty) RTAC document. Issue a token with
  `python scripts/issue_token.py <sigel>` (see [libraries/README.md](libraries/README.md)).
- `GET /<sigel>/validate-folio-connection` — checks the API(s) a library's rtac
  request uses. Every backend starts by resolving the instance in FOLIO, so this
  logs in **and** runs a minimal `/instance-storage/instances` query (so a login
  that lacks inventory-read is caught, not just bad credentials); the `200` body
  reports `backend` and `folio: {"status": "ok"}`. When `backend` is `edge`, the
  edge-rtac hop is probed too and reported under `edge`. Returns `404` for an
  unknown sigel, `503` if FOLIO (or, for edge, edge url/apiKey) settings are
  missing, `502` if FOLIO or edge-rtac is unreachable or rejects the credentials.
- `GET /` — an HTML landing page describing the service (in Swedish), with a
  link to the Libris documentation, onboarding instructions for libraries, and
  the list of configured sigels.
- `GET /docs` — interactive API documentation.

## Metrics (optional)

The service can expose Prometheus metrics — **off by default**. Unless the
`METRICS_PORT` environment variable is set, nothing new listens and nothing
changes, so operators who don't want metrics can ignore this section entirely.

To try it locally:

    METRICS_PORT=9105 uv run uvicorn application:application --port 5000
    curl localhost:9105/metrics

`METRICS_HOST` (default `0.0.0.0`) sets the bind address. In Docker, use the
ready-made overlay instead — see [docker-compose.metrics.yml](docker-compose.metrics.yml).
The exporter deliberately runs on its own port, never as a route on the app
port: the app port is typically fronted by a public reverse proxy, and metrics
should be reachable only by your monitoring.

What is exported:

| Metric | Labels | What it tells you |
| --- | --- | --- |
| `rtac_requests_total` | `sigel`, `channel`, `outcome` | lookups in, by how they ended: `holdings` / `empty` / `error` / `no_identifier` / `rate_limited`. `channel` separates Libris's fast-track URL (`fast_track`) from `public` traffic |
| `rtac_request_seconds` | `sigel` | lookup duration histogram |
| `rtac_upstream_requests_total` | `sigel`, `target`, `status` | outbound calls per upstream API (`folio_login`, `folio_instance_search`, `folio_rtac`, `folio_rtac_cache`, `edge_rtac`), with the HTTP status or a synthetic one (`timeout`, `connect_error`, `transport_error`, `error`) |
| `rtac_upstream_request_seconds` | `target` | upstream call duration histogram |
| `rtac_folio_auth_retries_total` | `sigel` | lookups retried with a fresh FOLIO login after a 401/403 |

The `error` outcome is the signal HTTP-level monitoring cannot see: this
service always answers 200 with valid XML even when FOLIO is down (the `Okänd`
placeholder — see "Always answers" above), so upstream failures never show in
status codes. `rate(rtac_requests_total{outcome="error"}[5m])` does show them.

**No personal data.** Label values come only from configuration or fixed
vocabularies — never identifier values, client IPs, tokens, or dates. Requests
for unknown sigels are recorded under the single label value `_unknown`, so
arbitrary URLs cannot create new time series. The full doctrine is documented
at the top of [metrics.py](metrics.py).

The exporter assumes a single application process (the default here, and how
the container runs). With multiple uvicorn workers each process keeps its own
counters and only one can bind the port — that setup needs
`prometheus_client`'s multiprocess mode instead.

## Deployment

See [DEPLOY.md](DEPLOY.md) for running in Docker behind a Caddy reverse proxy.

## Tests

Run the test suite with `uv run pytest`.
