"""Tests for the Prometheus metrics.

Covers the four things that make the metrics trustworthy:

* outcome classification — every way a lookup can end maps to exactly one
  ``outcome`` label value, without changing the response;
* label hygiene — a client-controlled path segment can never become a label
  value (only configured sigels may; everything else lands in ``_unknown``);
* upstream status mapping — exceptions from FOLIO/edge calls map to the fixed
  ``status`` vocabulary, and are re-raised untouched;
* the opt-in exporter — nothing listens unless METRICS_PORT is set, and a bad
  value or bind failure is logged, never fatal.

The prometheus_client registry is process-global, so every assertion reads a
before-value and checks the delta rather than an absolute count.
"""

import logging
import types

import httpx
import pytest
from prometheus_client import REGISTRY

import application
import metrics

from conftest import FakeFolioClient


def _sample(name, **labels):
    """Current value of a sample in the global registry (0.0 when unset)."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _fake_with_holdings():
    return FakeFolioClient(
        responses={
            "/instance-storage/instances": {"instances": [{"id": "i1"}]},
            "/rtac/": {"holdings": [{"id": "h1", "status": "Available"}]},
        }
    )


# --- inbound: outcome classification ----------------------------------------


def test_lookup_with_holdings_counts_holdings_outcome(
    client, libraries_dir, settings, monkeypatch
):
    libraries_dir("alpha", settings)
    monkeypatch.setattr(
        application, "get_folio_client", lambda sigel, s: _fake_with_holdings()
    )
    before = _sample(
        "rtac_requests_total", sigel="alpha", channel="public", outcome="holdings"
    )
    timed_before = _sample("rtac_request_seconds_count", sigel="alpha")

    client.get("/alpha/rtac", params={"Bib_ID": "1"})

    assert _sample(
        "rtac_requests_total", sigel="alpha", channel="public", outcome="holdings"
    ) == before + 1
    # Served lookups are also timed.
    assert _sample("rtac_request_seconds_count", sigel="alpha") == timed_before + 1


def test_lookup_without_match_counts_empty_outcome(
    client, libraries_dir, settings, monkeypatch
):
    libraries_dir("alpha", settings)
    fake = FakeFolioClient(
        responses={"/instance-storage/instances": {"instances": []}}
    )
    monkeypatch.setattr(application, "get_folio_client", lambda sigel, s: fake)
    before = _sample(
        "rtac_requests_total", sigel="alpha", channel="public", outcome="empty"
    )

    client.get("/alpha/rtac", params={"Bib_ID": "1"})

    assert _sample(
        "rtac_requests_total", sigel="alpha", channel="public", outcome="empty"
    ) == before + 1


def test_folio_error_counts_error_outcome(client, libraries_dir, settings, monkeypatch):
    # The client still gets the 200 placeholder XML; only the metric may tell
    # an operator FOLIO is failing. That is the whole point of the counter.
    libraries_dir("alpha", settings)
    fake = FakeFolioClient(error=RuntimeError("FOLIO down"))
    monkeypatch.setattr(application, "get_folio_client", lambda sigel, s: fake)
    before = _sample(
        "rtac_requests_total", sigel="alpha", channel="public", outcome="error"
    )

    resp = client.get("/alpha/rtac", params={"Bib_ID": "1"})

    assert resp.status_code == 200  # response unchanged by instrumentation
    assert _sample(
        "rtac_requests_total", sigel="alpha", channel="public", outcome="error"
    ) == before + 1


def test_request_without_identifier_counts_no_identifier(
    client, libraries_dir, settings
):
    libraries_dir("alpha", settings)
    before = _sample(
        "rtac_requests_total", sigel="alpha", channel="public", outcome="no_identifier"
    )

    client.get("/alpha/rtac")

    assert _sample(
        "rtac_requests_total", sigel="alpha", channel="public", outcome="no_identifier"
    ) == before + 1


def test_fast_track_token_counts_fast_track_channel(
    client, libraries_dir, settings, monkeypatch
):
    libraries_dir("alpha", dict(settings, fast_track_token="tok-xyz"))
    monkeypatch.setattr(
        application, "get_folio_client", lambda sigel, s: _fake_with_holdings()
    )
    before = _sample(
        "rtac_requests_total", sigel="alpha", channel="fast_track", outcome="holdings"
    )

    client.get("/alpha/rtac", params={"Bib_ID": "1", "token": "tok-xyz"})

    assert _sample(
        "rtac_requests_total", sigel="alpha", channel="fast_track", outcome="holdings"
    ) == before + 1


# --- label hygiene -----------------------------------------------------------


def test_unknown_sigel_never_becomes_a_label(client, libraries_dir, settings):
    """A client-controlled path segment must not mint new time series."""
    libraries_dir("alpha", settings)
    before = _sample(
        "rtac_requests_total",
        sigel=metrics.UNKNOWN_SIGEL, channel="public", outcome="error",
    )

    client.get("/ghost/rtac", params={"Bib_ID": "1"})

    assert _sample(
        "rtac_requests_total",
        sigel=metrics.UNKNOWN_SIGEL, channel="public", outcome="error",
    ) == before + 1
    # No series was created for the attacker-chosen value itself.
    assert REGISTRY.get_sample_value(
        "rtac_requests_total",
        {"sigel": "ghost", "channel": "public", "outcome": "error"},
    ) is None


# --- rate limiting -----------------------------------------------------------


@pytest.fixture
def limited(monkeypatch):
    """Enable the limiter with a tiny limit and a clean store for one test."""
    monkeypatch.setattr(application, "RTAC_RATE_LIMIT", "1/minute")
    application.limiter.enabled = True
    application.limiter._storage.reset()
    yield
    application.limiter.enabled = False
    application.limiter._storage.reset()


def test_throttled_request_counts_rate_limited_without_duration(
    client, libraries_dir, settings, monkeypatch, limited
):
    libraries_dir("alpha", settings)
    monkeypatch.setattr(
        application, "get_folio_client", lambda sigel, s: _fake_with_holdings()
    )
    client.get("/alpha/rtac", params={"Bib_ID": "1"})  # consumes the 1/minute
    before = _sample(
        "rtac_requests_total", sigel="alpha", channel="public", outcome="rate_limited"
    )
    timed_before = _sample("rtac_request_seconds_count", sigel="alpha")

    client.get("/alpha/rtac", params={"Bib_ID": "1"})  # throttled

    assert _sample(
        "rtac_requests_total", sigel="alpha", channel="public", outcome="rate_limited"
    ) == before + 1
    # Nothing was looked up, so nothing was timed.
    assert _sample("rtac_request_seconds_count", sigel="alpha") == timed_before


# --- outbound: per-target upstream series ------------------------------------


def test_live_lookup_records_upstream_series_per_target():
    fake = _fake_with_holdings()
    search_before = _sample(
        "rtac_upstream_requests_total",
        sigel="alpha", target="folio_instance_search", status="200",
    )
    rtac_before = _sample(
        "rtac_upstream_requests_total",
        sigel="alpha", target="folio_rtac", status="200",
    )

    application.create_rtac_response(fake, {}, "?query=(x)", sigel="alpha")

    assert _sample(
        "rtac_upstream_requests_total",
        sigel="alpha", target="folio_instance_search", status="200",
    ) == search_before + 1
    assert _sample(
        "rtac_upstream_requests_total",
        sigel="alpha", target="folio_rtac", status="200",
    ) == rtac_before + 1


def test_create_rtac_response_defaults_to_placeholder_sigel():
    """Callers that don't vouch for a sigel (tests, tools) get _unknown."""
    fake = _fake_with_holdings()
    before = _sample(
        "rtac_upstream_requests_total",
        sigel=metrics.UNKNOWN_SIGEL, target="folio_instance_search", status="200",
    )

    application.create_rtac_response(fake, {}, "?query=(x)")

    assert _sample(
        "rtac_upstream_requests_total",
        sigel=metrics.UNKNOWN_SIGEL, target="folio_instance_search", status="200",
    ) == before + 1


