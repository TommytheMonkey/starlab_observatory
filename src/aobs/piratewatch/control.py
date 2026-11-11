"""HTTP control for the pirate watch, so the Slack agent (on monkeybrain) can free OBS-R05 for SDR++ and take it back.

Listens on observatory's Tailscale address only and takes the broker's bearer token: whoever may lease R05 from
the broker may also ask the watch to let go of it. The endpoints only edit control.json, exactly as the CLI's
`release` / `resume` do; the watch loop does the rest.

  GET  /v1/piratewatch            status + control
  POST /v1/piratewatch/release    {"minutes": N}  (0 or absent = until resumed); waits up to 20 s for the radio to stop
  POST /v1/piratewatch/resume
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .config import Settings
from .store import iso

LOG = logging.getLogger("aobs.piratewatch.control")
UTC = dt.timezone.utc
MAX_RELEASE_MIN = 24 * 60


def _read_json(path: Any) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def snapshot(s: Settings) -> dict[str, Any]:
    from .daemon import read_control, released
    ctl = read_control(s)
    st = _read_json(s.path("status.json"))
    return {"receiver": s.receiver, "state": st.get("state"), "detail": st.get("detail"), "status_at": st.get("at"),
            "lease_held": st.get("lease_held"), "hits": st.get("hits"), "baseline": st.get("baseline"),
            "released": released(ctl, dt.datetime.now(UTC)), "released_until": ctl.get("until")}


def release(s: Settings, minutes: int, wait_s: float = 20.0, poll: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    from .daemon import write_control
    if not 0 <= minutes <= MAX_RELEASE_MIN:
        raise ValueError(f"minutes must be 0-{MAX_RELEASE_MIN} (0 = until resumed)")
    now = dt.datetime.now(UTC)
    until = iso(now + dt.timedelta(minutes=minutes)) if minutes else None
    write_control(s, released=True, until=until, at=iso(now))
    deadline = time.monotonic() + wait_s
    snap = snapshot(s)
    while snap["state"] not in ("released", "stopped", None) and time.monotonic() < deadline:
        poll(0.5)
        snap = snapshot(s)
    snap["confirmed"] = snap["state"] in ("released", "stopped", None)
    if not snap["confirmed"]:
        snap["note"] = "release requested, but the watch has not confirmed yet; check status again shortly"
    return snap


def resume(s: Settings) -> dict[str, Any]:
    from .daemon import write_control
    write_control(s, released=False, until=None)
    return {**snapshot(s), "resume_requested": True,
            "note": "the watch restarts once no other program (SDR++ server) holds the receiver"}


def make_handler(s: Settings, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "starlab-piratewatch/1"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOG.info("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            got = self.headers.get("Authorization", "")
            if got.startswith("Bearer ") and token and secrets.compare_digest(got[7:].strip(), token):
                return True
            self._send(401, {"detail": "bad or missing bearer token"})
            return False

        def _body(self) -> dict[str, Any]:
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                raise ValueError("the body must be a JSON object")
            return body

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            if self.path.rstrip("/") != "/v1/piratewatch":
                return self._send(404, {"detail": "not found"})
            self._send(200, snapshot(s))

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            try:
                if self.path == "/v1/piratewatch/release":
                    minutes = self._body().get("minutes") or 0
                    if isinstance(minutes, bool) or not isinstance(minutes, int):
                        raise ValueError("minutes must be a whole number")
                    return self._send(200, release(s, minutes))
                if self.path == "/v1/piratewatch/resume":
                    return self._send(200, resume(s))
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            self._send(404, {"detail": "not found"})

    return Handler


class ControlServer:
    """Serves in a daemon thread. If the address cannot be bound (Tailscale down), it retries every minute."""

    def __init__(self, s: Settings, token: str, host: str | None = None, port: int | None = None):
        self.s, self.token = s, token
        self.host = host if host is not None else s.control_host
        self.port = port if port is not None else s.control_port
        self.httpd: ThreadingHTTPServer | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pirate-control", daemon=True)
        self.bound = threading.Event()

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.httpd = ThreadingHTTPServer((self.host, self.port), make_handler(self.s, self.token))
            except OSError as exc:
                LOG.warning("control endpoint %s:%s not available (%s); retrying in 60 s", self.host, self.port, exc)
                self._stop.wait(60)
                continue
            self.port = self.httpd.server_address[1]
            LOG.info("control endpoint on http://%s:%s/v1/piratewatch", self.host, self.port)
            self.bound.set()
            self.httpd.serve_forever(poll_interval=0.5)
            return

    def stop(self) -> None:
        self._stop.set()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
