"""uvicorn's access log stays on, but the fast-track token never reaches it."""

import logging

import application


def access_record(path: str) -> logging.LogRecord:
    # Shaped like uvicorn's: '%s - "%s %s HTTP/%s" %d'
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        '%s - "%s %s HTTP/%s" %d', ("10.0.0.1:5000", "GET", path, "1.1", 200), None,
    )


def test_masks_token_query_parameter_only():
    f = application._MaskTokenFilter()
    for path, expected in [
        ("/S12/rtac?Bib_ID=123&token=abc&ONR=9", "/S12/rtac?Bib_ID=123&token=_token_&ONR=9"),
        ("/S12/rtac?token=abc", "/S12/rtac?token=_token_"),
        ("/S12/rtac?Bib_ID=123", "/S12/rtac?Bib_ID=123"),
    ]:
        record = access_record(path)
        assert f.filter(record)
        assert record.getMessage() == f'10.0.0.1:5000 - "GET {expected} HTTP/1.1" 200'


def test_error_log_masks_token(client, libraries_dir, caplog):
    # Unknown sigel → handle_error logs the request URL.
    with caplog.at_level(logging.ERROR, logger="rtac"):
        client.get("/NOPE/rtac?Bib_ID=1&token=SECRETTOKEN")
    assert "Error while serving" in caplog.text
    assert "SECRETTOKEN" not in caplog.text
    assert "token=_token_" in caplog.text


def test_filter_installed_once_on_uvicorn_access():
    filters = [f for f in logging.getLogger("uvicorn.access").filters
               if isinstance(f, application._MaskTokenFilter)]
    assert len(filters) == 1
