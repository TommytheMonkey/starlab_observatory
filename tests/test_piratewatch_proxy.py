"""Starlab's Pirate Watch page: served with the usual sign-in, and its API forwarded to the watch's control endpoint."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

import pytest
from fastapi import Cookie, FastAPI, Header, HTTPException, Request

from aobs.piratewatch.config import Settings
from aobs.piratewatch.control import ControlServer
from aobs.web import piratewatch_proxy
from test_piratewatch_web import _populate

TOKEN = "broker-token"
PAGE_HEADERS = {"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self'"}


def _serve(app: FastAPI) -> tuple[int, Any]:
    import uvicorn
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    return port, server


def _call(port: int) -> Callable[..., tuple[int, Any, Any]]:
    def call(path: str, method: str = "GET", body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any, Any]:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=headers or {},
                                     data=json.dumps(body).encode() if body is not None else None)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get("content-type", "").startswith("application/json")
                                  else raw), r.headers
        except urllib.error.HTTPError as e:
            raw = e.read() or b"{}"
            return e.code, json.loads(raw), e.headers
    return call


def ui_auth(request: Request, authorization: str = Header(default=""), aobs_rf: str | None = Cookie(default=None)) -> str:
    """Same shape as the broker's: the bearer token, or a session cookie plus X-AOBS-RF on writes."""
    if authorization == f"Bearer {TOKEN}":
        return "api"
    if aobs_rf == "good":
        if request.method != "GET" and request.headers.get("x-aobs-rf") != "1":
            raise HTTPException(403, "missing X-AOBS-RF header")
        return "owner (web)"
    raise HTTPException(401, "not signed in")


@pytest.fixture
def stack(tmp_path):
    s = Settings(data_dir=str(tmp_path / "data"))
    ids = _populate(s)
    s.path("status.json").write_text(json.dumps({"state": "sweeping", "at": "x"}))
    watch = ControlServer(s, TOKEN, host="127.0.0.1", port=0)
    watch.start()
    assert watch.bound.wait(5)
    url = {"value": f"http://127.0.0.1:{watch.port}"}
    app = FastAPI()
    piratewatch_proxy.register(app, ui_auth, TOKEN, lambda: url["value"], PAGE_HEADERS)
    port, server = _serve(app)
    yield _call(port), s, ids, url
    server.should_exit = True
    watch.stop()


SIGNED_IN = {"Cookie": "aobs_rf=good"}


def test_page_is_served_and_the_api_needs_sign_in(stack) -> None:
    call, *_ = stack
    code, page, headers = call("/piratewatch")
    assert code == 200 and b"<title>Pirate Watch" in page and headers["content-security-policy"] == "default-src 'self'"
    assert call("/v1/piratewatch")[0] == 401
    assert call("/v1/piratewatch/hits")[0] == 401
    assert call("/v1/piratewatch/release", "POST", {"minutes": 5})[0] == 401
    code, st, _ = call("/v1/piratewatch", headers=SIGNED_IN)
    assert code == 200 and st["state"] == "sweeping"


def test_reads_are_forwarded_with_the_broker_token(stack) -> None:
    call, s, ids, _ = stack
    code, h, _ = call("/v1/piratewatch/hits?speech=0&min_hz=260000000", headers=SIGNED_IN)
    assert code == 200 and [x["id"] for x in h["hits"]] == [ids["missed"], ids["no_speech"]]
    code, g, _ = call("/v1/piratewatch/heatmap?since=2026-11-12T00:00:00Z", headers=SIGNED_IN)
    assert code == 200 and len(g["channels"]) == 2
    assert call("/v1/piratewatch/ignored", headers=SIGNED_IN)[0] == 200
    code, err, _ = call("/v1/piratewatch/hits?hour=99", headers=SIGNED_IN)
    assert code == 400 and "hour" in err["detail"]                          # the watch's own error comes through
    assert call("/v1/piratewatch/status.json", headers=SIGNED_IN)[0] == 404   # only the known views are forwarded
    assert call("/v1/piratewatch/clips/abc", headers=SIGNED_IN)[0] == 404


def test_clips_stream_with_ranges(stack) -> None:
    call, s, ids, _ = stack
    code, wav, h = call(f"/v1/piratewatch/clips/{ids['speech']}", headers=SIGNED_IN)
    assert code == 200 and wav[:4] == b"RIFF" and h["content-type"] == "audio/wav" and h["accept-ranges"] == "bytes"
    code, part, h = call(f"/v1/piratewatch/clips/{ids['speech']}", headers={**SIGNED_IN, "Range": "bytes=0-3"})
    assert code == 206 and part == b"RIFF" and h["content-range"] == f"bytes 0-3/{len(wav)}"
    assert call(f"/v1/piratewatch/clips/{ids['missed']}", headers=SIGNED_IN)[0] == 404


def test_release_and_resume_need_the_csrf_header_and_reach_the_watch(stack) -> None:
    call, s, ids, _ = stack
    assert call("/v1/piratewatch/release", "POST", {"minutes": 30}, SIGNED_IN)[0] == 403
    s.path("status.json").write_text(json.dumps({"state": "released"}))   # no watch loop here: confirm at once
    code, r, _ = call("/v1/piratewatch/release", "POST", {"minutes": 30}, {**SIGNED_IN, "X-AOBS-RF": "1"})
    assert code == 200 and r["released"] and r["released_until"]
    ctl = json.loads(s.path("control.json").read_text())
    assert ctl["released"] is True and ctl["until"]
    code, r, _ = call("/v1/piratewatch/release", "POST", {"minutes": 99999}, {**SIGNED_IN, "X-AOBS-RF": "1"})
    assert code == 400
    code, r, _ = call("/v1/piratewatch/resume", "POST", {}, {**SIGNED_IN, "X-AOBS-RF": "1"})
    assert code == 200 and r["resume_requested"]
    assert json.loads(s.path("control.json").read_text())["released"] is False


def test_a_stopped_watch_is_a_clear_503(stack) -> None:
    call, _, _, url = stack
    with socket.socket() as sock:              # a port nothing listens on
        sock.bind(("127.0.0.1", 0))
        url["value"] = f"http://127.0.0.1:{sock.getsockname()[1]}"
    code, err, _ = call("/v1/piratewatch", headers=SIGNED_IN)
    assert code == 503 and err["watch_down"] and "not answering" in err["error"]


def test_watch_url_comes_from_the_starlab_config(tmp_config) -> None:
    assert piratewatch_proxy.watch_url(tmp_config) == "http://127.0.0.1:8752"
    tmp_config.raw["piratewatch"] = {"url": "http://127.0.0.1:9999/"}
    assert piratewatch_proxy.watch_url(tmp_config) == "http://127.0.0.1:9999"
    assert piratewatch_proxy.watch_url(None) == "http://127.0.0.1:8752"


def test_the_broker_app_serves_the_page_behind_its_own_sign_in(tmp_config) -> None:
    from aobs.coordination.api import create_app
    from aobs.coordination.broker import Broker
    from test_broker import FakeHw

    tmp_config.raw["broker"] = {"receivers": ["OBS-R05"]}
    port, server = _serve(create_app(Broker(tmp_config, FakeHw()), "t"))
    call = _call(port)
    try:
        code, page, _ = call("/piratewatch")
        assert code == 200 and b"Pirate Watch" in page
        assert call("/v1/piratewatch")[0] == 401
        assert call("/v1/piratewatch/hits", headers={"Authorization": "Bearer wrong"})[0] == 401
    finally:
        server.should_exit = True