# --- measured_upstream: status vocabulary ------------------------------------


def _http_status_error(code):
    request = httpx.Request("GET", "https://folio.example")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(code, request=request)
    )


def _response_attr_error(code):
    """An opaque exception carrying an httpx-style response attribute — the
    shape of folioclient's own exception types, same as _is_auth_error sees."""
    exc = Exception("auth")
    exc.response = types.SimpleNamespace(status_code=code)
    return exc


@pytest.mark.parametrize(
    "exc, expected_status",
    [
        (httpx.TimeoutException("slow"), "timeout"),
        (httpx.ConnectError("refused"), "connect_error"),
        (httpx.TransportError("broken pipe"), "transport_error"),
        (_http_status_error(502), "502"),
        (_response_attr_error(401), "401"),
        (RuntimeError("no status anywhere"), "error"),
    ],
    ids=["timeout", "connect", "transport", "http_502", "response_attr", "opaque"],
)
def test_measured_upstream_maps_exception_to_status(exc, expected_status):
    before = _sample(
        "rtac_upstream_requests_total",
        sigel="alpha", target="folio_rtac", status=expected_status,
    )

    with pytest.raises(Exception) as raised:
        with metrics.measured_upstream("alpha", "folio_rtac"):
            raise exc
    assert raised.value is exc  # re-raised untouched

    assert _sample(
        "rtac_upstream_requests_total",
        sigel="alpha", target="folio_rtac", status=expected_status,
    ) == before + 1


