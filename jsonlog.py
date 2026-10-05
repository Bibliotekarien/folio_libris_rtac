"""Structured logging: opt-in JSON, one line per record, tagged per library.

Off by default. Unless LOG_FORMAT=json is set, `configure_if_requested()` does
nothing and logging behaves exactly as before (uvicorn's own plain lines, plus
the app's warnings/errors). Operators who don't ship logs anywhere can ignore
this module entirely.

With LOG_FORMAT=json every record — the app's, uvicorn's, and any library's —
is written to stdout as a single JSON object per line. That buys two things a
log pipeline needs:

* **One record, one line.** Messages and tracebacks are JSON-encoded, so
  nothing a request contains (or an upstream response quoted in an exception)
  can start a new line. A shipper that routes lines by their content — e.g.
  "lines for library X go to X's own log store" — depends on that: with plain
  multi-line output, text an outsider controls could pose as a line of its own.

* **A `sigel` field** saying which library a line concerns, so each library's
  lines can be filtered out, or routed to a store only that library can read.

The sigel doctrine — same as the metrics label doctrine (see metrics.py)
-------------------------------------------------------------------------

The `sigel` field is only ever a *configured* sigel that the code has vouched
for. The path segment is client-controlled; echoing it unchecked would let
anyone write lines into any library's log just by requesting
`/<their-sigel>/rtac`-shaped URLs with a sigel that is not theirs to name.
Lines that concern no particular library (startup, unknown paths, uvicorn's
access log) carry no `sigel` field at all.

Never in a line this module adds: the fast-track token, FOLIO credentials, or
client IPs. (uvicorn's access line and the error line do contain the request
URL, as they always have.)
"""

import contextvars
import json
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger("rtac.jsonlog")

# The library the current request concerns, once vouched for. A context
# variable rather than an argument threaded through every helper: the lines
# worth tagging are logged several calls deep (auth retries, ignored
# identifiers), and each request runs in its own context, so the value can
# never leak into another request's lines.
_current_sigel = contextvars.ContextVar("rtac_log_sigel", default=None)

# Record attributes (passed via `extra=`) that are copied into the JSON line.
# An allowlist, so a stray `extra` key can never smuggle a new field out.
_EXTRA_FIELDS = ("outcome", "channel", "duration_ms", "identifiers")


@contextmanager
def sigel_context(sigel):
    """Tag every line logged inside the block with ``sigel``.

    The caller vouches for ``sigel`` being a configured sigel (see the doctrine
    above) — never pass the raw path segment.
    """
    token = _current_sigel.set(sigel)
    try:
        yield
    finally:
        _current_sigel.reset(token)


class JsonFormatter(logging.Formatter):
    """Format a record as one JSON object on one line."""

    def format(self, record):
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # An explicit `extra={"sigel": ...}` wins: it is how code running
        # outside the request's context (the exception handlers) tags a line.
        sigel = getattr(record, "sigel", None) or _current_sigel.get()
        if sigel:
            payload["sigel"] = sigel
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # json.dumps escapes newlines and control characters, which is what
        # keeps the record on one line. default=str so an unexpected value in
        # `extra` degrades to its repr instead of losing the whole line.
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_if_requested():
    """Switch all logging to JSON lines when LOG_FORMAT=json; else do nothing.

    Returns True when JSON logging was enabled. An unrecognised value is logged
    and ignored, not raised: answering Libris matters more than log formatting.

    uvicorn configures its own loggers (plain text, not propagating) before it
    imports the application, so they are re-pointed at the JSON handler here —
    otherwise its multi-line "Exception in ASGI application" tracebacks would
    still reach stdout raw, defeating the one-record-one-line guarantee.
    """
    raw = (os.environ.get("LOG_FORMAT") or "").strip().lower()
    if raw in ("", "text"):
        return False
    if raw != "json":
        logger.warning("Ignoring invalid LOG_FORMAT=%r (expected 'json')", raw)
        return False

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.getLogger().handlers[:] = [handler]

    # The app's own lines from INFO up, which includes one line per lookup.
    # LOG_LEVEL, when set, is applied after this and takes precedence.
    logging.getLogger("rtac").setLevel(logging.INFO)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers[:] = []
        uvicorn_logger.propagate = True
    return True
