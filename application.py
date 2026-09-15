import html
import json
import logging
import math
import os
import re
import secrets
import threading
import time

import httpx
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from folioclient import (
    FolioAuthenticationError,
    FolioClient,
    FolioPermissionError,
)
from lxml import etree
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import metrics

logger = logging.getLogger("rtac")

# Optional logging for local debugging. Set LOG_LEVEL (e.g. DEBUG, INFO) to
# attach a stderr handler and raise the app's log level; at DEBUG this also turns
# on httpx, so every outgoing FOLIO/edge HTTP request is logged — request line
# and status only, not headers or bodies, so the okapi password / edge apiKey are
# never exposed. Unset (the default, incl. in the container), logging is left to
# uvicorn and only warnings/errors surface.
_LOG_LEVEL = os.environ.get("LOG_LEVEL")
if _LOG_LEVEL:
    _level = getattr(logging, _LOG_LEVEL.strip().upper(), None)
    if isinstance(_level, int):
        logging.basicConfig(level=_level)
        logger.setLevel(_level)
        if _level <= logging.DEBUG:
            logging.getLogger("httpx").setLevel(logging.DEBUG)
    else:
        logger.warning("Ignoring invalid LOG_LEVEL=%r", _LOG_LEVEL)

application = FastAPI()

# Rate limit for the rtac endpoint, keyed per client IP. A request that carries
# a library's configured fast_track_token costs 0 and is therefore never limited
# — Libris's registered status URL embeds the token, so legitimate availability
# checks are unaffected while the public path is capped. Throttled requests still
# get a valid (empty) RTAC document. Tune via RTAC_RATE_LIMIT (limits syntax).
RTAC_RATE_LIMIT = os.environ.get("RTAC_RATE_LIMIT", "30/minute")
limiter = Limiter(key_func=get_remote_address, default_limits=[])
application.state.limiter = limiter

LIBRARIES_DIR = os.environ.get("LIBRARIES_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "libraries"
)