def test_measured_upstream_success_is_200_and_timed():
    count_before = _sample(
        "rtac_upstream_requests_total", sigel="alpha", target="edge_rtac", status="200"
    )
    timed_before = _sample("rtac_upstream_request_seconds_count", target="edge_rtac")

    with metrics.measured_upstream("alpha", "edge_rtac"):
        pass

    assert _sample(
        "rtac_upstream_requests_total", sigel="alpha", target="edge_rtac", status="200"
    ) == count_before + 1
    assert (
        _sample("rtac_upstream_request_seconds_count", target="edge_rtac")
        == timed_before + 1
    )


# --- auth retry counter ------------------------------------------------------


class _AuthError(Exception):
    """Mimics an HTTP error carrying a 401 response."""

    def __init__(self):
        super().__init__("auth")
        self.response = types.SimpleNamespace(status_code=401)


def test_auth_retry_is_counted(monkeypatch):
    stale = FakeFolioClient(error=_AuthError())
    fresh = _fake_with_holdings()
    clients = iter([stale, fresh])
    monkeypatch.setattr(
        application, "get_folio_client", lambda sigel, s: next(clients)
    )
    monkeypatch.setattr(application, "_invalidate_client", lambda sigel: None)
    before = _sample("rtac_folio_auth_retries_total", sigel="alpha")

    application.fetch_holdings("alpha", {}, "?query=(x)")

    assert _sample("rtac_folio_auth_retries_total", sigel="alpha") == before + 1


# --- the opt-in exporter -----------------------------------------------------


def test_exporter_not_started_when_unconfigured(monkeypatch):
    monkeypatch.delenv("METRICS_PORT", raising=False)
    started = []
    monkeypatch.setattr(
        metrics, "start_http_server", lambda port, addr: started.append((port, addr))
    )
    assert metrics.start_exporter_if_configured() is None
    assert started == []


def test_exporter_starts_on_configured_port(monkeypatch):
    monkeypatch.setenv("METRICS_PORT", "9105")
    monkeypatch.delenv("METRICS_HOST", raising=False)
    started = []
    monkeypatch.setattr(
        metrics, "start_http_server", lambda port, addr: started.append((port, addr))
    )
    assert metrics.start_exporter_if_configured() == 9105
    assert started == [(9105, "0.0.0.0")]


def test_exporter_honours_metrics_host(monkeypatch):
    monkeypatch.setenv("METRICS_PORT", "9105")
    monkeypatch.setenv("METRICS_HOST", "10.0.0.4")
    started = []
    monkeypatch.setattr(
        metrics, "start_http_server", lambda port, addr: started.append((port, addr))
    )
    metrics.start_exporter_if_configured()
    assert started == [(9105, "10.0.0.4")]


@pytest.mark.parametrize("bad", ["abc", "9105.5", "0", "-1", "70000"])
def test_exporter_rejects_bad_port_with_warning(monkeypatch, caplog, bad):
    monkeypatch.setenv("METRICS_PORT", bad)
    started = []
    monkeypatch.setattr(
        metrics, "start_http_server", lambda port, addr: started.append(port)
    )
    assert metrics.start_exporter_if_configured() is None
    assert started == []
    assert "METRICS_PORT" in caplog.text


def test_exporter_bind_failure_is_logged_not_fatal(monkeypatch, caplog):
    # Answering Libris matters more than the exporter: a monitoring problem
    # must never take the service down.
    monkeypatch.setenv("METRICS_PORT", "9105")

    def boom(port, addr):
        raise OSError("address already in use")

    monkeypatch.setattr(metrics, "start_http_server", boom)
    with caplog.at_level(logging.ERROR):
        assert metrics.start_exporter_if_configured() is None
    assert "metrics exporter" in caplog.text
