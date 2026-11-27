"""HTTP API for the broker's /stratolink page, on 127.0.0.1 only, with the broker's bearer token (the SSTV watch's
pattern). The broker forwards signed-in users' calls here and adds X-Starlab-User (who, for the audit log).

  GET  /v1/stratolink                         status: receiver, gateways, trackers, active flights
  GET  /v1/stratolink/live?after=RX_ID        receptions newer than RX_ID (the page polls this every 2 s)
  GET  /v1/stratolink/console?before=&node=&port=&trackers=1&limit=
  GET  /v1/stratolink/track?node=!id&flight=&since=&until=
  GET  /v1/stratolink/packets/REF             one packet and every reception of it, by gateway
  GET  /v1/stratolink/nodes                   discovered nodes
  GET  /v1/stratolink/coverage?days=&node=
  GET  /v1/stratolink/flights                 POST {name, tracker, mode, notes, loss_warn_s, loss_lost_s, start}
  GET  /v1/stratolink/flights/ID              POST: update; POST /start, /end
  GET  /v1/stratolink/flights/ID/export.(csv|geojson|kml)
  GET  /v1/stratolink/flights/ID/preflight    POST /preflight {item, confirmed, note}
  POST /v1/stratolink/trackers                {node_id, name, short_name, hardware, channel, color, notes}
  POST /v1/stratolink/trackers/!ID            update (active: false retires it); POST /trackers/!ID/key {psk}
  POST /v1/stratolink/gateways                {eui, name, lat, lon, alt_m, ip, mgmt_ip, notes}
  GET  /v1/stratolink/profiles                PUT-by-POST /profiles/SLUG {profile}; POST /profiles/SLUG/reset
  GET  /v1/stratolink/hardware
  POST /v1/stratolink/provision/bundle        {name, short_name, hardware, profile, overrides} -> bundle JSON
  GET  /v1/stratolink/provision               bundles and their reports; POST /provision/report {report}
  GET  /v1/stratolink/provision/tool          the provisioning tool (stratolink_provision.py)
  GET  /v1/stratolink/firmware                archived ground-station firmware; GET /firmware/REL/PATH downloads
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import config as cfgmod
from . import firmware
from . import meshtastic as mt
from . import profiles as prof
from . import track, views
from .provision import Provisioning
from .store import Store, iso

LOG = logging.getLogger("aobs.stratolink.control")
UTC = dt.timezone.utc
P = "/v1/stratolink"
MAX_BODY = 256 * 1024
ISO = re.compile(r"\d{4}-\d\d-\d\d(T[\d:.]+Z?)?")


class HTTPError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def _one(q: dict[str, list[str]], k: str) -> str | None:
    v = (q.get(k) or [None])[0]
    return v.strip() if isinstance(v, str) and v.strip() else None


def _int(q: dict[str, list[str]], k: str, default: int | None = None) -> int | None:
    v = _one(q, k)
    if v is None:
        return default
    if not re.fullmatch(r"\d{1,12}", v):
        raise HTTPError(400, f"{k} must be a whole number")
    return int(v)


def _time(q: dict[str, list[str]], k: str) -> str | None:
    v = _one(q, k)
    if v and not ISO.fullmatch(v):
        raise HTTPError(400, f"{k} must be an ISO date or UTC time")
    return v


def _node(v: Any) -> int:
    try:
        return mt.node_num(str(v or ""))
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from None


class API:
    def __init__(self, s: cfgmod.Settings, store: Store, receiver_snapshot: Callable[[], dict[str, Any]]):
        self.s, self.store, self.snap = s, store, receiver_snapshot
        self.profiles = prof.Profiles(s.path("profiles"))
        self.prov = Provisioning(s.path("provision"), store, self.profiles)

    # ------------------------------------------------------------ GET
    def get(self, path: str, q: dict[str, list[str]]) -> tuple[int, Any, dict[str, str]]:
        st = self.store
        if path == P:
            return 200, views.status(st, self.s, self.snap()), {}
        if path == f"{P}/live":
            after = _int(q, "after", 0) or 0
            rows = st.console(after_id=after, limit=200)
            return 200, {"rx": rows[::-1], "last_rx_id": max([r["rx_id"] for r in rows], default=after),
                         "at": iso(dt.datetime.now(UTC))}, {}
        if path == f"{P}/console":
            node = _node(_one(q, "node")) if _one(q, "node") else None
            return 200, {"rx": st.console(before_id=_int(q, "before"), node=node, port=_one(q, "port"),
                                          trackers_only=_one(q, "trackers") == "1", limit=_int(q, "limit", 200))}, {}
        if path == f"{P}/track":
            node = _node(_one(q, "node"))
            flight = self._flight(_int(q, "flight")) if _one(q, "flight") else None
            if flight and flight["tracker_node"] != node:
                raise HTTPError(400, "that flight belongs to another tracker")
            return 200, views.track_view(st, self.s, node, flight, _time(q, "since"), _time(q, "until")), {}
        if m := re.fullmatch(rf"{P}/packets/(\d{{1,12}})", path):
            p = st.one("SELECT * FROM packets WHERE id = ?", (int(m.group(1)),))
            if not p:
                raise HTTPError(404, "no such packet")
            p["content"] = json.loads(p["content"]) if p.get("content") else None
            p["data"] = json.loads(p["data"]) if p.get("data") else None
            p["sender_id"], p["packet_id"] = mt.node_id(p["sender"]), f"{p['packet_id']:08x}"
            return 200, {"packet": p, "receptions": st.packet_receptions(p["id"])}, {}
        if path == f"{P}/nodes":
            return 200, {"nodes": st.nodes()}, {}
        if path == f"{P}/coverage":
            days = min(_int(q, "days", 7) or 7, 90)
            node = _node(_one(q, "node")) if _one(q, "node") else None
            since = iso(dt.datetime.now(UTC) - dt.timedelta(days=days))
            return 200, {"since": since, "hours": st.coverage(since, node)}, {}
        if path == f"{P}/flights":
            return 200, {"flights": st.flights()}, {}
        if m := re.fullmatch(rf"{P}/flights/(\d{{1,9}})", path):
            f = self._flight(int(m.group(1)))
            t = st.tracker(f["tracker_node"])
            return 200, {"flight": f, "tracker": views.tracker_view(t, st, self.s, dt.datetime.now(UTC), f)
                         if t else None}, {}
        if m := re.fullmatch(rf"{P}/flights/(\d{{1,9}})/export\.(csv|geojson|kml)", path):
            return self._export(self._flight(int(m.group(1))), m.group(2))
        if m := re.fullmatch(rf"{P}/flights/(\d{{1,9}})/preflight", path):
            return 200, views.preflight(st, self.s, self._flight(int(m.group(1)))), {}
        if path == f"{P}/profiles":
            out = {}
            for slug, p in self.profiles.list().items():
                out[slug] = {**p, "resolved": {hw: prof.resolve(p, hw) for hw in ("heltec_v4", "rak4631")},
                             "builtin": slug in prof.DEFAULT_PROFILES}
            return 200, {"profiles": out}, {}
        if path == f"{P}/hardware":
            return 200, {"hardware": prof.HARDWARE, "rules": {k: (list(v) if isinstance(v, tuple) and v and v[0]
                                                                  is not int else str(v)) for k, v in
                                                              prof.RULES.items()}}, {}
        if path == f"{P}/provision":
            bundles = self.prov.bundles()
            for b in bundles:
                if b.get("report"):
                    node = mt.node_num(b["report"]["identity"]["node_id"])
                    b["rf_test"] = self.prov.rf_test(node) if st.tracker(node) else None
            return 200, {"bundles": bundles}, {}
        if path == f"{P}/provision/tool":
            return 200, self.prov.tool(), {"Content-Type": "text/x-python",
                                           "Content-Disposition": 'attachment; filename="stratolink_provision.py"'}
        if path == f"{P}/firmware":
            return 200, {"releases": firmware.releases(self._fw_root(), verify=_one(q, "verify") == "1")}, {}
        if m := re.fullmatch(rf"{P}/firmware/([A-Za-z0-9._-]+)/(.+)", path):
            f = firmware.file_for(self._fw_root(), m.group(1), m.group(2))
            if f is None:
                raise HTTPError(404, "not an archived file, or its checksum no longer matches")
            return 200, f.read_bytes(), {"Content-Type": "application/octet-stream",
                                         "Content-Disposition": f'attachment; filename="{f.name}"'}
        raise HTTPError(404, "not found")

    def _fw_root(self):
        import pathlib
        return pathlib.Path(self.s.firmware_dir).expanduser()

    def _flight(self, fid: int | None) -> dict[str, Any]:
        f = self.store.flight(fid) if fid else None
        if not f:
            raise HTTPError(404, "no such flight")
        return f

    def _export(self, f: dict[str, Any], kind: str) -> tuple[int, Any, dict[str, str]]:
        lo, hi = self.store.window(f, None, None)
        pos = self.store.positions(f["tracker_node"], lo, hi)
        t = self.store.tracker(f["tracker_node"]) or {}
        meta = {"flight": f["name"], "tracker": t.get("name"), "node_id": mt.node_id(f["tracker_node"]),
                "mode": f["mode"], "started_utc": f["started_utc"], "ended_utc": f["ended_utc"],
                "fixes": len(pos), "source": "Stratolink (valid GPS fixes received over RF)"}
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f["name"])[:60] or "flight"
        if kind == "csv":
            body, ctype = track.to_csv(pos), "text/csv"
        elif kind == "geojson":
            body, ctype = track.to_geojson(f["name"], pos, meta), "application/geo+json"
        else:
            body, ctype = track.to_kml(f["name"], pos, meta), "application/vnd.google-earth.kml+xml"
        return 200, body.encode(), {"Content-Type": ctype,
                                    "Content-Disposition": f'attachment; filename="{safe}.{kind}"'}

    # ------------------------------------------------------------ POST
    def post(self, path: str, body: dict[str, Any], who: str) -> tuple[int, Any, dict[str, str]]:
        st = self.store
        try:
            if path == f"{P}/flights":
                f = st.create_flight(body.get("name"), _node(body.get("tracker")), body.get("mode", "flight"),
                                     body.get("notes"), body.get("loss_warn_s"), body.get("loss_lost_s"),
                                     start=bool(body.get("start")))
                LOG.info("flight %s %r created by %s", f["id"], f["name"], who)
                return 200, {"flight": f}, {}
            if m := re.fullmatch(rf"{P}/flights/(\d{{1,9}})(?:/(start|end|preflight))?", path):
                fid, action = int(m.group(1)), m.group(2)
                self._flight(fid)
                if action == "start":
                    f = st.start_flight(fid)
                elif action == "end":
                    f = st.end_flight(fid)
                elif action == "preflight":
                    item = body.get("item")
                    if item not in views.MANUAL_ITEMS:
                        raise HTTPError(400, f"only the manual items can be confirmed: {', '.join(views.MANUAL_ITEMS)}")
                    st.confirm(fid, item, bool(body.get("confirmed")), who, (body.get("note") or "")[:300] or None)
                    return 200, views.preflight(st, self.s, self._flight(fid)), {}
                else:
                    f = st.update_flight(fid, **body)
                LOG.info("flight %s %s by %s", fid, action or "updated", who)
                return 200, {"flight": f}, {}
            if path == f"{P}/trackers":
                t = st.register_tracker(str(body.get("node_id", "")), body.get("name"),
                                        **{k: body.get(k) for k in ("short_name", "hardware", "gps", "channel",
                                                                    "color", "profile", "notes")})
                LOG.info("tracker %s registered as %r by %s", t["node_id"], t["name"], who)
                return 200, {"tracker": t}, {}
            if m := re.fullmatch(rf"{P}/trackers/(!?[0-9a-fA-F]{{8}})(/key)?", path):
                node = _node(m.group(1))
                t = st.tracker(node)
                if not t:
                    raise HTTPError(404, "not a registered tracker")
                if m.group(2):
                    channel = str(body.get("channel") or t.get("channel") or "")
                    if not channel or channel == "LongFast":
                        raise HTTPError(400, "name the tracker's private channel (LongFast uses the public key)")
                    import pathlib
                    cfgmod.write_key(pathlib.Path(self.s.keys_file).expanduser(), channel, str(body.get("psk", "")))
                    st.register_tracker(t["node_id"], t["name"], channel=channel)
                    LOG.info("channel key for %s (%s) set by %s", channel, t["node_id"], who)   # never the key
                    return 200, {"tracker": st.tracker(node), "key_saved": True}, {}
                fields = {k: body[k] for k in ("short_name", "hardware", "gps", "channel", "color", "profile",
                                               "notes") if k in body}
                if "active" in body:
                    fields["active"] = int(bool(body["active"]))
                t = st.register_tracker(t["node_id"], body.get("name") or t["name"], **fields)
                return 200, {"tracker": t}, {}
            if path == f"{P}/gateways":
                g = st.register_gateway(str(body.get("eui", "")), **{k: v for k, v in body.items() if k != "eui"})
                LOG.info("gateway %s registered as %r by %s", g["eui"], g.get("name"), who)
                return 200, {"gateway": g}, {}
            if m := re.fullmatch(rf"{P}/profiles/([a-z0-9-]{{1,40}})(/reset)?", path):
                if m.group(2):
                    return 200, {"profile": self.profiles.reset(m.group(1))}, {}
                p = self.profiles.save(m.group(1), body.get("profile") or {})
                LOG.info("profile %s saved by %s", m.group(1), who)
                return 200, {"profile": p}, {}
            if path == f"{P}/provision/bundle":
                b = self.prov.bundle(body.get("name"), body.get("short_name"), body.get("hardware", "heltec_v4"),
                                     body.get("profile", "ground-test"), body.get("overrides") or None,
                                     body.get("channel") or "LongFast")
                LOG.info("provisioning bundle %s for %r by %s", b["bundle_id"], b["tracker"]["name"], who)
                return 200, b, {}
            if path == f"{P}/provision/report":
                r = self.prov.accept(body.get("report") or body)
                LOG.info("provisioning report accepted: %s %r by %s", r["tracker"]["node_id"], r["tracker"]["name"],
                         who)
                return 200, r, {}
        except (ValueError, KeyError, cfgmod.KeyFileError) as exc:
            raise HTTPError(400, str(exc).strip("'\"")) from None
        raise HTTPError(404, "not found")


def make_handler(api: API, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "starlab-stratolink/1"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOG.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: Any, headers: dict[str, str] | None = None) -> None:
            headers = dict(headers or {})
            if isinstance(body, (bytes, bytearray)):
                data = bytes(body)
                ctype = headers.pop("Content-Type", "application/octet-stream")
            else:
                data = json.dumps(body, default=str).encode()
                ctype = "application/json"
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in headers.items():
                self.send_header(k, v)
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
            try:
                code, body, headers = api.get(url.path.rstrip("/"), parse_qs(url.query))
                self._send(code, body, headers)
            except HTTPError as exc:
                self._send(exc.code, {"detail": str(exc)})
            except Exception as exc:  # noqa: BLE001
                LOG.exception("GET %s", self.path)
                self._send(500, {"detail": f"internal error: {exc}"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {"detail": "body too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
            except ValueError as exc:
                return self._send(400, {"detail": f"bad JSON: {exc}"})
            who = (self.headers.get("X-Starlab-User") or "api")[:80]
            try:
                code, out, headers = api.post(urlsplit(self.path).path.rstrip("/"), body, who)
                self._send(code, out, headers)
            except HTTPError as exc:
                self._send(exc.code, {"detail": str(exc)})
            except Exception as exc:  # noqa: BLE001
                LOG.exception("POST %s", self.path)
                self._send(500, {"detail": f"internal error: {exc}"})

    return Handler


class ControlServer:
    """Serves in a daemon thread; retries the bind every minute if the port is taken."""

    def __init__(self, api: API, token: str, host: str, port: int):
        self.api, self.token, self.host, self.port = api, token, host, port
        self.httpd: ThreadingHTTPServer | None = None
        self._stop = threading.Event()
        self.bound = threading.Event()
        self._thread = threading.Thread(target=self._run, name="stratolink-control", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.httpd = ThreadingHTTPServer((self.host, self.port), make_handler(self.api, self.token))
            except OSError as exc:
                LOG.warning("control endpoint %s:%s not available (%s); retrying in 60 s", self.host, self.port, exc)
                self._stop.wait(60)
                continue
            self.port = self.httpd.server_address[1]
            LOG.info("control endpoint on http://%s:%s%s", self.host, self.port, P)
            self.bound.set()
            self.httpd.serve_forever(poll_interval=0.5)
            return

    def stop(self) -> None:
        self._stop.set()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