def _positive_float_env(name, default):
    """Read a positive, finite float from the environment.

    Falls back to `default` (with a warning) on anything that would be a
    footgun: a non-numeric value would otherwise crash at import, and a value
    <= 0 / non-finite would break things — e.g. a 0 timeout makes every FOLIO
    request time out immediately.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default
    if not math.isfinite(value) or value <= 0:
        logger.warning(
            "Invalid %s=%r (must be > 0); using default %s", name, raw, default
        )
        return default
    return value


# Bound how long a hung FOLIO call can tie up a worker thread. Passed to each
# FolioClient as its (httpx) request timeout.
FOLIO_TIMEOUT = _positive_float_env("FOLIO_TIMEOUT", 15.0)

# Cache one logged-in FolioClient per sigel so a burst of requests does not turn
# into a burst of FOLIO logins (DoS amplification). A per-sigel lock serialises
# the login so a cold-cache burst for one sigel triggers a single login, not N.
FOLIO_CLIENT_TTL = _positive_float_env("FOLIO_CLIENT_TTL", 300.0)
_client_cache = {}
_client_cache_lock = threading.Lock()
_client_locks = {}
_client_locks_guard = threading.Lock()

# Accepted characters in an identifier value before it is interpolated into the
# CQL query. The end is anchored with \Z (not $, which also matches before a
# trailing newline) and the '"' that would break out of the quoted term is
# excluded, to prevent CQL injection.
_VALID_IDENTIFIER = re.compile(r"^[A-Za-z0-9 ._:/-]{1,128}\Z")

# Configured identifier-type ids must be canonical UUIDs. This catches config
# mistakes (with a clear log line) and, like the value guard above, keeps a
# stray '"' from being interpolated into the CQL query.
_VALID_TYPE_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

ITEM_FIELDS = [
    "Item_no",
    "UniqueItemId",
    "Location",
    "Call_No",
    "Loan_Policy",
    "Status",
    "Status_Date_Description",
    "Status_Date",
]


def available_sigels():
    """Return the configured library sigels (sub-dirs holding a settings.json)."""
    if not os.path.isdir(LIBRARIES_DIR):
        return []
    return sorted(
        name
        for name in os.listdir(LIBRARIES_DIR)
        if os.path.isfile(os.path.join(LIBRARIES_DIR, name, "settings.json"))
    )


def load_settings(sigel):
    """Load settings.json for a library sigel.

    The sigel is validated against the configured libraries, which also guards
    against path traversal: an unknown or malicious sigel is never in the list.
    """
    if sigel not in available_sigels():
        raise FileNotFoundError("Unknown library sigel: {}".format(sigel))
    path = os.path.join(LIBRARIES_DIR, sigel, "settings.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _is_fast_track(request):
    """True if the request carries the correct fast-track token for its sigel.

    The token rides in the `?token=` query parameter of the library's registered
    Libris status URL. It is compared in constant time and never interpolated
    anywhere, so it is purely a rate-limit bypass marker (not a secret to guard).
    Returns False on any miss: no token, unknown sigel, or no token configured.
    """
    token = request.query_params.get("token")
    sigel = request.path_params.get("sigel")
    if not token or not sigel:
        return False
    try:
        configured = load_settings(sigel).get("fast_track_token")
    except Exception:
        return False
    if not configured:
        return False
    return secrets.compare_digest(str(token), str(configured))


def _rtac_cost(request):
    """Rate-limit cost of a request: 0 (exempt) when fast-tracked, else 1."""
    return 0 if _is_fast_track(request) else 1


def _new_folio_client(settings):
    """Construct (and log in) a fresh FolioClient from settings.

    The settings' "okapi_url" is the FOLIO gateway / Okapi base URL; FOLIO_TIMEOUT
    bounds each request.
    """
    return FolioClient(
        settings["okapi_url"],
        settings["tenant_id"],
        settings["username"],
        settings["password"],
        timeout=FOLIO_TIMEOUT,
    )


def _probe_folio_connection(folio_client):
    """Exercise the instance-search API that every backend starts from.

    A successful login alone doesn't prove the user may read inventory; a minimal
    instance-storage query (limit=1) confirms /instance-storage/instances is
    reachable and authorized — the first hop of an rtac request in every backend,
    edge included.
    """
    folio_client.folio_get_single_object("/instance-storage/instances?limit=1")


def _sigel_lock(sigel):
    """Return a per-sigel lock, creating it on first use."""
    with _client_locks_guard:
        lock = _client_locks.get(sigel)
        if lock is None:
            lock = threading.Lock()
            _client_locks[sigel] = lock
        return lock


def get_folio_client(sigel, settings):
    """Return a cached FolioClient for the sigel, logging in only when needed.

    Reuses a client for up to FOLIO_CLIENT_TTL seconds so a burst of requests
    does not each trigger a FOLIO login. The per-sigel lock plus the re-check
    inside it mean a cold-cache burst for one sigel triggers a single login.
    """
    now = time.monotonic()
    with _client_cache_lock:
        cached = _client_cache.get(sigel)
        if cached and cached[1] > now:
            return cached[0]
    with _sigel_lock(sigel):
        now = time.monotonic()
        with _client_cache_lock:
            cached = _client_cache.get(sigel)
            if cached and cached[1] > now:
                return cached[0]
        # Only logins on the live lookup path are measured (this function).
        # /validate-folio-connection logs in too, but that endpoint IS
        # monitoring — counting its logins would drown the real traffic.
        with metrics.measured_upstream(sigel, "folio_login"):
            client = _new_folio_client(settings)
        with _client_cache_lock:
            _client_cache[sigel] = (client, now + FOLIO_CLIENT_TTL)
        return client


def _close_client(client):
    """Best-effort close of a FolioClient's underlying httpx connection pool."""
    try:
        client.close()
    except Exception:
        pass


def _invalidate_client(sigel):
    """Drop (and close) any cached client for the sigel, e.g. after an auth failure."""
    with _client_cache_lock:
        cached = _client_cache.pop(sigel, None)
    if cached is not None:
        _close_client(cached[0])


def append_item(root, values):
    """Append an <Item> element with the standard RTAC fields to `root`."""
    item = etree.SubElement(root, "Item")
    for tag in ITEM_FIELDS:
        value = values.get(tag, "")
        etree.SubElement(item, tag).text = "" if value is None else str(value)
    return item


def _loan_type_name(value):
    """Resolve a loan-type value to its name.

    The value may be a plain name string or an object with a "name"; returns
    "" when absent or empty.
    """
    if isinstance(value, dict):
        value = value.get("name")
    return value or ""


