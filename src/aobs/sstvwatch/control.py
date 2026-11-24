"""HTTP control and gallery data for the SSTV watch, on 127.0.0.1 for the broker's /sstvwatch page (the pirate
watch's pattern: the broker's bearer token, release/resume edit control.json, everything else is read-only).

  GET  /v1/sstvwatch                    status + control
  GET  /v1/sstvwatch/images             ?band=&mode=&source=&since=&until=&status=decoded&before=&limit=  newest first
  GET  /v1/sstvwatch/facets             bands and modes seen, counts by status (the gallery's filters)
  GET  /v1/sstvwatch/images/ID          one image's record
  GET  /v1/sstvwatch/images/ID/(image|thumb|audio|iq)   its files (Range requests supported)
  POST /v1/sstvwatch/release            {"minutes": N} (0 = until resumed)
  POST /v1/sstvwatch/resume
Files are found from the record, never from the request, and only under the watch's data_dir or the broker's
recordings (ISS images stay where the broker decoded them).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..piratewatch.daemon import read_control, released, write_control
from .config import Settings
from .store import Store, iso

LOG = logging.getLogger("aobs.sstvwatch.control")
UTC = dt.timezone.utc
MAX_RELEASE_MIN = 24 * 60
FILE_ROOTS = ("/data/archive/starlab/recordings", "/data/starlab/recordings")
KINDS = {"image": ("image_path", "image/png"), "thumb": ("thumb_path", "image/jpeg"),
         "audio": ("audio_path", "audio/wav"), "iq": ("iq_path", "application/octet-stream")}


def _read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def snapshot(s: Settings) -> dict[str, Any]:
    ctl = read_control(s)
    st = _read_json(s.path("status.json"))
    return {"state": st.get("state"), "status_at": st.get("at"), "started_at": st.get("started_at"),
            "watchers": st.get("watchers") or [], "counts": st.get("counts") or {},
            "decoded_this_run": st.get("decoded_this_run"), "pending_batch": st.get("pending_batch"),
            "released": released(ctl, dt.datetime.now(UTC)), "released_until": ctl.get("until"),
            "channels": [{"name": c.name, "hz": c.hz, "mode": c.mode, "band": c.band, "day_weight": c.day_weight,
                          "night_weight": c.night_weight, "when": c.when} for c in s.channels],
            "min_quality": s.min_quality}


def _one(q: dict[str, list[str]], name: str) -> str | None:
    v = (q.get(name) or [None])[0]
    return v.strip() if isinstance(v, str) and v.strip() else None


def images_view(store: Store, q: dict[str, list[str]]) -> dict[str, Any]:
    for k in ("since", "until"):
        v = _one(q, k)
        if v and not re.fullmatch(r"\d{4}-\d\d-\d\d(T[\d:.]+Z?)?", v):
            raise ValueError(f"{k} must be an ISO date or UTC time")
    status = _one(q, "status") or "decoded"
    if status not in ("decoded", "noise", "failed", "decoding", "all"):
        raise ValueError("status must be decoded, noise, failed, decoding or all")
    limit = int(_one(q, "limit") or 60)
    before = _one(q, "before")
    rows = store.images(status=None if status == "all" else status, band=_one(q, "band"), mode=_one(q, "mode"),
                        source=_one(q, "source"), since=_one(q, "since"), until=_one(q, "until"),
                        limit=limit, before_id=int(before) if before and before.isdigit() else None)
    return {"images": [public(r) for r in rows], "more": len(rows) >= max(1, min(500, limit))}


def public(r: dict[str, Any]) -> dict[str, Any]:
    """A record as the page sees it: file paths become links."""
    out = {k: v for k, v in r.items() if not k.endswith("_path")}
    for kind, (col, _) in KINDS.items():
        out[f"{kind}_url"] = f"/v1/sstvwatch/images/{r['id']}/{kind}" if r.get(col) else None
    out["recording_url"] = f"/rf#recording={r['recording_id']}" if r.get("recording_id") else None
    return out


def file_for(s: Settings, store: Store, image_id: int, kind: str) -> tuple[pathlib.Path, str] | None:
    row = store.get(image_id)
    if not row or kind not in KINDS or not row.get(KINDS[kind][0]):
        return None
    path = pathlib.Path(row[KINDS[kind][0]]).resolve()
    roots = [s.root.resolve(), *(pathlib.Path(r).resolve() for r in FILE_ROOTS)]
    if not any(path.is_relative_to(r) for r in roots) or not path.is_file():
        return None
    mime = KINDS[kind][1]
    if kind == "iq" and path.suffix in (".cu8", ".cs16"):
        mime = "application/octet-stream"
    return path, mime


def release(s: Settings, minutes: int) -> dict[str, Any]:
    if not 0 <= minutes <= MAX_RELEASE_MIN:
        raise ValueError(f"minutes must be 0-{MAX_RELEASE_MIN} (0 = until resumed)")
    now = dt.datetime.now(UTC)
    write_control(s, released=True, until=iso(now + dt.timedelta(minutes=minutes)) if minutes else None, at=iso(now))
    return {**snapshot(s), "release_requested": True}


def resume(s: Settings) -> dict[str, Any]:
    write_control(s, released=False, until=None)
    return {**snapshot(s), "resume_requested": True}


def make_handler(s: Settings, token: str) -> type[BaseHTTPRequestHandler]:
    store = Store(s.path("sstvwatch.sqlite3"))

    class Handler(BaseHTTPRequestHandler):
        server_version = "starlab-sstvwatch/1"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOG.debug("%s %s", self.address_string(), fmt % args)

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

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            url = urlsplit(self.path)
            path, q = url.path.rstrip("/"), parse_qs(url.query)
            try:
                if path == "/v1/sstvwatch":
                    return self._send(200, snapshot(s))
                if path == "/v1/sstvwatch/images":
                    return self._send(200, images_view(store, q))
                if path == "/v1/sstvwatch/facets":
                    return self._send(200, store.facets())
                if m := re.fullmatch(r"/v1/sstvwatch/images/(\d{1,12})", path):
                    row = store.get(int(m.group(1)))
                    return self._send(200, public(row)) if row else self._send(404, {"detail": "no such image"})
                if m := re.fullmatch(r"/v1/sstvwatch/images/(\d{1,12})/(image|thumb|audio|iq)", path):
                    return self._file(int(m.group(1)), m.group(2))
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            self._send(404, {"detail": "not found"})

        def _file(self, image_id: int, kind: str) -> None:
            found = file_for(s, store, image_id, kind)
            if found is None:
                return self._send(404, {"detail": f"no {kind} file for image {image_id}"})
            path, mime = found
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
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "private, max-age=3600")
            if kind == "iq":
                self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
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
                if self.path == "/v1/sstvwatch/release":
                    n = min(int(self.headers.get("Content-Length") or 0), 4096)
                    body = json.loads(self.rfile.read(n) or b"{}")
                    minutes = body.get("minutes") or 0 if isinstance(body, dict) else None
                    if isinstance(minutes, bool) or not isinstance(minutes, int):
                        raise ValueError("minutes must be a whole number")
                    return self._send(200, release(s, minutes))
                if self.path == "/v1/sstvwatch/resume":
                    return self._send(200, resume(s))
            except ValueError as exc:
                return self._send(400, {"detail": str(exc)})
            self._send(404, {"detail": "not found"})

    return Handler


class ControlServer:
    """Serves in a daemon thread; retries the bind every minute if the port is taken."""

    def __init__(self, s: Settings, token: str, host: str | None = None, port: int | None = None):
        self.s, self.token = s, token
        self.host = host if host is not None else s.control_host
        self.port = port if port is not None else s.control_port
        self.httpd: ThreadingHTTPServer | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sstv-control", daemon=True)
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
            LOG.info("control endpoint on http://%s:%s/v1/sstvwatch", self.host, self.port)
            self.bound.set()
            self.httpd.serve_forever(poll_interval=0.5)
            return

    def stop(self) -> None:
        self._stop.set()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
