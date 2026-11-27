"""Starlab's Stratolink page: served with the usual sign-in and its map-tile CSP, and its API (reads, writes,
downloads) forwarded to the Stratolink daemon's control endpoint with the signed-in user."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI

from aobs.stratolink import config as cfgmod
from aobs.stratolink.control import API, ControlServer
from aobs.stratolink.store import Store
from aobs.web import stratolink_proxy
from test_piratewatch_proxy import PAGE_HEADERS, TOKEN, _call, _serve, ui_auth
from test_stratolink import EUI, KNOWN, ingest

UTC = dt.timezone.utc


@pytest.fixture
def stack(tmp_path):
    s = cfgmod.Settings(data_dir=str(tmp_path / "d"), udp_host="127.0.0.1", allowed_sources=["127.0.0.1"],
                        keys_file=str(tmp_path / "k.toml"), import_paths=[], firmware_dir=str(tmp_path / "fw"))
    store = Store(s.db_path)
    store.register_tracker("!f661fb94", "Stratolink-01")
    store.register_gateway(EUI, name="Observatory", lat=35.48, lon=-80.83)
    ingest(store, s, KNOWN, dt.datetime.now(UTC))
    daemon = ControlServer(API(s, store, lambda: {"state": "listening"}), TOKEN, "127.0.0.1", 0)
    daemon.start()
    assert daemon.bound.wait(5)
    app = FastAPI()
    url = {"v": f"http://127.0.0.1:{daemon.port}"}
    stratolink_proxy.register(app, ui_auth, TOKEN, lambda: url["v"], PAGE_HEADERS)
    port, server = _serve(app)
    yield _call(port), url, store
    server.should_exit = True
    daemon.stop()
    store.close()


def test_page_reads_writes_and_downloads(stack):
    call, url, store = stack
    code, html, headers = call("/stratolink")
    assert code == 200 and b"STRATOLINK" in html and "tile.openstreetmap.org" in headers["content-security-policy"]
    assert call("/v1/stratolink")[0] == 401                               # not signed in
    cookie = {"Cookie": "aobs_rf=good"}
    code, st, _ = call("/v1/stratolink", headers=cookie)
    assert code == 200 and st["trackers"][0]["node_id"] == "!f661fb94"
    code, live, _ = call("/v1/stratolink/live?after=0", headers=cookie)
    assert live["rx"][0]["content"]["text"] == "STRATOLINK TEST 001"
    # writes need the CSRF header, and the daemon learns who made them
    assert call("/v1/stratolink/flights", "POST", {"name": "x", "tracker": "!f661fb94"}, headers=cookie)[0] == 403
    code, f, _ = call("/v1/stratolink/flights", "POST", {"name": "Proxy flight", "tracker": "!f661fb94", "start": True},
                      headers={**cookie, "X-AOBS-RF": "1"})
    assert code == 200 and f["flight"]["status"] == "active"
    code, body, h = call(f"/v1/stratolink/flights/{f['flight']['id']}/export.csv", headers=cookie)
    assert code == 200 and body.startswith(b"rx_utc,") and "attachment" in h["content-disposition"]
    assert call("/v1/stratolink/../../etc/passwd", headers=cookie)[0] == 404
    url["v"] = "http://127.0.0.1:1"                                       # daemon stopped
    code, body, _ = call("/v1/stratolink", headers=cookie)
    assert code == 503 and body["daemon_down"]