def _loan_policy(holding):
    """Loan_Policy from edge-rtac's loan type, if present.

    edge-rtac supplies permanentLoanType (mod-rtac does not), so this is empty
    on the mod-rtac fallback. When temporaryLoanType is set it takes precedence
    over permanentLoanType. Each value may be a plain name string or an object
    with a "name"; returns "" when absent or empty.
    """
    temporary = _loan_type_name(holding.get("temporaryLoanType"))
    if temporary:
        return temporary
    return _loan_type_name(holding.get("permanentLoanType"))


def holding_values(holding):
    return {
        "Item_no": "1",
        "UniqueItemId": holding.get("id", ""),
        "Location": holding.get("location", ""),
        "Call_No": holding.get("callNumber", ""),
        "Loan_Policy": _loan_policy(holding),
        "Status": holding.get("status", ""),
        "Status_Date_Description": "",
        "Status_Date": holding.get("dueDate", "")[:10],
    }


def empty_item_information():
    """Build an <Item_Information> document with a single 'Okänd' placeholder."""
    root = etree.Element("Item_Information")
    append_item(root, {"Status": "Okänd"})
    return root


def _type_id_list(configured, name=None):
    """Normalise a configured identifier-type UUID setting to a list.

    Accepts a list of UUIDs, a single UUID string, or a comma-separated string.
    Missing/empty values (and empty entries) yield an empty list — they simply
    do not count. Non-empty entries that are not canonical UUIDs are dropped
    with a warning (a config mistake, and a CQL-injection guard). `name` is the
    identifier this setting belongs to, used only for the log line.
    """
    if not configured:
        return []
    if isinstance(configured, str):
        configured = configured.split(",")
    type_ids = []
    for type_id in configured:
        if type_id is None:
            continue
        type_id = str(type_id).strip()
        if not type_id:
            continue
        if not _VALID_TYPE_ID.match(type_id):
            logger.warning(
                "Ignoring invalid identifier-type UUID for %s: %r",
                name or "?",
                type_id,
            )
            continue
        type_ids.append(type_id)
    return type_ids


def build_identifier_query(identifiers, identifier_type_ids):
    """Build the CQL instance query from the supplied identifier values.

    `identifiers` maps a query-parameter name (Bib_ID, ONR, ISSN, ISBN) to its
    value. `identifier_type_ids` maps the same names to the library's
    identifier-type UUID(s) — a list, a single UUID string, or a comma-separated
    string. For each value that is set, one OR-clause is added per configured
    UUID; values without any configured UUID are skipped. Returns None when no
    clause could be built.
    """
    clauses = []
    for name, value in identifiers.items():
        if not value:
            continue
        if not _VALID_IDENTIFIER.match(value):
            logger.warning("Ignoring invalid %s identifier value: %r", name, value)
            continue
        for type_id in _type_id_list(identifier_type_ids.get(name), name):
            clauses.append(
                'identifiers=/@value/@identifierTypeId="{}" "{}"'.format(type_id, value)
            )
    if not clauses:
        return None
    return "?query=(" + " or ".join(clauses) + ")"


# Global fallbacks for the edge-rtac backend, used when a library's settings
# omit them. edge-rtac needs an edge API key to return data (with only an okapi
# token it answers 200 but with empty holdings), so the key is required there.
EDGE_RTAC_URL = os.environ.get("EDGE_RTAC_URL")
EDGE_RTAC_API_KEY = os.environ.get("EDGE_RTAC_API_KEY")

# How a library's holdings are fetched once the instance is resolved. Selected
# per library via the "rtac_backend" setting; see create_rtac_response.
RTAC_BACKENDS = ("rtac-cache", "edge", "rtac")
DEFAULT_RTAC_BACKEND = "rtac"


def _edge_rtac_config(settings):
    """Resolve the edge-rtac url + api key: per-library setting, then env fallback."""
    return (
        settings.get("edge_rtac_url") or EDGE_RTAC_URL,
        settings.get("edge_rtac_api_key") or EDGE_RTAC_API_KEY,
    )


