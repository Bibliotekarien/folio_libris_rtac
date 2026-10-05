"""Tests for the opt-in JSON logging.

Covers the three things a log pipeline that routes lines by content relies on:

* one record, one line — whatever a message or traceback contains;
* sigel hygiene — a client-controlled path segment can never become the
  ``sigel`` of a line (only configured sigels may; everything else has none);
* opt-in — nothing about logging changes unless LOG_FORMAT=json is set.
"""

import io
import json
import logging

import pytest

import application
import jsonlog

from conftest import FakeFolioClient


@pytest.fixture
def json_lines():
    """Capture what the JSON formatter writes for the app's loggers.

    A real handler on the "rtac" logger rather than caplog: the formatter reads
    the current sigel when the record is emitted, in the thread that logged it,
    and that is the behaviour under test.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(jsonlog.JsonFormatter())
    rtac_logger = logging.getLogger("rtac")
    previous_level = rtac_logger.level
    rtac_logger.addHandler(handler)
    rtac_logger.setLevel(logging.DEBUG)

    def read():
        return [json.loads(line) for line in stream.getvalue().splitlines()]

    yield read
    rtac_logger.removeHandler(handler)
    rtac_logger.setLevel(previous_level)


def _lookup_lines(lines):
    return [line for line in lines if line["logger"] == "rtac.request"]


def _fake_with_holdings():
    return FakeFolioClient(
        responses={
            "/instance-storage/instances": {"instances": [{"id": "i1"}]},
            "/rtac/": {"holdings": [{"id": "h1", "status": "Available"}]},
        }
    )


# --- one record, one line ----------------------------------------------------


def test_message_with_newlines_stays_on_one_line():
    record = logging.LogRecord(
        "rtac", logging.WARNING, __file__, 1,
        'first\n{"sigel": "victim", "msg": "forged"}', None, None,
    )
    out = jsonlog.JsonFormatter().format(record)

    assert "\n" not in out
    assert json.loads(out)["msg"].startswith("first\n")
    # The forged object is inside the msg string, not a field of the line.
    assert "sigel" not in json.loads(out)


def test_traceback_stays_on_one_line():
    try:
        raise RuntimeError('upstream said:\n{"sigel": "victim"}')
    except RuntimeError as exc:
        record = logging.LogRecord(
            "rtac", logging.ERROR, __file__, 1, "failed", None,
            (type(exc), exc, exc.__traceback__),
        )
    out = jsonlog.JsonFormatter().format(record)

    assert "\n" not in out
    parsed = json.loads(out)
    assert "RuntimeError" in parsed["exc"]
    assert "sigel" not in parsed


def test_unserialisable_extra_does_not_lose_the_line():
    record = logging.LogRecord("rtac", logging.INFO, __file__, 1, "m", None, None)
    record.identifiers = {"ISBN": object()}

    assert json.loads(jsonlog.JsonFormatter().format(record))["msg"] == "m"


def test_unknown_extra_fields_are_not_emitted():
    record = logging.LogRecord("rtac", logging.INFO, __file__, 1, "m", None, None)
    record.password = "hunter2"

    assert "hunter2" not in jsonlog.JsonFormatter().format(record)


# --- sigel tagging -----------------------------------------------------------


def test_sigel_context_tags_lines_and_ends_with_the_block():
    formatter = jsonlog.JsonFormatter()
    record = logging.LogRecord("rtac", logging.INFO, __file__, 1, "m", None, None)

    with jsonlog.sigel_context("alpha"):
        assert json.loads(formatter.format(record))["sigel"] == "alpha"
    assert "sigel" not in json.loads(formatter.format(record))


def test_lookup_logs_one_line_tagged_with_the_sigel(
    client, libraries_dir, settings, monkeypatch, json_lines
):
    libraries_dir("alpha", settings)
    monkeypatch.setattr(
        application, "get_folio_client", lambda sigel, s: _fake_with_holdings()
    )

    client.get("/alpha/rtac", params={"ISBN": "9789100000000"})

    (line,) = _lookup_lines(json_lines())
    assert line["sigel"] == "alpha"
    assert line["outcome"] == "holdings"
    assert line["channel"] == "public"
    assert line["identifiers"] == {"ISBN": "9789100000000"}
    assert isinstance(line["duration_ms"], int)


def test_lookup_for_unknown_sigel_is_never_tagged(client, libraries_dir, json_lines):
    # The path segment is client-controlled: naming a sigel that is not
    # configured must not put that name in the `sigel` field of any line.
    # (libraries_dir points the app at an empty directory: nothing is configured.)
    client.get("/victim/rtac", params={"Bib_ID": "1"})

    lines = json_lines()
    assert _lookup_lines(lines)[0]["outcome"] == "error"
    assert all("sigel" not in line for line in lines)


def test_lines_logged_during_a_lookup_are_tagged(
    client, libraries_dir, settings, monkeypatch, json_lines
):
    # An invalid identifier value is logged several calls below the endpoint;
    # the context carries the sigel down to it.
    libraries_dir("alpha", settings)
    monkeypatch.setattr(
        application, "get_folio_client", lambda sigel, s: _fake_with_holdings()
    )

    client.get("/alpha/rtac", params={"ISBN": 'bad"value', "Bib_ID": "1"})

    ignored = [line for line in json_lines() if "Ignoring invalid" in line["msg"]]
    assert ignored and all(line["sigel"] == "alpha" for line in ignored)


def test_one_lookups_sigel_does_not_leak_into_the_next(
    client, libraries_dir, settings, monkeypatch, json_lines
):
    libraries_dir("alpha", settings)
    monkeypatch.setattr(
        application, "get_folio_client", lambda sigel, s: _fake_with_holdings()
    )

    client.get("/alpha/rtac", params={"Bib_ID": "1"})
    client.get("/victim/rtac", params={"Bib_ID": "1"})

    first, second = _lookup_lines(json_lines())
    assert first["sigel"] == "alpha"
    assert "sigel" not in second


def test_error_line_is_tagged_for_a_configured_sigel(
    client, libraries_dir, settings, monkeypatch, json_lines
):
    libraries_dir("alpha", settings)
    fake = FakeFolioClient(error=RuntimeError("FOLIO is down"))
    monkeypatch.setattr(application, "get_folio_client", lambda sigel, s: fake)

    client.get("/alpha/rtac", params={"Bib_ID": "1"})

    (error,) = [line for line in json_lines() if line["level"] == "ERROR"]
    assert error["sigel"] == "alpha"
    assert "FOLIO is down" in error["exc"]


def test_lookup_line_never_contains_the_fast_track_token(
    client, libraries_dir, settings, monkeypatch, json_lines
):
    libraries_dir("alpha", dict(settings, fast_track_token="s3cret-token"))
    monkeypatch.setattr(
        application, "get_folio_client", lambda sigel, s: _fake_with_holdings()
    )

    client.get("/alpha/rtac", params={"Bib_ID": "1", "token": "s3cret-token"})

    (line,) = _lookup_lines(json_lines())
    assert line["channel"] == "fast_track"
    assert "s3cret-token" not in json.dumps(line)


def test_oversized_identifier_is_cut_in_the_log_line(
    client, libraries_dir, settings, json_lines
):
    libraries_dir("alpha", settings)

    client.get("/alpha/rtac", params={"ISBN": "9" * 5000})

    (line,) = _lookup_lines(json_lines())
    assert len(line["identifiers"]["ISBN"]) == 128


# --- opt-in ------------------------------------------------------------------


@pytest.fixture
def restore_logging():
    """Put the root and uvicorn loggers back after a configure call."""
    names = ("", "rtac", "uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in names
    }
    yield
    for name, (handlers, level, propagate) in saved.items():
        target = logging.getLogger(name)
        target.handlers[:] = handlers
        target.setLevel(level)
        target.propagate = propagate


@pytest.mark.parametrize("value", [None, "", "text"])
def test_logging_is_untouched_unless_json_is_requested(
    monkeypatch, restore_logging, value
):
    if value is None:
        monkeypatch.delenv("LOG_FORMAT", raising=False)
    else:
        monkeypatch.setenv("LOG_FORMAT", value)
    before = list(logging.getLogger().handlers)

    assert jsonlog.configure_if_requested() is False
    assert logging.getLogger().handlers == before


def test_invalid_format_is_ignored_not_fatal(monkeypatch, restore_logging):
    monkeypatch.setenv("LOG_FORMAT", "yaml")
    before = list(logging.getLogger().handlers)

    assert jsonlog.configure_if_requested() is False
    assert logging.getLogger().handlers == before


def test_json_takes_over_root_and_uvicorn_loggers(monkeypatch, restore_logging):
    monkeypatch.setenv("LOG_FORMAT", " JSON ")
    # As uvicorn leaves them: own handlers, not propagating.
    for name in ("uvicorn", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = [logging.NullHandler()]
        logging.getLogger(name).propagate = False

    assert jsonlog.configure_if_requested() is True

    (handler,) = logging.getLogger().handlers
    assert isinstance(handler.formatter, jsonlog.JsonFormatter)
    assert logging.getLogger("rtac").level == logging.INFO
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True
