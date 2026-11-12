"""HTTP control for the pirate watch: the Slack agent (on monkeybrain) frees OBS-R05 for SDR++ and takes it back,
and Starlab's web page (through the broker, on 127.0.0.1) shows the watch, its hits and its clips.

Listens on observatory's Tailscale address and on 127.0.0.1, and takes the broker's bearer token: whoever may lease
R05 from the broker may also ask the watch to let go of it. release / resume only edit control.json, exactly as the
CLI's `release` / `resume` do; the watch loop does the rest. Everything else is read-only.

  GET  /v1/piratewatch            status + control
  GET  /v1/piratewatch/hits       ?speech=1&min_hz=&max_hz=&since=&until=&channel_hz=&hour=&before=&limit=
  GET  /v1/piratewatch/heatmap    ?since=&until=   (ISO UTC; per channel x UTC hour of day)
  GET  /v1/piratewatch/ignored    the learned ignore list (steady carriers / spurs) and auto-masked channels
  GET  /v1/piratewatch/clips/ID   the WAV of hit ID (Range requests supported)
  POST /v1/piratewatch/release    {"minutes": N}  (0 or absent = until resumed); waits up to 20 s for the radio to stop
  POST /v1/piratewatch/resume
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import threading
import pathlib
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import heatmap
from .config import Settings
from .spectrum import Baseline
from .store import Reader, iso

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
            "started_at": st.get("started_at"), "recording_hz": st.get("recording_hz"),
            "lease_held": st.get("lease_held"), "lease_expires": st.get("lease_expires"), "hits": st.get("hits"),
            "baseline": st.get("baseline"), "last_sweep": st.get("last_sweep"),
            "released": released(ctl, dt.datetime.now(UTC)), "released_until": ctl.get("until"),
            "band_hz": [s.start_hz, s.stop_hz], "no_speech_keep_h": s.no_speech_keep_h}


# ------------------------------------------------------------ read-only views (web page)
def _stamp(value: str | None, name: str) -> str | None:
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date/time") from exc
    return iso(t if t.tzinfo else t.replace(tzinfo=UTC))


def _int(q: dict[str, list[str]], name: str, lo: int | None = None, hi: int | None = None) -> int | None:
    raw = (q.get(name) or [""])[0].strip()
    if not raw:
        return None
    try:
        v = int(float(raw))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ValueError(f"{name} must be {lo}-{hi}")
    return v


def hits_view(s: Settings, q: dict[str, list[str]]) -> dict[str, Any]:
    speech = (q.get("speech") or ["1"])[0].lower() not in ("0", "false", "no", "")
    return Reader(s.path("piratewatch.sqlite3")).hits(
        speech_only=speech, min_hz=_int(q, "min_hz"), max_hz=_int(q, "max_hz"),
        since=_stamp((q.get("since") or [None])[0], "since"), until=_stamp((q.get("until") or [None])[0], "until"),
        channel_hz=_int(q, "channel_hz"), hour=_int(q, "hour", 0, 23), before_id=_int(q, "before"),
        limit=_int(q, "limit", 1, 200) or 50)


def heatmap_view(s: Settings, q: dict[str, list[str]]) -> dict[str, Any]:
    since = _stamp((q.get("since") or [None])[0], "since")
    until = _stamp((q.get("until") or [None])[0], "until")
    if since is None:
        since = iso(dt.datetime.now(UTC) - dt.timedelta(days=7))
    # activity rows are whole UTC hours: an hour counts when it starts inside [since, until)
    rows = Reader(s.path("piratewatch.sqlite3")).activity(since[:13] if since else None,
                                                          until[:13] if until else None)
    return {"since": since, "until": until, **heatmap.hour_grid(rows)}


def ignored_view(s: Settings) -> dict[str, Any]:
    b = Baseline(s.learn_sweeps, s.threshold_db, s.duty_mask, static_ranges=s.static_ranges())
    found = b.load(s.path("baseline.npz"))
    out = b.ignore_list()
    out["saved"] = found
    out["combs"] = [{"tooth_hz": c[0], "spacing_hz": c[1], "half_width_hz": c[2]} for c in s.comb_masks]
    if out["auto"]:
        hits = Reader(s.path("piratewatch.sqlite3")).hits(speech_only=False, limit=200)["hits"]
        masked = [h for h in hits if "masked as a steady carrier" in (h.get("note") or "")]
        for r in out["auto"]:
            h = next((h for h in masked if r["lo_hz"] <= h["freq_hz"] <= r["hi_hz"]), None)
            r["hit"] = {"id": h["id"], "at": h.get("start_utc") or h["detected_at"]} if h else None
    return out


def clip_path(s: Settings, hit_id: int) -> pathlib.Path | None:
    """The WAV of one hit, only if it sits under the watch's clips directory."""
    row = Reader(s.path("piratewatch.sqlite3")).hit(hit_id)
    if not row or not row.get("audio_path"):
        return None
    path = pathlib.Path(row["audio_path"]).resolve()
    if not path.is_relative_to(s.path("clips").resolve()) or not path.is_file():
        return None
    return path


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
            url = urlsplit(self.path)
            path, q = url.path.rstrip("/"), parse_qs(url.query)
            try:
                if path == "/v1/piratewatch":
                    return self._send(200, snapshot(s))
                if path == "/v1/piratewatch/hits":
                    return self._send(200, hits_view(s, q))
                if path == "/v1/piratewatch/heatmap":
                    return self._send(200, heatmap_view(s, q))
                if path == "/v1/piratewatch/ignored":
                    return self._send(200, ignored_view(s))
                if m := re.fullmatch(r"/v1/piratewatch/clips/(\d{1,12})(?:\.wav)?", path):
                    return self._clip(int(m.group(1)))
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            self._send(404, {"detail": "not found"})

        def _clip(self, hit_id: int) -> None:
            path = clip_path(s, hit_id)
            if path is None:
                return self._send(404, {"detail": f"no audio for hit {hit_id} (non-speech audio is kept "
                                                  f"{s.no_speech_keep_h:g} h; missed bursts have none)"})
            size = path.stat().st_size
            start, end, code = 0, size - 1, 200
            if m := re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip()):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
            self.send_response(code)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "private, max-age=3600")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with path.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0 and (chunk := f.read(min(65536, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)

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