def _edge_rtac_request(edge_url, instance_id, params, headers):
    """GET edge-rtac's getInstanceRtac and return the parsed JSON.

    A separate seam so tests can stub the HTTP call. Raises on a non-2xx
    response (httpx.HTTPStatusError); a 401/403 is treated as an auth error
    upstream so the cached client is refreshed and the call retried once.
    """
    url = edge_url.rstrip("/") + "/rtac/" + instance_id
    response = httpx.get(url, params=params, headers=headers, timeout=FOLIO_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _edge_rtac_holdings(settings, instance_id):
    """edge-rtac getInstanceRtac via the edge service, authenticated by apiKey.

    The apiKey already encodes tenant + user, so no okapi headers are needed.
    """
    edge_url, api_key = _edge_rtac_config(settings)
    if not edge_url or not api_key:
        raise RuntimeError(
            "edge backend needs edge_rtac_url and edge_rtac_api_key "
            "(per-library settings or EDGE_RTAC_URL/EDGE_RTAC_API_KEY env)."
        )
    params = {
        "fullPeriodicals": "true" if settings.get("full_periodicals") else "false"
    }
    lang = settings.get("lang")
    if lang:
        params["lang"] = lang
    headers = {"Authorization": api_key, "Accept": "application/json"}
    return _edge_rtac_request(edge_url, instance_id, params, headers)


# A throwaway instance id used only to exercise the edge-rtac connection from the
# validate endpoint. edge-rtac answers getInstanceRtac for an unknown instance
# with 200 + empty holdings, so a good url/apiKey returns cleanly while a bad
# apiKey yields 401/403 and a wrong/unreachable url a transport error.
EDGE_VALIDATE_INSTANCE_ID = "00000000-0000-0000-0000-000000000000"


def _probe_edge_connection(settings):
    """Confirm edge-rtac is reachable and the apiKey is accepted.

    Runs a real getInstanceRtac for a throwaway instance id over the same path a
    live request uses (sharing the url/apiKey/header handling), so a bad edge
    url/apiKey is caught at validate time rather than silently yielding empty
    holdings. Returns None on success; propagates the auth/transport error on
    failure.
    """
    _edge_rtac_holdings(settings, EDGE_VALIDATE_INSTANCE_ID)


def create_rtac_response(folio_client:FolioClient, settings, query,
                         sigel=metrics.UNKNOWN_SIGEL):
    """Return the holdings for the first matching instance.

    The instance is resolved with FolioClient (folio_get_single_object, which
    unlike folio_get does not treat an empty result list as an error, so "no
    matching instance" yields an empty list). Holdings are then fetched from one
    of three backends, chosen per library via the ``rtac_backend`` setting:

    * ``"rtac-cache"`` — mod-rtac-cache ``GET /rtac-cache/{id}`` via the gateway
      (okapi token, no apiKey). Rich response incl. ``permanentLoanType``.
    * ``"edge"`` — edge-rtac ``getInstanceRtac`` via the edge service,
      authenticated with the per-library ``edge_rtac_api_key`` (an okapi token
      alone returns empty holdings there). Also rich; ``full_periodicals``/
      ``lang`` map to its query parameters.
    * ``"rtac"`` (default) — mod-rtac ``GET /rtac/{id}`` via the gateway. Always
      available but deprecated and lean (no loan type).

    All three return a ``{"holdings": [...]}`` envelope.

    ``sigel`` is only a metrics label. The caller vouches for it being a
    configured sigel (see the label doctrine in metrics.py); callers that
    cannot — like tests calling this directly — get the UNKNOWN_SIGEL default.
    """
    full_path = "/instance-storage/instances" + query
    with metrics.measured_upstream(sigel, "folio_instance_search"):
        search = folio_client.folio_get_single_object(full_path)
    instances = search.get("instances", [])
    if not instances:
        logger.debug("No instance found for query %r", query)
        return []
    instance_id = instances[0]["id"]
    backend = (settings.get("rtac_backend") or DEFAULT_RTAC_BACKEND).strip().lower()
    if backend == "rtac-cache":
        with metrics.measured_upstream(sigel, "folio_rtac_cache"):
            rtac = folio_client.folio_get_single_object("/rtac-cache/{}".format(instance_id))
    elif backend == "edge":
        with metrics.measured_upstream(sigel, "edge_rtac"):
            rtac = _edge_rtac_holdings(settings, instance_id)
    elif backend == "rtac":
        with metrics.measured_upstream(sigel, "folio_rtac"):
            rtac = folio_client.folio_get_single_object("/rtac/{}".format(instance_id))
    else:
        raise RuntimeError(
            "Unknown rtac_backend {!r}; expected one of {}".format(
                backend, ", ".join(RTAC_BACKENDS)
            )
        )
    holdings = rtac.get("holdings", [])
    if not holdings:
        logger.debug("No holdings found for instance %r (query %r)", instance_id, query)
    return holdings

def _is_auth_error(exc):
    """True if the exception is a FOLIO 401/403 (e.g. an expired token)."""
    if isinstance(exc, (FolioAuthenticationError, FolioPermissionError)):
        return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) in (401, 403)


