"""The Semtech UDP server: one socket on udp_host:udp_port, acknowledge first, then file.

Only one process may own the port. The socket is opened without SO_REUSEADDR/SO_REUSEPORT, so while another receiver
(the old manual receiver.py) holds it the bind fails, and this one waits and retries instead of splitting the gateway's
traffic with it. Datagrams from addresses outside allowed_sources are dropped unanswered.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import errno
import json
import logging
import pathlib
import socket
import threading
import time
from collections import deque
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import config as cfgmod
from . import meshtastic as mt
from . import semtech
from .store import Store, iso

LOG = logging.getLogger("aobs.stratolink.receiver")
UTC = dt.timezone.utc


class Receiver:
    def __init__(self, s: cfgmod.Settings, store: Store, clock: Callable[[], dt.datetime] | None = None):
        self.s, self.store = s, store
        self.clock = clock or (lambda: dt.datetime.now(UTC))
        self.sock: socket.socket | None = None
        self.state = "starting"
        self.detail = ""
        self.bound_at: str | None = None
        self.counters = {"datagrams": 0, "acks": 0, "rejected_source": 0, "bad_datagrams": 0, "rxpk": 0,
                         "stats": 0, "decoded": 0, "duplicates": 0, "errors": 0}
        self.recent_errors: deque[dict[str, Any]] = deque(maxlen=20)
        self._stop = threading.Event()
        self._channels: list[mt.Channel] = []
        self._channels_at = 0.0
        self._gw_ips: set[str] = set()
        self._gw_ips_at = 0.0
        self.thread = threading.Thread(target=self.run, name="stratolink-udp", daemon=True)

    # ------------------------------------------------------------ channels (keys file re-read every minute)
    def channels(self) -> list[mt.Channel]:
        if time.monotonic() - self._channels_at > 60 or not self._channels:
            try:
                self._channels = cfgmod.channels(self.s)
            except (cfgmod.KeyFileError, ValueError) as exc:
                LOG.error("channel keys: %s (decoding with the public channels only)", exc)
                self._channels = list(mt.DEFAULT_CHANNELS)
            self._channels_at = time.monotonic()
        return self._channels

    def allowed(self, ip: str) -> bool:
        """allowed_sources from the settings, or the address of a registered ground station (`gateway deploy` and
        `gateway register --ip` record it)."""
        if self.s.source_allowed(ip):
            return True
        if time.monotonic() - self._gw_ips_at > 60:
            self._gw_ips = {g["ip"] for g in self.store.q("SELECT ip FROM gateways WHERE registered = 1 AND ip IS NOT "
                                                          "NULL")}
            self._gw_ips_at = time.monotonic()
        return ip in self._gw_ips

    # ------------------------------------------------------------ socket
    def bind(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET6 if ":" in self.s.udp_host else socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.s.udp_host, int(self.s.udp_port)))
        except OSError:
            sock.close()
            raise
        sock.settimeout(1.0)
        return sock

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.thread.join(timeout=5)
        if self.sock:
            self.sock.close()
            self.sock = None

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sock = self.bind()
            except OSError as exc:
                self.state = "port_busy" if exc.errno == errno.EADDRINUSE else "bind_failed"
                self.detail = (f"{self.s.udp_host}:{self.s.udp_port} is held by another process (is the manual "
                               f"receiver.py still running? `ss -ulpn | grep {self.s.udp_port}`); retrying"
                               if exc.errno == errno.EADDRINUSE else f"cannot bind {self.s.udp_host}:"
                               f"{self.s.udp_port}: {exc}")
                LOG.warning(self.detail)
                self._stop.wait(self.s.rebind_retry_s)
                continue
            self.state, self.detail, self.bound_at = "listening", "", iso(self.clock())
            LOG.info("listening for Semtech UDP on %s:%s", self.s.udp_host, self.s.udp_port)
            while not self._stop.is_set():
                try:
                    data, addr = self.sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError as exc:
                    LOG.error("receive failed: %s; reopening the socket", exc)
                    break
                try:
                    self.handle(data, addr, self.sock.sendto)
                except Exception as exc:  # noqa: BLE001 - one bad datagram must never stop the receiver
                    self.counters["errors"] += 1
                    self.recent_errors.append({"at": iso(self.clock()), "error": repr(exc)[:300]})
                    LOG.exception("datagram from %s not filed", addr)
            if self.sock:
                self.sock.close()
                self.sock = None
        self.state = "stopped"

    # ------------------------------------------------------------ protocol
    def handle(self, data: bytes, addr: tuple[str, int], send: Callable[[bytes, Any], Any]) -> list[dict[str, Any]]:
        ip = addr[0]
        if not self.allowed(ip):
            self.counters["rejected_source"] += 1
            return []
        self.counters["datagrams"] += 1
        try:
            d = semtech.parse(data)
        except semtech.ProtocolError as exc:
            self.counters["bad_datagrams"] += 1
            self.recent_errors.append({"at": iso(self.clock()), "from": ip, "error": str(exc)})
            return []
        ack = d.ack()
        if ack:                                    # acknowledge before any filing, so the gateway never waits on disk
            send(ack, addr)
            self.counters["acks"] += 1
        at = self.clock()
        self.store.gateway_seen(d.eui, ip, d.kind_name, at, bool(ack))
        if d.kind != semtech.PUSH_DATA:
            return []
        if d.body_error:
            self.counters["bad_datagrams"] += 1
            self.recent_errors.append({"at": iso(at), "from": ip, "error": f"PUSH_DATA JSON: {d.body_error}"})
            return []
        if isinstance(d.body.get("stat"), dict):
            self.counters["stats"] += 1
            self.store.gateway_stat(d.eui, ip, at, semtech.stat_fields(d.body["stat"]))
        out = []
        for pk in d.body.get("rxpk") or []:
            if not isinstance(pk, dict):
                continue
            self.counters["rxpk"] += 1
            r = file_rxpk(self.store, self.s, d.eui, ip, at, pk, "live", self.channels())
            if r:
                if r.get("status") == "decoded" and not r.get("duplicate"):
                    self.counters["decoded"] += 1
                if r.get("duplicate"):
                    self.counters["duplicates"] += 1
                out.append(r)
        return out

    def snapshot(self) -> dict[str, Any]:
        return {"state": self.state, "detail": self.detail, "bound_at": self.bound_at,
                "listen": f"{self.s.udp_host}:{self.s.udp_port}", "allowed_sources": list(self.s.allowed_sources),
                "counters": dict(self.counters), "recent_errors": list(self.recent_errors)}


def file_rxpk(store: Store, s: cfgmod.Settings, eui: str, ip: str | None, at: dt.datetime, pk: dict[str, Any],
              source: str, channels: list[mt.Channel]) -> dict[str, Any] | None:
    meta = semtech.rxpk_fields(pk)
    raw: bytes | None
    try:
        raw = base64.b64decode(pk.get("data") or "", validate=True)
        if not raw:
            raise binascii.Error("empty data")
    except (binascii.Error, ValueError, TypeError) as exc:
        raw = None
        meta["_error"] = f"rxpk data is not base64: {exc}"
    return store.ingest(eui=eui, ip=ip, rx_utc=at, meta=meta, raw=raw, source=source, channels=channels,
                        max_speed_mps=s.max_speed_mps, min_alt_m=s.min_alt_m, max_alt_m=s.max_alt_m,
                        foreign_decimals=s.foreign_position_decimals)


# ------------------------------------------------------------------ importing the manual receiver's logs
def import_log(store: Store, s: cfgmod.Settings, path: pathlib.Path, tz: str | None = None) -> dict[str, int]:
    """packets.jsonl from ~/stratolink/receiver: {"timestamp": naive local ISO, "gateway": EUI, "packet": rxpk,
    "hex": frame}. Each line is filed like a live reception with source "import"; one already filed (the same
    gateway, concentrator counter and frame) is skipped, so importing again adds nothing."""
    zone = ZoneInfo(tz or s.import_timezone)
    lines = added = skipped = errors = 0
    channels = cfgmod.channels(s)
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            lines += 1
            try:
                rec = json.loads(line)
                t = dt.datetime.fromisoformat(rec["timestamp"])
                t = (t.replace(tzinfo=zone) if t.tzinfo is None else t).astimezone(UTC)
                pk = dict(rec["packet"])
                if rec.get("hex") and not pk.get("data"):
                    pk["data"] = base64.b64encode(bytes.fromhex(rec["hex"])).decode()
                r = file_rxpk(store, s, str(rec.get("gateway") or "UNKNOWN").upper(), None, t, pk, "import", channels)
            except (KeyError, ValueError, TypeError) as exc:
                errors += 1
                LOG.warning("%s line %d not imported: %s", path, lines, exc)
                continue
            if r is None:
                skipped += 1
            else:
                added += 1
    store.import_record(str(path), lines, added, skipped, errors)
    return {"lines": lines, "added": added, "skipped": skipped, "errors": errors}
