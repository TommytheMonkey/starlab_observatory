from __future__ import annotations

import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "agent"))

from obsagent import tools  # noqa: E402
from obsagent.piratewatch_client import url_from_broker  # noqa: E402


class FakeWatch(BaseHTTPRequestHandler):
    calls: list[tuple[str, str, Any]] = []

    def log_message(self, *_: Any) -> None:
        pass

    def _send(self, code: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("Authorization") != "Bearer tok":
            return self._send(401, {"detail": "bad or missing bearer token"})
        self.calls.append(("GET", self.path, None))
        self._send(200, {"state": "sweeping", "released": False})

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.calls.append(("POST", self.path, body))
        if self.path.endswith("/release"):
            return self._send(200, {"state": "released", "released": True, "confirmed": True})
        self._send(200, {"state": "waiting", "resume_requested": True})


class FakeSession:
    def __init__(self, holding: bool) -> None:
        self.holding = holding

    def release(self) -> dict[str, Any] | None:
        was, self.holding = self.holding, False
        return {"released": True} if was else None


class FakeBroker:
    def __init__(self, url: str, token: str = "tok") -> None:
        self.url, self.token = url, token


@pytest.fixture
def watch_url(monkeypatch: pytest.MonkeyPatch) -> Any:
    FakeWatch.calls = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeWatch)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("PIRATEWATCH_URL", url)
    yield url
    srv.shutdown()


def _ctx(token: str = "tok", holding: bool = True) -> Any:
    return tools.ToolContext(FakeBroker("http://100.101.102.103:8751", token), FakeSession(holding))  # type: ignore[arg-type]


def test_release_frees_r05_from_the_watch_and_the_agent(watch_url: str) -> None:
    ctx = _ctx()
    out = json.loads(tools.run_tool(ctx, "pirate_watch", {"action": "release", "minutes": 90}))
    assert out["released"] and out["agent_lease_released"]
    assert FakeWatch.calls[-1] == ("POST", "/v1/piratewatch/release", {"minutes": 90})
    assert not ctx.session.holding


def test_status_and_resume(watch_url: str) -> None:
    assert json.loads(tools.run_tool(_ctx(), "pirate_watch", {"action": "status", "minutes": 0}))["state"] == "sweeping"
    out = json.loads(tools.run_tool(_ctx(holding=False), "pirate_watch", {"action": "resume", "minutes": 0}))
    assert out["resume_requested"]
    assert FakeWatch.calls[-1][1] == "/v1/piratewatch/resume"


def test_errors_come_back_as_data(watch_url: str) -> None:
    out = json.loads(tools.run_tool(_ctx(token="wrong"), "pirate_watch", {"action": "status", "minutes": 0}))
    assert "401" in out["error"]


def test_url_is_derived_from_the_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIRATEWATCH_URL", raising=False)
    assert url_from_broker("http://100.101.102.103:8751/") == "http://100.101.102.103:8752"


def test_pirate_watch_tool_is_strict_and_registered() -> None:
    tool = next(t for t in tools.TOOLS if t["name"] == "pirate_watch")
    assert tool["strict"] and "pirate_watch" in tools.HANDLERS