def fetch_holdings(sigel, settings, query):
    """Run the FOLIO lookup, refreshing the cached client once on an auth error.

    A cached client whose token has expired yields 401/403; drop it and retry
    with a fresh login so a stale token does not wedge a sigel until the TTL.
    """
    client = get_folio_client(sigel, settings)
    try:
        return create_rtac_response(client, settings, query, sigel=sigel)
    except Exception as exc:
        if not _is_auth_error(exc):
            raise
        logger.warning(
            "Auth error for %s; refreshing FOLIO client and retrying", sigel
        )
        metrics.record_auth_retry(sigel)
        _invalidate_client(sigel)
        client = get_folio_client(sigel, settings)
        return create_rtac_response(client, settings, query, sigel=sigel)


# Libris documentation for the loan-status ("lånestatus") gateway this service
# implements. The page hosts the technical PDF and explains how a library
# registers its status-gateway URL in Biblioteksdatabasen.
LIBRIS_DOCS_URL = (
    "https://www.kb.se/samverkan-och-utveckling/libris/librissamarbetet/"
    "librissystemen/om-biblioteksdatabasen.html"
)

# The National Library's technical specification of Libris lånestatus (PDF).
LIBRIS_LANESTATUS_PDF_URL = (
    "https://www.kb.se/download/18.53200c4319739465c5d2e7/1749808483695/"
    "Libris%20l%C3%A5nestatus%202025.pdf"
)

# A ready-to-use, hosted instance of this service that libraries can try out and
# have operated for them.
HOSTED_SERVICE_URL = "https://rtac.bibliotekarien.se"

# Source code, for libraries that want to run the service themselves.
SOURCE_CODE_URL = "https://github.com/fontanka16/folio_libris_rtac"

