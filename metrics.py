"""Prometheus metrics: recording is always on, exposure is opt-in.

The counters and histograms below are plain in-process numbers (atomic floats),
so they are recorded unconditionally — the cost is nanoseconds and it keeps the
call sites in application.py branch-free. What is opt-in is the HTTP exposure:
`start_exporter_if_configured()` starts a /metrics server only when the
METRICS_PORT environment variable is set. Operators who do nothing get exactly
the previous behaviour: no new port, no new endpoint.

The exporter is deliberately its own server on its own port, never a route on
the application port: deployments front the app port with a public reverse
proxy, so anything served there is world-readable. Publish the metrics port
only where your Prometheus can reach it (see docker-compose.metrics.yml).

Label doctrine — low cardinality, no personal data
--------------------------------------------------

Every label value must come from configuration or a fixed vocabulary, never
from a request. Concretely:

* ``sigel``   — only *configured* sigels (a bounded, operator-controlled set).
  The path segment is client-controlled, so callers that cannot vouch for it
  must pass UNKNOWN_SIGEL instead: unchecked values would let any client mint
  new time series (cardinality abuse — the metrics twin of the path-traversal
  guard on settings files).
* ``channel`` — "fast_track" or "public".
* ``outcome`` / ``target`` / ``status`` — fixed vocabularies defined here.

Never labels, never values: identifier values (what was searched), client IPs,
tokens, URLs, due dates. That keeps the exporter free of personal data (GDPR)
and the series count bounded.
"""

import logging
import os
import time
from contextlib import contextmanager

import httpx
from prometheus_client import Counter, Histogram, start_http_server

logger = logging.getLogger("rtac.metrics")

# Label value for requests whose sigel is not in the configured set. A single
# shared bucket, so unknown/garbage paths can never create new series.
UNKNOWN_SIGEL = "_unknown"

# Spans the realistic range for this service: a lookup is 1-3 FOLIO round trips
# (up to ~45 s worst case with the auth retry, FOLIO_TIMEOUT being 15 s), while
# a single upstream call is bounded by FOLIO_TIMEOUT itself.
_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

# ── Inbound: the /<sigel>/rtac lookups ───────────────────────────────────────
#
# ``outcome`` says how the request ended, which the HTTP layer cannot: the
# service always answers 200 with valid XML, even on upstream failure (the
# "Okänd" placeholder), so status codes are useless for monitoring.
#
#   holdings      — at least one holding returned
#   empty         — clean lookup, nothing found (placeholder served)
#   no_identifier — no usable identifier in the request (placeholder served)
#   error         — something raised; the handler served the placeholder
#   rate_limited  — throttled before any lookup (placeholder served)
#
# ``channel`` separates Libris's registered status URL (fast_track) from
# everything else (public), so "real" traffic can be graphed on its own.

REQUESTS = Counter(
    "rtac_requests_total",
    "RTAC lookups by library sigel, channel and outcome.",
    labelnames=("sigel", "channel", "outcome"),
)

REQUEST_SECONDS = Histogram(
    "rtac_request_seconds",
    "Duration of RTAC lookups (rate-limited requests are not timed).",
    labelnames=("sigel",),
    buckets=_BUCKETS,
)

# ── Outbound: calls to FOLIO and edge-rtac ───────────────────────────────────
#
# ``target`` is the API being called, a fixed vocabulary:
#
#   folio_login            — FOLIO authentication (FolioClient construction)
#   folio_instance_search  — /instance-storage/instances (every backend's
#                            first hop)
#   folio_rtac             — mod-rtac via the gateway
#   folio_rtac_cache       — mod-rtac-cache via the gateway
#   edge_rtac              — edge-rtac via the edge service
#
# ``status`` is the HTTP status code as text, or a synthetic outcome for calls
# without one: "timeout" / "connect_error" / "transport_error" (no response
# received) or "error" (failed without an HTTP status). Success is "200" —
# both folioclient and our edge call raise on any non-2xx.
#
# Only the live request path records these. The /validate-folio-connection
# endpoint exercises the same upstreams but is itself monitoring (probed
# externally); mixing its calls in would drown the traffic actually served.

