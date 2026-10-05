"""End-to-end tests for the HTTP endpoints via FastAPI's TestClient."""

import types

from lxml import etree

import application

from conftest import FakeFolioClient


def _items(xml_bytes):
    root = etree.fromstring(xml_bytes)
    return root.findall("Item")


# --- index ------------------------------------------------------------------


def test_index_serves_html_landing_page(client, libraries_dir, settings):
    libraries_dir("alpha", settings)
    libraries_dir("beta", settings)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    # Describes the service and links to the Libris documentation.
    assert "lånestatus" in body
    assert application.LIBRIS_DOCS_URL in body
    # Points at the hosted instance libraries can test / have operated, and at
    # the source code for self-hosting.
    assert application.HOSTED_SERVICE_URL in body
    assert application.SOURCE_CODE_URL in body
    # Links to the interactive API docs.
    assert 'href="/docs"' in body
    # Lists the configured sigels.
    assert "alpha" in body and "beta" in body
    # No template placeholders left unsubstituted.
    assert "__" not in body


# --- /{sigel}/rtac ----------------------------------------------------------


def test_rtac_returns_items_for_matching_holdings(
    client, libraries_dir, settings, monkeypatch
):
    libraries_dir("alpha", settings)
    fake = FakeFolioClient(
        responses={
            "/instance-storage/instances": {"instances": [{"id": "i1"}]},
            "/rtac/": {
                "holdings": [
                    {"id": "h1", "location": "Main", "status": "Available"},
                    {"id": "h2", "location": "Annex", "status": "Checked out",
                     "dueDate": "2024-05-01T00:00:00Z"},
                ]
            },
        }
    )
    monkeypatch.setattr(application, "get_folio_client", lambda sigel, s: fake)

    resp = client.get("/alpha/rtac", params={"Bib_ID": "123"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/xml")
    items = _items(resp.content)
    assert len(items) == 2
    assert items[0].findtext("UniqueItemId") == "h1"
    assert items[1].findtext("Status_Date") == "2024-05-01"
    # The configured Bib_ID UUIDs both end up in the CQL query.
    assert 'identifierTypeId="11111111-1111-1111-1111-111111111111"' in fake.calls[0]


def test_rtac_no_holdings_returns_unknown_placeholder(
    client, libraries_dir, settings, monkeypatch
):
    libraries_dir("alpha", settings)
    fake = FakeFolioClient(
        responses={"/instance-storage/instances": {"instances": []}}
    )
    monkeypatch.setattr(application, "get_folio_client", lambda sigel, s: fake)

    resp = client.get("/alpha/rtac", params={"Bib_ID": "123"})
    assert resp.status_code == 200
    items = _items(resp.content)
    assert len(items) == 1
    assert items[0].findtext("Status") == "Okänd"


def test_rtac_without_identifier_returns_unknown_placeholder(
    client, libraries_dir, settings
):
    # No identifier -> ValueError -> exception handler -> empty XML on /rtac.
    libraries_dir("alpha", settings)
    resp = client.get("/alpha/rtac")
    assert resp.status_code == 200
    items = _items(resp.content)
    assert len(items) == 1
    assert items[0].findtext("Status") == "Okänd"


def test_rtac_folio_error_returns_unknown_placeholder(
    client, libraries_dir, settings, monkeypatch
):
    libraries_dir("alpha", settings)
    fake = FakeFolioClient(error=RuntimeError("FOLIO down"))
    monkeypatch.setattr(application, "get_folio_client", lambda sigel, s: fake)

    resp = client.get("/alpha/rtac", params={"Bib_ID": "123"})
    assert resp.status_code == 200
    assert _items(resp.content)[0].findtext("Status") == "Okänd"


def test_rtac_unknown_sigel_returns_unknown_placeholder(client, libraries_dir, settings):
    libraries_dir("alpha", settings)
    resp = client.get("/ghost/rtac", params={"Bib_ID": "123"})
    # load_settings raises FileNotFoundError -> handled -> empty XML.
    assert resp.status_code == 200
    assert _items(resp.content)[0].findtext("Status") == "Okänd"


# --- /{sigel}/validate-folio-connection -------------------------------------


def test_validate_unknown_sigel_404(client, libraries_dir, settings):
    libraries_dir("alpha", settings)
    resp = client.get("/ghost/validate-folio-connection")
    assert resp.status_code == 404
    assert resp.json()["status"] == "error"


def test_validate_missing_settings_503(client, libraries_dir):
    libraries_dir("alpha", {"okapi_url": "https://x"})  # tenant/user/pass missing
    resp = client.get("/alpha/validate-folio-connection")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "tenant_id" in detail and "username" in detail and "password" in detail


def test_validate_folio_unreachable_502(client, libraries_dir, settings, monkeypatch):
    def boom(settings):
        raise ConnectionError("refused")

    monkeypatch.setattr(application, "_new_folio_client", boom)
    libraries_dir("alpha", settings)
    resp = client.get("/alpha/validate-folio-connection")
    assert resp.status_code == 502
    assert "Could not connect" in resp.json()["detail"]


def test_validate_success_200(client, libraries_dir, settings, monkeypatch):
    fake = FakeFolioClient(gateway_url="https://okapi.example", tenant_id="diku")
    monkeypatch.setattr(application, "_new_folio_client", lambda s: fake)
    libraries_dir("alpha", settings)
    resp = client.get("/alpha/validate-folio-connection")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "status": "ok",
        "sigel": "alpha",
        "backend": "rtac",  # default backend; only the FOLIO API to report
        "folio": {"status": "ok"},
    }
    # Server details (okapi_url / tenant) must not be disclosed in the response.
    assert "okapi_url" not in body and "tenant" not in body
    # The instance-search API is actually exercised, not just login.
    assert any("/instance-storage/instances" in c for c in fake.calls)
    # The one-shot client must be closed so we don't leak a connection pool.
    assert fake.closed is True


def test_validate_folio_instance_storage_unauthorized_502(
    client, libraries_dir, settings, monkeypatch
):
    # Login succeeds but the instance-search query fails (e.g. missing inventory
    # read perm) -> the API check, not just login, catches it.
    fake = FakeFolioClient(error=RuntimeError("forbidden"))
    monkeypatch.setattr(application, "_new_folio_client", lambda s: fake)
    libraries_dir("alpha", settings)
    resp = client.get("/alpha/validate-folio-connection")
    assert resp.status_code == 502
    assert "instance-storage" in resp.json()["detail"]
    assert fake.closed is True


# --- validate with the edge backend (also probes edge-rtac) -----------------


def _edge_settings(settings):
    return {
        **settings,
        "rtac_backend": "edge",
        "edge_rtac_url": "https://edge.example",
        "edge_rtac_api_key": "KEY123",
    }


def test_validate_edge_success_reports_edge_status(
    client, libraries_dir, settings, monkeypatch
):
    fake = FakeFolioClient()
    monkeypatch.setattr(application, "_new_folio_client", lambda s: fake)
    # edge reachable + apiKey accepted -> probe returns without raising.
    monkeypatch.setattr(application, "_probe_edge_connection", lambda s: None)
    libraries_dir("alpha", _edge_settings(settings))

    resp = client.get("/alpha/validate-folio-connection")
    assert resp.status_code == 200
    # Both APIs the edge flow uses are reported: FOLIO (instance search) + edge.
    assert resp.json() == {
        "status": "ok",
        "sigel": "alpha",
        "backend": "edge",
        "folio": {"status": "ok"},
        "edge": {"status": "ok"},
    }
    # The one-shot FOLIO client is still closed even on the edge path.
    assert fake.closed is True


def test_validate_edge_unreachable_502(client, libraries_dir, settings, monkeypatch):
    fake = FakeFolioClient()
    monkeypatch.setattr(application, "_new_folio_client", lambda s: fake)

    def boom(s):
        raise ConnectionError("edge refused")

    monkeypatch.setattr(application, "_probe_edge_connection", boom)
    libraries_dir("alpha", _edge_settings(settings))

    resp = client.get("/alpha/validate-folio-connection")
    assert resp.status_code == 502
    body = resp.json()
    assert body["status"] == "error" and body["backend"] == "edge"
    assert "edge-rtac" in body["detail"]
    # FOLIO connected fine, so its one-shot client must still be closed.
    assert fake.closed is True


def test_validate_edge_missing_config_503(client, libraries_dir, settings, monkeypatch):
    # backend=edge but no edge url/key and no env fallback -> config incomplete.
    monkeypatch.setattr(application, "EDGE_RTAC_URL", None)
    monkeypatch.setattr(application, "EDGE_RTAC_API_KEY", None)
    libraries_dir("alpha", {**settings, "rtac_backend": "edge"})

    resp = client.get("/alpha/validate-folio-connection")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "edge_rtac_url" in detail and "edge_rtac_api_key" in detail


# --- exception handler (JSON branch) ----------------------------------------


def test_handle_error_non_rtac_path_returns_json_500():
    request = types.SimpleNamespace(
        url=types.SimpleNamespace(path="/alpha/validate-folio-connection"),
        path_params={"sigel": "alpha"},
    )
    resp = application.handle_error(request, ValueError("boom"))
    assert resp.status_code == 500
    assert resp.media_type == "application/json"


def test_handle_error_rtac_path_returns_xml():
    request = types.SimpleNamespace(
        url=types.SimpleNamespace(path="/alpha/rtac"),
        path_params={"sigel": "alpha"},
    )
    resp = application.handle_error(request, ValueError("boom"))
    assert resp.media_type == "text/xml"
    # etree.tostring() defaults to ASCII, so "ä" comes back as a char reference.
    assert _items(resp.body)[0].findtext("Status") == "Okänd"