_INDEX_PAGE = """<!DOCTYPE html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FOLIO Libris RTAC – lånestatus-gateway</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: system-ui, -apple-system, sans-serif; line-height: 1.6;
           max-width: 46rem; margin: 2rem auto; padding: 0 1rem; }
    h1 { margin-bottom: 0.2rem; }
    .lead { color: #666; font-size: 1.15rem; margin-top: 0; }
    h2 { margin-top: 2rem; }
    code { background: rgba(127,127,127,0.18); padding: 0.1em 0.35em;
           border-radius: 3px; font-size: 0.95em; }
    ol li, ul li { margin-bottom: 0.4rem; }
    a { color: #0b69c7; }
    footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #ccc;
             color: #888; font-size: 0.85rem; }
  </style>
</head>
<body>
  <h1>FOLIO Libris RTAC</h1>
  <p class="lead">En lånestatus-gateway som visar bibliotekets bestånd i Libris,
     med FOLIO som källa.</p>

  <h2>Vad tjänsten gör</h2>
  <p>
    Tjänsten besvarar Libris förfrågningar om <strong>lånestatus</strong> &ndash;
    den realtidskoll som i Libris webbsök visar om ett exemplar är inne eller
    utlånat. När någon tittar på en titel i Libris anropas Libris status-gateway
    (<code>status.libris.kb.se</code>), som i sin tur skickar förfrågan vidare
    till bibliotekets registrerade status-URL: den här tjänsten.
  </p>
  <p>
    Förfrågan bär en identifierare (<code>Bib_ID</code>, <code>ONR</code>,
    <code>ISSN</code> eller <code>ISBN</code>). Tjänsten slår upp titeln i
    bibliotekets <strong>FOLIO</strong> och svarar med bestånd &ndash; placering,
    hyllsignum, lånestatus och eventuellt återlämningsdatum &ndash; som det
    XML-svar Libris förväntar sig. Hittas ingen träff returneras en
    <code>Okänd</code>-platshållare.
  </p>
  <p>
    Mer om lånestatus och Biblioteksdatabasen finns i
    <a href="__DOCS_URL__">Kungliga bibliotekets dokumentation</a>, och den
    tekniska specifikationen i
    <a href="__LANESTATUS_PDF_URL__">Libris lånestatus 2025 (PDF)</a>.
  </p>
  <p>
    Bakom kulisserna slår tjänsten upp instansen i FOLIO
    (<code>instance-storage</code>) och hämtar sedan beståndet via det FOLIO-API
    biblioteket valt (<code>rtac_backend</code>): <strong>mod-rtac-cache</strong>
    eller <strong>edge-rtac</strong> (båda ger lånestatus/loan type), eller
    <strong>mod-rtac</strong> via gatewayen (alltid tillgänglig, men utan loan
    type). För edge-rtac styr biblioteket även <code>fullPeriodicals</code> och
    <code>lang</code>.
  </p>

  <h2>Så ansluter ert bibliotek</h2>
  <p>Varje bibliotek identifieras av sitt <em>sigel</em>. För att bli uppsatt:</p>
  <ol>
    <li>Ha ett FOLIO-tenant som tjänsten kan nå.</li>
    <li>Skapa en FOLIO-användare med <strong>enbart läsbehörigheter</strong>.
        Användaren behöver följande behörigheter:
        <ul>
          <li><code>inventory-storage.instances.collection.get</code> &ndash; läs instanser (samling)</li>
          <li><code>inventory-storage.instances.item.get</code> &ndash; läs enskild instans</li>
          <li><code>inventory-storage.holdings.collection.get</code> &ndash; läs beståndsposter (samling)</li>
          <li><code>inventory-storage.holdings.item.get</code> &ndash; läs enskild beståndspost</li>
          <li><code>inventory-storage.items.collection.get</code> &ndash; läs exemplar (samling)</li>
          <li><code>inventory-storage.items.item.get</code> &ndash; läs enskilt exemplar</li>
          <li><code>inventory-storage.loan-types.collection.get</code> &ndash; läs lånetyper (samling)</li>
          <li><code>inventory-storage.loan-types.item.get</code> &ndash; läs enskild lånetyp</li>
          <li><code>inventory-storage.locations.collection.get</code> &ndash; läs placeringar (samling)</li>
          <li><code>inventory-storage.locations.item.get</code> &ndash; läs enskild placering</li>
          <li><code>rtac.holdings.item.get</code> &ndash; äldre RTAC (mod-rtac): läs enskild beståndspost</li>
        </ul>
        Hela listan finns i <code>libraries/folio-permissions.json</code> i källkoden.</li>
    <li>Ta reda på vilka FOLIO <em>identifier-type</em>-UUID:n som motsvarar de
        identifierare ni vill kunna söka på (<code>Bib_ID</code>,
        <code>ONR</code>, <code>ISSN</code>, <code>ISBN</code>).</li>
    <li>Skicka ert <strong>sigel</strong>, FOLIO-uppgifter
        (<code>okapi_url</code>, <code>tenant_id</code>, användarnamn och
        lösenord) samt UUID:na ovan till tjänstens förvaltare.</li>
    <li>Förvaltaren lägger upp er. Anslutningen kan därefter verifieras på
        <code>/&lt;sigel&gt;/validate-folio-connection</code>.</li>
    <li>Registrera tjänstens status-URL för ert sigel i
        <strong>Biblioteksdatabasen</strong> hos KB, så att Libris anropar den.</li>
  </ol>
  <p>
    När allt är på plats nås beståndskollen på
    <code>/&lt;sigel&gt;/rtac?Bib_ID=&lt;libris-id&gt;</code>
    (även <code>ONR</code>, <code>ISSN</code> och <code>ISBN</code> fungerar).
  </p>

  <h2>API-dokumentation</h2>
  <p>
    Interaktiv API-dokumentation finns på <a href="/docs">/docs</a> (Swagger UI)
    och <a href="/redoc">/redoc</a> (ReDoc). Det maskinläsbara OpenAPI-schemat
    ligger på <a href="/openapi.json">/openapi.json</a>.
  </p>

__LIBRARIES__
  <h2>Testa eller få tjänsten driftad</h2>
  <p>
    Vill ni prova tjänsten eller slippa drifta den själva? En färdig, driftad
    instans finns på <a href="__HOSTED_URL__">__HOSTED_URL__</a> &ndash; testa
    den där, eller hör av er för att få ert bibliotek uppsatt och driftat.
    Vill ni drifta tjänsten själva hämtar ni källkoden på
    <a href="__SOURCE_URL__">github.com/fontanka16/folio_libris_rtac</a>.
  </p>
  <footer>FOLIO Libris RTAC</footer>
</body>
</html>
"""