UPSTREAM_REQUESTS = Counter(
    "rtac_upstream_requests_total",
    "Outbound FOLIO/edge-rtac calls by sigel, target API and outcome.",
    labelnames=("sigel", "target", "status"),
)

UPSTREAM_SECONDS = Histogram(
    "rtac_upstream_request_seconds",
    "Duration of outbound FOLIO/edge-rtac calls.",
    labelnames=("target",),
    buckets=_BUCKETS,
)

# A retry after a FOLIO 401/403 means a cached token expired mid-TTL. The
# occasional one is normal; a spike means FOLIO is rejecting our sessions.
AUTH_RETRIES = Counter(
    "rtac_folio_auth_retries_total",
    "Lookups retried with a fresh FOLIO login after a 401/403.",
    labelnames=("sigel",),
)


def record_request(sigel, channel, outcome, seconds=None):
    """Record one inbound RTAC lookup.

    ``sigel`` must already be sanitized by the caller (a configured sigel or
    UNKNOWN_SIGEL — see the label doctrine above). ``seconds`` is None for
    requests that were never timed (the rate-limited short-circuit).
    """
    REQUESTS.labels(sigel=sigel, channel=channel, outcome=outcome).inc()
    if seconds is not None:
        REQUEST_SECONDS.labels(sigel=sigel).observe(seconds)


def record_auth_retry(sigel):
    """Record a lookup being retried with a fresh FOLIO login (expired token)."""
    AUTH_RETRIES.labels(sigel=sigel).inc()


@contextmanager
def measured_upstream(sigel, target):
    """Measure one outbound call to ``target``, whatever way it ends.

    Success is recorded as "200" (folioclient and the edge call both raise on
    any non-2xx). Exceptions are recorded and re-raised, mapped to the
    ``status`` vocabulary documented above; the ordering matters because
    TimeoutException and ConnectError are TransportError subclasses. The final
    branch mirrors ``_is_auth_error``'s tolerance: folioclient's own exception
    types carry the httpx response as an attribute, so their status code is
    used when present.
    """
    start = time.perf_counter()
    status = "200"
    try:
        yield
    except httpx.TimeoutException:
        status = "timeout"
        raise
    except httpx.HTTPStatusError as exc:
        status = str(exc.response.status_code)
        raise
    except httpx.ConnectError:
        status = "connect_error"
        raise
    except httpx.TransportError:
        status = "transport_error"
        raise
    except Exception as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        status = str(code) if code is not None else "error"
        raise
    finally:
        seconds = time.perf_counter() - start
        UPSTREAM_REQUESTS.labels(sigel=sigel, target=target, status=status).inc()
        UPSTREAM_SECONDS.labels(target=target).observe(seconds)


def start_exporter_if_configured():
    """Start the /metrics HTTP server when METRICS_PORT is set; else do nothing.

    METRICS_HOST (default 0.0.0.0) sets the bind address — in Docker the
    published port controls exposure, but a bare-metal deployment should bind
    the address its Prometheus scrapes (e.g. a private interface) rather than
    firewalling a wildcard bind.

    A bad value or an unbindable port is logged and swallowed, not raised:
    answering Libris matters more than the exporter, so a monitoring problem
    must never take the service down. Returns the port on success, else None
    (for tests and log-readers; callers don't need it).
    """
    raw = os.environ.get("METRICS_PORT")
    if raw is None or raw.strip() == "":
        return None
    try:
        port = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid METRICS_PORT=%r", raw)
        return None
    if not 0 < port < 65536:
        logger.warning("Ignoring out-of-range METRICS_PORT=%r", raw)
        return None
    host = os.environ.get("METRICS_HOST", "0.0.0.0")
    try:
        start_http_server(port, addr=host)
    except OSError as e:
        logger.error("Could not start the metrics exporter on %s:%s: %s", host, port, e)
        return None
    logger.info("Metrics exporter listening on %s:%s", host, port)
    return port