def index_html(sigels):
    """Render the public landing page describing the service and onboarding.

    `sigels` (trusted, from the filesystem) are still HTML-escaped on principle
    before being interpolated into the page.
    """
    if sigels:
        items = "\n".join(
            '      <li><code>{0}</code> &ndash; '
            '<a href="/{1}/validate-folio-connection">kontrollera '
            "FOLIO-anslutning</a></li>".format(html.escape(s), html.escape(s, quote=True))
            for s in sigels
        )
        libraries = (
            "  <h2>Anslutna bibliotek</h2>\n  <ul>\n" + items + "\n  </ul>\n"
        )
    else:
        libraries = (
            "  <h2>Anslutna bibliotek</h2>\n"
            "  <p>Inga bibliotek är konfigurerade ännu.</p>\n"
        )
    return (
        _INDEX_PAGE.replace("__DOCS_URL__", LIBRIS_DOCS_URL)
        .replace("__LANESTATUS_PDF_URL__", LIBRIS_LANESTATUS_PDF_URL)
        .replace("__HOSTED_URL__", HOSTED_SERVICE_URL)
        .replace("__SOURCE_URL__", SOURCE_CODE_URL)
        .replace("__LIBRARIES__", libraries)
    )


@application.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index():
    return HTMLResponse(index_html(available_sigels()))


@application.get("/{sigel}/rtac")
@limiter.limit(lambda: RTAC_RATE_LIMIT, cost=_rtac_cost)
def rtac(
    request: Request,
    sigel: str,
    Bib_ID: str = Query(None),
    ONR: str = Query(None),
    ISSN: str = Query(None),
    ISBN: str = Query(None),
    token: str = Query(
        None,
        description="Per-sigel fast-track token; a valid value exempts the "
        "request from rate limiting.",
    ),
):
    # The finally block records the request into the metrics exactly once,
    # whichever way it ends. Exceptions still propagate to handle_error, which
    # serves the placeholder XML — outcome classification does not change the
    # response. The sigel label starts as the placeholder and is upgraded only
    # once load_settings has vouched for it: the path segment is
    # client-controlled, and unchecked label values would let anyone mint new
    # time series (see the label doctrine in metrics.py).
    started = time.perf_counter()
    label_sigel = metrics.UNKNOWN_SIGEL
    channel = "public"
    outcome = "error"
    try:
        settings = load_settings(sigel)
        label_sigel = sigel
        channel = "fast_track" if _is_fast_track(request) else "public"
        query = build_identifier_query(
            {"Bib_ID": Bib_ID, "ONR": ONR, "ISSN": ISSN, "ISBN": ISBN},
            settings.get("identifier_type_ids", {}),
        )
        if query is None:
            outcome = "no_identifier"
            raise ValueError(
                "No searchable identifier provided (a value with a configured UUID)."
            )

        holdings = fetch_holdings(sigel, settings, query)
        outcome = "holdings" if holdings else "empty"
        if not holdings:
            root = empty_item_information()
        else:
            root = etree.Element("Item_Information")
            for holding in holdings:
                append_item(root, holding_values(holding))
        return Response(etree.tostring(root), media_type="text/xml")
    finally:
        metrics.record_request(
            label_sigel, channel, outcome, time.perf_counter() - started
        )


@application.get("/{sigel}/validate-folio-connection")
def validate_folio_connection(sigel: str):
    if sigel not in available_sigels():
        return JSONResponse(
            {"status": "error", "detail": "Unknown library sigel: {}".format(sigel)},
            status_code=404,
        )
    settings = load_settings(sigel)
    required = ["okapi_url", "tenant_id", "username", "password"]
    missing = [key for key in required if not settings.get(key)]
    if missing:
        return JSONResponse(
            {"status": "error", "detail": "Missing settings: " + ", ".join(missing)},
            status_code=503,
        )
    backend = (settings.get("rtac_backend") or DEFAULT_RTAC_BACKEND).strip().lower()
    # The edge backend resolves the instance via FOLIO (below) but fetches
    # holdings from edge-rtac with its own url + apiKey, so those must be present
    # too — treat them like missing FOLIO settings (config incomplete -> 503).
    if backend == "edge":
        edge_url, api_key = _edge_rtac_config(settings)
        edge_missing = [
            name
            for name, value in (
                ("edge_rtac_url", edge_url),
                ("edge_rtac_api_key", api_key),
            )
            if not value
        ]
        if edge_missing:
            return JSONResponse(
                {
                    "status": "error",
                    "sigel": sigel,
                    "backend": backend,
                    "detail": "Missing edge settings: " + ", ".join(edge_missing),
                },
                status_code=503,
            )
    try:
        folio_client = _new_folio_client(settings)
    except Exception as e:
        logger.error(
            "FOLIO connection validation failed for %s: %s", sigel, e, exc_info=e
        )
        return JSONResponse(
            {"status": "error", "detail": "Could not connect to FOLIO: {}".format(e)},
            status_code=502,
        )
    # This client is one-shot (not cached), so close its connection pool once
    # we've confirmed the connection — otherwise every validate call leaks a pool.
    # We deliberately don't echo back okapi_url/tenant to avoid disclosing
    # server details; "ok" is enough to confirm the connection works.
    try:
        # Every backend starts by resolving the instance via FOLIO, so confirm
        # that API actually answers (not just that login succeeded) before
        # reporting ok — that first hop is shared by all modes, edge included.
        try:
            _probe_folio_connection(folio_client)
        except Exception as e:
            logger.error(
                "FOLIO instance-storage validation failed for %s: %s",
                sigel,
                e,
                exc_info=e,
            )
            return JSONResponse(
                {
                    "status": "error",
                    "sigel": sigel,
                    "backend": backend,
                    "detail": "Could not query FOLIO instance-storage: {}".format(e),
                },
                status_code=502,
            )
        result = {
            "status": "ok",
            "sigel": sigel,
            "backend": backend,
            "folio": {"status": "ok"},
        }
        # When the library serves holdings from edge-rtac, validate that second
        # API too and report its status alongside the FOLIO one.
        if backend == "edge":
            try:
                _probe_edge_connection(settings)
            except Exception as e:
                logger.error(
                    "edge-rtac connection validation failed for %s: %s",
                    sigel,
                    e,
                    exc_info=e,
                )
                return JSONResponse(
                    {
                        "status": "error",
                        "sigel": sigel,
                        "backend": backend,
                        "detail": "Could not connect to edge-rtac: {}".format(e),
                    },
                    status_code=502,
                )
            result["edge"] = {"status": "ok"}
        return result
    finally:
        _close_client(folio_client)


@application.exception_handler(RateLimitExceeded)
def handle_rate_limit(request: Request, exc: RateLimitExceeded):
    """A throttled rtac request still gets a valid (empty) RTAC document."""
    logger.warning(
        "Rate limit hit for %s from %s", request.url.path, get_remote_address(request)
    )
    if request.url.path.endswith("/rtac"):
        # Throttled requests never reach the endpoint, so they are recorded
        # here. Channel is "public" by definition: a valid fast-track token
        # costs 0 and is never throttled. The sigel is sanitized against the
        # configured set before it may become a label value (doctrine in
        # metrics.py); no duration — nothing was looked up.
        sigel = request.path_params.get("sigel")
        if sigel not in available_sigels():
            sigel = metrics.UNKNOWN_SIGEL
        metrics.record_request(sigel, "public", "rate_limited")
        return Response(
            etree.tostring(empty_item_information()), media_type="text/xml"
        )
    return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)


@application.exception_handler(Exception)
def handle_error(request: Request, e: Exception):
    logger.error("Error while serving %s: %s", request.url, e, exc_info=e)
    if request.url.path.endswith("/rtac"):
        return Response(
            etree.tostring(empty_item_information()), media_type="text/xml"
        )
    # Don't echo the exception text to the client — it can leak internal paths,
    # FOLIO URLs, or stack details. The full error is logged above.
    return JSONResponse({"error": "Internal server error"}, status_code=500)


# Opt-in: serves /metrics on its own port only when METRICS_PORT is set (see
# metrics.py). On import so it works under both uvicorn and __main__; assumes
# a single process — with multiple uvicorn workers each would need its own
# port (prometheus_client's multiprocess mode is the proper fix there).
metrics.start_exporter_if_configured()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("application:application", host="0.0.0.0", port=5000)
