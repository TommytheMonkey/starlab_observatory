"""Stratolink's archive: <data_dir>/stratolink.sqlite3 (WAL), one writer (the receiver) and the control endpoint.

rx         every reception by every ground station: radio metadata (RSSI, SNR, frequency, CRC...) and the frame
packets    one row per Meshtastic message (sender, packet id, encrypted-body hash): retransmissions and the same frame
           heard by several gateways are receptions of one packet, so statistics stay per reception
positions  GPS fixes from registered trackers only (valid, or rejected with the reason)
telemetry  telemetry from registered trackers
nodes      every node heard (discovery); other people's nodes keep a ~1 km position, never a track
gateways, gateway_stats   ground stations and their Semtech status reports
trackers, flights, preflight
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
import threading
from typing import Any, Iterable

from . import meshtastic as mt
from . import track

UTC = dt.timezone.utc
SCHEMA = """
CREATE TABLE IF NOT EXISTS gateways (
  eui TEXT PRIMARY KEY, name TEXT, registered INTEGER NOT NULL DEFAULT 0,
  lat REAL, lon REAL, alt_m REAL, ip TEXT, mgmt_ip TEXT, hardware TEXT, firmware TEXT, radio TEXT, notes TEXT,
  first_seen TEXT, last_seen TEXT, last_pull TEXT, last_push TEXT, last_stat TEXT, last_ip TEXT,
  datagrams INTEGER NOT NULL DEFAULT 0, acks_sent INTEGER NOT NULL DEFAULT 0, created_utc TEXT
);
CREATE TABLE IF NOT EXISTS gateway_stats (
  id INTEGER PRIMARY KEY, eui TEXT NOT NULL, rx_utc TEXT NOT NULL, ip TEXT, gw_time TEXT,
  lati REAL, long REAL, alti REAL, rxnb INTEGER, rxok INTEGER, rxfw INTEGER, ackr REAL, dwnb INTEGER, txnb INTEGER,
  temp REAL
);
CREATE INDEX IF NOT EXISTS gateway_stats_eui ON gateway_stats (eui, rx_utc);
CREATE TABLE IF NOT EXISTS packets (
  id INTEGER PRIMARY KEY, sender INTEGER NOT NULL, packet_id INTEGER NOT NULL, body_sha TEXT NOT NULL,
  dest INTEGER, channel_hash INTEGER, channel TEXT, channel_public INTEGER, flags INTEGER, hop_start INTEGER,
  want_ack INTEGER, via_mqtt INTEGER, status TEXT NOT NULL, portnum INTEGER, port TEXT, content TEXT, data TEXT,
  error TEXT, size INTEGER, is_tracker INTEGER NOT NULL DEFAULT 0,
  first_rx_utc TEXT NOT NULL, last_rx_utc TEXT NOT NULL, rx_count INTEGER NOT NULL DEFAULT 1,
  gateway_count INTEGER NOT NULL DEFAULT 1, best_rssi REAL, best_snr REAL, min_hops INTEGER,
  UNIQUE (sender, packet_id, body_sha)
);
CREATE INDEX IF NOT EXISTS packets_sender ON packets (sender, first_rx_utc);
CREATE INDEX IF NOT EXISTS packets_first ON packets (first_rx_utc);
CREATE TABLE IF NOT EXISTS rx (
  id INTEGER PRIMARY KEY, rx_utc TEXT NOT NULL, eui TEXT NOT NULL, ip TEXT, rx_key TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL, tmst INTEGER, gw_time TEXT, freq_mhz REAL, chan INTEGER, rfch INTEGER, modu TEXT, datr TEXT,
  sf INTEGER, bw_khz INTEGER, codr TEXT, rssi REAL, rssis REAL, snr REAL, foff_hz REAL, crc INTEGER, size INTEGER,
  raw_hex TEXT, status TEXT NOT NULL, error TEXT, packet_ref INTEGER, duplicate INTEGER NOT NULL DEFAULT 0,
  hop_limit INTEGER, relay_node INTEGER, sender INTEGER
);
CREATE INDEX IF NOT EXISTS rx_time ON rx (rx_utc);
CREATE INDEX IF NOT EXISTS rx_sender ON rx (sender, rx_utc);
CREATE INDEX IF NOT EXISTS rx_packet ON rx (packet_ref);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY, packet_ref INTEGER NOT NULL UNIQUE, node INTEGER NOT NULL, rx_utc TEXT NOT NULL,
  gps_utc TEXT, lat REAL, lon REAL, alt_m REAL, speed_mps REAL, heading_deg REAL, sats INTEGER, pdop REAL,
  precision_bits INTEGER, valid INTEGER NOT NULL, reject_reason TEXT
);
CREATE INDEX IF NOT EXISTS positions_node ON positions (node, rx_utc);
CREATE TABLE IF NOT EXISTS telemetry (
  id INTEGER PRIMARY KEY, packet_ref INTEGER NOT NULL UNIQUE, node INTEGER NOT NULL, rx_utc TEXT NOT NULL,
  kind TEXT, battery_level INTEGER, voltage REAL, channel_utilization REAL, air_util_tx REAL, uptime_s INTEGER,
  temperature REAL, humidity REAL, pressure REAL, data TEXT
);
CREATE INDEX IF NOT EXISTS telemetry_node ON telemetry (node, rx_utc);
CREATE TABLE IF NOT EXISTS nodes (
  node INTEGER PRIMARY KEY, node_id TEXT NOT NULL, long_name TEXT, short_name TEXT, hw_model TEXT, role TEXT,
  has_public_key INTEGER, first_heard TEXT NOT NULL, last_heard TEXT NOT NULL, packets INTEGER NOT NULL DEFAULT 0,
  last_port TEXT, coarse_lat REAL, coarse_lon REAL, last_rssi REAL, last_snr REAL, channel TEXT
);
CREATE TABLE IF NOT EXISTS trackers (
  node INTEGER PRIMARY KEY, node_id TEXT NOT NULL, name TEXT NOT NULL, short_name TEXT, hardware TEXT, gps TEXT,
  channel TEXT, color TEXT, profile TEXT, notes TEXT, active INTEGER NOT NULL DEFAULT 1, registered_utc TEXT,
  provision TEXT
);
CREATE TABLE IF NOT EXISTS flights (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, tracker_node INTEGER NOT NULL, mode TEXT NOT NULL,
  status TEXT NOT NULL, created_utc TEXT NOT NULL, started_utc TEXT, ended_utc TEXT, notes TEXT,
  loss_warn_s REAL, loss_lost_s REAL
);
CREATE TABLE IF NOT EXISTS preflight (
  flight_id INTEGER NOT NULL, item TEXT NOT NULL, confirmed INTEGER NOT NULL, by_who TEXT, at_utc TEXT, note TEXT,
  PRIMARY KEY (flight_id, item)
);
CREATE TABLE IF NOT EXISTS imports (
  path TEXT PRIMARY KEY, at_utc TEXT, lines INTEGER, added INTEGER, skipped INTEGER, errors INTEGER
);
"""
FLIGHT_MODES = ("flight", "ground_test")


def iso(t: dt.datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z") if t else None


def parse_t(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def now() -> dt.datetime:
    return dt.datetime.now(UTC)


def _j(v: Any) -> str | None:
    return json.dumps(v, separators=(",", ":"), default=str) if v is not None else None


def rx_key(eui: str, tmst: Any, raw_hex: str, rx_utc: str) -> str:
    """Identity of one reception, the same whether it arrives live or from an imported log: the gateway, its
    concentrator counter and the frame (the counter alone wraps every 71 minutes; with the frame it cannot repeat).
    Without a counter the reception time stands in."""
    basis = f"{eui}|{tmst}|{raw_hex}" if tmst is not None else f"{eui}|t={rx_utc}|{raw_hex}"
    return hashlib.sha1(basis.encode()).hexdigest()


class Store:
    def __init__(self, path: pathlib.Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, tuple(args))]

    def one(self, sql: str, args: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    # ------------------------------------------------------------ gateways
    def gateway_seen(self, eui: str, ip: str, kind: str, at: dt.datetime, acked: bool) -> None:
        t = iso(at)
        col = {"PUSH_DATA": "last_push", "PULL_DATA": "last_pull"}.get(kind)
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO gateways (eui, first_seen, created_utc) VALUES (?, ?, ?)",
                            (eui, t, t))
            self.db.execute(f"UPDATE gateways SET last_seen = ?, last_ip = ?, datagrams = datagrams + 1, "
                            f"acks_sent = acks_sent + ?{', ' + col + ' = ?' if col else ''} WHERE eui = ?",
                            (t, ip, int(acked), *((t,) if col else ()), eui))

    def gateway_stat(self, eui: str, ip: str, at: dt.datetime, st: dict[str, Any]) -> int:
        t = iso(at)
        with self.lock, self.db:
            self.db.execute("UPDATE gateways SET last_stat = ? WHERE eui = ?", (t, eui))
            cur = self.db.execute(
                "INSERT INTO gateway_stats (eui, rx_utc, ip, gw_time, lati, long, alti, rxnb, rxok, rxfw, ackr, dwnb,"
                " txnb, temp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eui, t, ip, st.get("gw_time"), st.get("lati"), st.get("long"), st.get("alti"), st.get("rxnb"),
                 st.get("rxok"), st.get("rxfw"), st.get("ackr"), st.get("dwnb"), st.get("txnb"), st.get("temp")))
            return int(cur.lastrowid)

    def register_gateway(self, eui: str, **f: Any) -> dict[str, Any]:
        eui = eui.strip().upper()
        if len(eui) != 16 or any(c not in "0123456789ABCDEF" for c in eui):
            raise ValueError("a gateway EUI is 16 hex digits")
        allowed = ("name", "lat", "lon", "alt_m", "ip", "mgmt_ip", "hardware", "firmware", "radio", "notes")
        vals = {k: (_j(v) if k == "radio" and isinstance(v, dict) else v) for k, v in f.items() if k in allowed}
        for k in ("lat", "lon", "alt_m"):
            if vals.get(k) is not None:
                vals[k] = float(vals[k])
        if vals.get("lat") is not None and not -90 <= vals["lat"] <= 90:
            raise ValueError("latitude must be -90..90")
        if vals.get("lon") is not None and not -180 <= vals["lon"] <= 180:
            raise ValueError("longitude must be -180..180")
        t = iso(now())
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO gateways (eui, created_utc) VALUES (?, ?)", (eui, t))
            sets = ", ".join(f"{k} = ?" for k in vals)
            self.db.execute(f"UPDATE gateways SET registered = 1{', ' + sets if sets else ''} WHERE eui = ?",
                            (*vals.values(), eui))
        return self.gateway(eui) or {}

    def gateway(self, eui: str) -> dict[str, Any] | None:
        g = self.one("SELECT * FROM gateways WHERE eui = ?", (eui,))
        if g and g.get("radio"):
            g["radio"] = json.loads(g["radio"])
        return g

    def gateways(self) -> list[dict[str, Any]]:
        out = []
        for g in self.q("SELECT * FROM gateways ORDER BY registered DESC, name, eui"):
            g["radio"] = json.loads(g["radio"]) if g.get("radio") else None
            g["last_stats"] = self.one("SELECT * FROM gateway_stats WHERE eui = ? ORDER BY id DESC LIMIT 1",
                                       (g["eui"],))
            out.append(g)
        return out

    # ------------------------------------------------------------ trackers
    def trackers(self, active_only: bool = False) -> list[dict[str, Any]]:
        rows = self.q("SELECT * FROM trackers" + (" WHERE active = 1" if active_only else "") + " ORDER BY name")
        for r in rows:
            r["provision"] = json.loads(r["provision"]) if r.get("provision") else None
        return rows

    def tracker(self, node: int) -> dict[str, Any] | None:
        r = self.one("SELECT * FROM trackers WHERE node = ?", (node,))
        if r:
            r["provision"] = json.loads(r["provision"]) if r.get("provision") else None
        return r

    def tracker_nodes(self) -> set[int]:
        return {r["node"] for r in self.q("SELECT node FROM trackers WHERE active = 1")}

    def register_tracker(self, node_id: str, name: str, *, seed: bool = False, **f: Any) -> dict[str, Any]:
        node = mt.node_num(node_id)
        name = (name or "").strip()
        if not name or len(name) > 40:
            raise ValueError("a tracker needs a name of 1-40 characters")
        allowed = ("short_name", "hardware", "gps", "channel", "color", "profile", "notes", "provision", "active")
        vals = {k: (_j(v) if k == "provision" and isinstance(v, dict) else v) for k, v in f.items()
                if k in allowed and v is not None}
        if vals.get("short_name") and len(vals["short_name"]) > 4:
            raise ValueError("a Meshtastic short name is at most 4 characters")
        with self.lock, self.db:
            exists = self.db.execute("SELECT 1 FROM trackers WHERE node = ?", (node,)).fetchone()
            if exists and seed:
                return self.tracker(node) or {}
            if exists:
                sets = ", ".join(f"{k} = ?" for k in vals)
                self.db.execute(f"UPDATE trackers SET name = ?{', ' + sets if sets else ''} WHERE node = ?",
                                (name, *vals.values(), node))
            else:
                cols = ["node", "node_id", "name", "registered_utc", *vals]
                self.db.execute(f"INSERT INTO trackers ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                                (node, mt.node_id(node), name, iso(now()), *vals.values()))
            # its packets so far become tracker packets; positions from before registration were never kept
            self.db.execute("UPDATE packets SET is_tracker = 1 WHERE sender = ?", (node,))
        return self.tracker(node) or {}

    # ------------------------------------------------------------ ingest
    def ingest(self, *, eui: str, ip: str | None, rx_utc: dt.datetime, meta: dict[str, Any], raw: bytes | None,
               source: str, channels: list[mt.Channel], max_speed_mps: float = 150.0, min_alt_m: float = -500,
               max_alt_m: float = 60_000, foreign_decimals: int = 2) -> dict[str, Any] | None:
        """File one reception. Returns the live-feed summary, or None when this exact reception is already filed (an
        imported log line the receiver also heard, or a log imported twice)."""
        t = iso(rx_utc)
        raw_hex = raw.hex() if raw is not None else ""
        key = rx_key(eui, meta.get("tmst"), raw_hex, t)
        trackers = self.tracker_nodes()
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM rx WHERE rx_key = ?", (key,)).fetchone():
                return None
            status, error, dec = "received", None, None
            if raw is None:
                status, error = "malformed", meta.pop("_error", "no frame")
            elif meta.get("crc") == -1:
                status, error = "crc_error", "CRC failed at the gateway"
            elif meta.get("crc") == 0:
                status, error = "no_crc", "frame has no CRC (not a Meshtastic frame)"
            else:
                dec = mt.decode(raw, channels)
                status, error = dec.status, dec.error
            if meta.get("size") is not None and raw is not None and meta["size"] != len(raw):
                error = (error + "; " if error else "") + f"size field {meta['size']} != frame {len(raw)} bytes"
            h = dec.header if dec and status != "malformed" else None
            sender = h.sender if h else None
            cur = self.db.execute(
                "INSERT INTO rx (rx_utc, eui, ip, rx_key, source, tmst, gw_time, freq_mhz, chan, rfch, modu, datr, sf,"
                " bw_khz, codr, rssi, rssis, snr, foff_hz, crc, size, raw_hex, status, error, hop_limit, relay_node,"
                " sender) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t, eui, ip, key, source, meta.get("tmst"), meta.get("gw_time"), meta.get("freq_mhz"),
                 meta.get("chan"), meta.get("rfch"), meta.get("modu"), meta.get("datr"), meta.get("sf"),
                 meta.get("bw_khz"), meta.get("codr"), meta.get("rssi"), meta.get("rssis"), meta.get("snr"),
                 meta.get("foff_hz"), meta.get("crc"), len(raw) if raw is not None else meta.get("size"),
                 raw_hex or None, status, error, h.hop_limit if h else None, h.relay_node if h else None, sender))
            rx_id = int(cur.lastrowid)
            out: dict[str, Any] = {"rx_id": rx_id, "rx_utc": t, "eui": eui, "status": status, "error": error,
                                   "rssi": meta.get("rssi"), "snr": meta.get("snr"), "freq_mhz": meta.get("freq_mhz"),
                                   "datr": meta.get("datr"), "size": len(raw) if raw is not None else None}
            if h is None:
                return out
            is_tracker = sender in trackers
            body_sha = hashlib.sha1(raw[mt.HEADER_LEN:]).hexdigest()[:20]
            content = dict(dec.content) if dec.status == "decoded" else {}
            if dec.port == "POSITION_APP" and not is_tracker:
                content = _coarse(content, foreign_decimals)
            prev = self.db.execute("SELECT * FROM packets WHERE sender = ? AND packet_id = ? AND body_sha = ?",
                                   (sender, h.packet_id, body_sha)).fetchone()
            hops = h.hops_away
            if prev is None:
                pc = self.db.execute(
                    "INSERT INTO packets (sender, packet_id, body_sha, dest, channel_hash, channel, channel_public, "
                    "flags, hop_start, want_ack, via_mqtt, status, portnum, port, content, data, error, size, "
                    "is_tracker, first_rx_utc, last_rx_utc, best_rssi, best_snr, min_hops) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sender, h.packet_id, body_sha, h.dest, h.channel_hash, dec.channel,
                     None if dec.channel_public is None else int(dec.channel_public), h.flags, h.hop_start,
                     int(h.want_ack), int(h.via_mqtt), dec.status, dec.portnum, dec.port, _j(content) if content
                     else None, _j(dec.data) if dec.data else None, dec.error, len(raw), int(is_tracker), t, t,
                     meta.get("rssi"), meta.get("snr"), hops))
                pref, duplicate = int(pc.lastrowid), False
            else:
                pref, duplicate = int(prev["id"]), True
                gws = self.db.execute("SELECT COUNT(DISTINCT eui) FROM rx WHERE packet_ref = ? OR id = ?",
                                      (pref, rx_id)).fetchone()[0]
                self.db.execute(
                    "UPDATE packets SET rx_count = rx_count + 1, last_rx_utc = MAX(last_rx_utc, ?), "
                    "first_rx_utc = MIN(first_rx_utc, ?), gateway_count = ?, "
                    "best_rssi = MAX(COALESCE(best_rssi, -999), COALESCE(?, -999)), "
                    "best_snr = MAX(COALESCE(best_snr, -999), COALESCE(?, -999)), "
                    "min_hops = CASE WHEN ? IS NULL THEN min_hops WHEN min_hops IS NULL OR ? < min_hops THEN ? "
                    "ELSE min_hops END WHERE id = ?",
                    (t, t, gws, meta.get("rssi"), meta.get("snr"), hops, hops, hops, pref))
            self.db.execute("UPDATE rx SET packet_ref = ?, duplicate = ? WHERE id = ?", (pref, int(duplicate), rx_id))
            self._node_heard(sender, t, dec, content, meta, is_tracker, foreign_decimals)
            out.update({"packet_ref": pref, "duplicate": duplicate, "sender": mt.node_id(sender),
                        "packet_id": f"{h.packet_id:08x}", "port": dec.port, "channel": dec.channel,
                        "hops_away": hops, "hop_limit": h.hop_limit, "hop_start": h.hop_start,
                        "relay_node": h.relay_node, "is_tracker": is_tracker,
                        "content": content if (is_tracker or dec.port != "POSITION_APP") else _coarse(content, 2)})
            if duplicate or dec.status != "decoded" or not is_tracker:
                return out
            if dec.port == "POSITION_APP":
                out["position"] = self._position(pref, sender, rx_utc, content, max_speed_mps, min_alt_m, max_alt_m)
            elif dec.port == "TELEMETRY_APP":
                self._telemetry(pref, sender, t, content)
            elif dec.port == "NODEINFO_APP":
                self.db.execute("UPDATE trackers SET short_name = COALESCE(short_name, ?), "
                                "hardware = COALESCE(hardware, ?) WHERE node = ?",
                                (content.get("short_name"), content.get("hw_model"), sender))
            return out

    def _node_heard(self, node: int, t: str, dec: mt.Decoded, content: dict[str, Any], meta: dict[str, Any],
                    is_tracker: bool, decimals: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO nodes (node, node_id, first_heard, last_heard) VALUES (?, ?, ?, ?)",
                        (node, mt.node_id(node), t, t))
        self.db.execute("UPDATE nodes SET last_heard = MAX(last_heard, ?), packets = packets + 1, "
                        "last_port = COALESCE(?, last_port), last_rssi = ?, last_snr = ?, "
                        "channel = COALESCE(?, channel) WHERE node = ?",
                        (t, dec.port, meta.get("rssi"), meta.get("snr"), dec.channel, node))
        if dec.port == "NODEINFO_APP" and content:
            self.db.execute("UPDATE nodes SET long_name = ?, short_name = ?, hw_model = ?, role = ?, "
                            "has_public_key = ? WHERE node = ?",
                            (content.get("long_name"), content.get("short_name"), content.get("hw_model"),
                             content.get("role"), int(bool(content.get("has_public_key"))), node))
        if dec.port == "POSITION_APP" and content.get("lat") is not None and content.get("lon") is not None:
            c = _coarse(content, decimals)
            self.db.execute("UPDATE nodes SET coarse_lat = ?, coarse_lon = ? WHERE node = ?",
                            (c.get("lat"), c.get("lon"), node))

    def _position(self, pref: int, node: int, rx_utc: dt.datetime, c: dict[str, Any], max_speed: float,
                  min_alt: float, max_alt: float) -> dict[str, Any]:
        last = self.db.execute("SELECT * FROM positions WHERE node = ? AND valid = 1 AND rx_utc <= ? "
                               "ORDER BY rx_utc DESC LIMIT 1", (node, iso(rx_utc))).fetchone()
        last_rej = self.db.execute("SELECT * FROM positions WHERE node = ? AND rx_utc <= ? ORDER BY rx_utc DESC "
                                   "LIMIT 1", (node, iso(rx_utc))).fetchone()
        ok, reason = track.validate(c, rx_utc, dict(last) if last else None,
                                    dict(last_rej) if last_rej and not last_rej["valid"] else None,
                                    max_speed, min_alt, max_alt)
        gps_utc = iso(dt.datetime.fromtimestamp(c["gps_time"], UTC)) if c.get("gps_time") else None
        row = {"packet_ref": pref, "node": node, "rx_utc": iso(rx_utc), "gps_utc": gps_utc, "lat": c.get("lat"),
               "lon": c.get("lon"), "alt_m": c.get("alt_m"), "speed_mps": c.get("speed_mps"),
               "heading_deg": c.get("heading_deg"), "sats": c.get("sats_in_view"), "pdop": c.get("pdop"),
               "precision_bits": c.get("precision_bits"), "valid": int(ok), "reject_reason": reason}
        self.db.execute(f"INSERT OR IGNORE INTO positions ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                        tuple(row.values()))
        return row

    def _telemetry(self, pref: int, node: int, t: str, c: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO telemetry (packet_ref, node, rx_utc, kind, battery_level, voltage, "
            "channel_utilization, air_util_tx, uptime_s, temperature, humidity, pressure, data) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pref, node, t, c.get("kind"), c.get("battery_level"), c.get("voltage"), c.get("channel_utilization"),
             c.get("air_util_tx"), c.get("uptime_seconds"), c.get("temperature"), c.get("relative_humidity"),
             c.get("barometric_pressure"), _j(c)))

    def purge_foreign_raw(self, older_than: dt.datetime) -> int:
        """Drop the frames of other people's packets after the retention window (their decoded summaries, which keep
        only a coarse position, stay)."""
        with self.lock, self.db:
            cur = self.db.execute(
                "UPDATE rx SET raw_hex = NULL WHERE raw_hex IS NOT NULL AND rx_utc < ? AND "
                "(sender IS NULL OR sender NOT IN (SELECT node FROM trackers))", (iso(older_than),))
            return cur.rowcount

    # ------------------------------------------------------------ flights
    def create_flight(self, name: str, tracker_node: int, mode: str = "flight", notes: str | None = None,
                      loss_warn_s: float | None = None, loss_lost_s: float | None = None,
                      start: bool = False) -> dict[str, Any]:
        name = (name or "").strip()
        if not name or len(name) > 80:
            raise ValueError("a flight needs a name of 1-80 characters")
        if mode not in FLIGHT_MODES:
            raise ValueError(f"mode must be one of {FLIGHT_MODES}")
        if not self.tracker(tracker_node):
            raise ValueError(f"{mt.node_id(tracker_node)} is not a registered tracker")
        _check_thresholds(loss_warn_s, loss_lost_s)
        t = iso(now())
        with self.lock, self.db:
            cur = self.db.execute("INSERT INTO flights (name, tracker_node, mode, status, created_utc, notes, "
                                  "loss_warn_s, loss_lost_s) VALUES (?,?,?,?,?,?,?,?)",
                                  (name, tracker_node, mode, "planned", t, notes, loss_warn_s, loss_lost_s))
            fid = int(cur.lastrowid)
        if start:
            return self.start_flight(fid)
        return self.flight(fid) or {}

    def flight(self, fid: int) -> dict[str, Any] | None:
        return self.one("SELECT * FROM flights WHERE id = ?", (fid,))

    def flights(self) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM flights ORDER BY COALESCE(started_utc, created_utc) DESC")

    def active_flights(self) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM flights WHERE status = 'active' ORDER BY started_utc")

    def start_flight(self, fid: int) -> dict[str, Any]:
        f = self.flight(fid)
        if not f:
            raise ValueError("no such flight")
        if f["status"] != "planned":
            raise ValueError(f"flight is {f['status']}; only a planned flight can start")
        with self.lock, self.db:
            other = self.db.execute("SELECT name FROM flights WHERE status = 'active' AND tracker_node = ?",
                                    (f["tracker_node"],)).fetchone()
            if other:
                raise ValueError(f"{mt.node_id(f['tracker_node'])} is already recording flight {other[0]!r}")
            self.db.execute("UPDATE flights SET status = 'active', started_utc = ? WHERE id = ?", (iso(now()), fid))
        return self.flight(fid) or {}

    def end_flight(self, fid: int) -> dict[str, Any]:
        f = self.flight(fid)
        if not f:
            raise ValueError("no such flight")
        if f["status"] != "active":
            raise ValueError(f"flight is {f['status']}; only an active flight can end")
        with self.lock, self.db:
            self.db.execute("UPDATE flights SET status = 'ended', ended_utc = ? WHERE id = ?", (iso(now()), fid))
        return self.flight(fid) or {}

    def update_flight(self, fid: int, **f: Any) -> dict[str, Any]:
        cur = self.flight(fid)
        if not cur:
            raise ValueError("no such flight")
        vals = {k: v for k, v in f.items() if k in ("name", "notes", "loss_warn_s", "loss_lost_s", "mode")}
        if "mode" in vals and vals["mode"] not in FLIGHT_MODES:
            raise ValueError(f"mode must be one of {FLIGHT_MODES}")
        if "name" in vals and not str(vals["name"]).strip():
            raise ValueError("a flight needs a name")
        _check_thresholds(vals.get("loss_warn_s", cur["loss_warn_s"]), vals.get("loss_lost_s", cur["loss_lost_s"]))
        if vals:
            with self.lock, self.db:
                self.db.execute(f"UPDATE flights SET {', '.join(k + ' = ?' for k in vals)} WHERE id = ?",
                                (*vals.values(), fid))
        return self.flight(fid) or {}

    def confirm(self, fid: int, item: str, confirmed: bool, by: str, note: str | None = None) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO preflight (flight_id, item, confirmed, by_who, at_utc, note) "
                            "VALUES (?,?,?,?,?,?)", (fid, item, int(confirmed), by, iso(now()), note))

    def confirmations(self, fid: int) -> dict[str, dict[str, Any]]:
        return {r["item"]: r for r in self.q("SELECT * FROM preflight WHERE flight_id = ?", (fid,))}

    # ------------------------------------------------------------ reads for the page
    def window(self, flight: dict[str, Any] | None, since: str | None, until: str | None) -> tuple[str, str]:
        lo = since or (flight or {}).get("started_utc") or "0000"
        hi = until or (flight or {}).get("ended_utc") or "9999"
        return lo, hi

    def positions(self, node: int, since: str = "0000", until: str = "9999", valid_only: bool = True,
                  limit: int = 50_000) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM positions WHERE node = ? AND rx_utc >= ? AND rx_utc <= ?"
                      + (" AND valid = 1" if valid_only else "") + " ORDER BY rx_utc LIMIT ?",
                      (node, since, until, limit))

    def telemetry_rows(self, node: int, since: str = "0000", until: str = "9999",
                       limit: int = 50_000) -> list[dict[str, Any]]:
        return self.q("SELECT id, rx_utc, kind, battery_level, voltage, channel_utilization, air_util_tx, uptime_s, "
                      "temperature, humidity, pressure FROM telemetry WHERE node = ? AND rx_utc >= ? AND rx_utc <= ? "
                      "ORDER BY rx_utc LIMIT ?", (node, since, until, limit))

    def receptions(self, node: int, since: str = "0000", until: str = "9999",
                   limit: int = 50_000) -> list[dict[str, Any]]:
        return self.q("SELECT r.id, r.rx_utc, r.eui, r.rssi, r.snr, r.duplicate, p.port, r.hop_limit "
                      "FROM rx r LEFT JOIN packets p ON p.id = r.packet_ref WHERE r.sender = ? AND r.rx_utc >= ? AND "
                      "r.rx_utc <= ? ORDER BY r.rx_utc LIMIT ?", (node, since, until, limit))

    def console(self, after_id: int = 0, before_id: int | None = None, node: int | None = None,
                port: str | None = None, trackers_only: bool = False, limit: int = 200) -> list[dict[str, Any]]:
        sql = ("SELECT r.id AS rx_id, r.rx_utc, r.eui, g.name AS gateway, r.rssi, r.snr, r.freq_mhz, r.datr, r.size, "
               "r.status AS rx_status, r.error AS rx_error, r.duplicate, r.hop_limit, r.relay_node, r.sender, "
               "p.id AS packet_ref, p.packet_id, p.dest, p.channel, p.channel_public, p.hop_start, p.port, "
               "p.content, p.status, p.error, p.is_tracker, p.rx_count, p.gateway_count, p.want_ack, "
               "pos.valid AS pos_valid, pos.reject_reason "
               "FROM rx r LEFT JOIN packets p ON p.id = r.packet_ref LEFT JOIN gateways g ON g.eui = r.eui "
               "LEFT JOIN positions pos ON pos.packet_ref = p.id AND r.duplicate = 0 WHERE r.id > ?")
        args: list[Any] = [after_id]
        if before_id:
            sql += " AND r.id < ?"
            args.append(before_id)
        if node is not None:
            sql += " AND r.sender = ?"
            args.append(node)
        if port:
            sql += " AND p.port = ?"
            args.append(port)
        if trackers_only:
            sql += " AND p.is_tracker = 1"
        sql += " ORDER BY r.id DESC LIMIT ?"
        args.append(max(1, min(1000, limit)))
        rows = self.q(sql, args)
        for r in rows:
            r["content"] = json.loads(r["content"]) if r.get("content") else None
            r["sender_id"] = mt.node_id(r["sender"]) if r.get("sender") is not None else None
            r["packet_id"] = f"{r['packet_id']:08x}" if r.get("packet_id") is not None else None
            r["dest"] = mt.node_id(r["dest"]) if r.get("dest") is not None else None
            if r.get("hop_start") and r.get("hop_limit") is not None and r["hop_start"] >= r["hop_limit"]:
                r["hops_away"] = r["hop_start"] - r["hop_limit"]
        return rows

    def nodes(self, limit: int = 500) -> list[dict[str, Any]]:
        return self.q("SELECT n.*, (n.node IN (SELECT node FROM trackers)) AS registered FROM nodes n "
                      "ORDER BY last_heard DESC LIMIT ?", (limit,))

    def counts(self, node: int | None = None, since: str = "0000", until: str = "9999") -> dict[str, Any]:
        w, a = "WHERE rx_utc >= ? AND rx_utc <= ?", [since, until]
        if node is not None:
            w += " AND sender = ?"
            a.append(node)
        r = self.one(f"SELECT COUNT(*) AS receptions, SUM(duplicate = 0 AND packet_ref IS NOT NULL) AS packets, "
                     f"SUM(duplicate) AS duplicates, MAX(rx_utc) AS last_rx FROM rx {w}", a) or {}
        return {k: (v or 0) if k != "last_rx" else v for k, v in r.items()}

    def coverage(self, since: str, node: int | None = None) -> list[dict[str, Any]]:
        """Receptions per gateway per hour (and per tracker when node is given)."""
        sql = ("SELECT eui, substr(rx_utc, 1, 13) AS hour, COUNT(*) AS receptions, AVG(rssi) AS rssi, "
               "AVG(snr) AS snr, MAX(rssi) AS best_rssi FROM rx WHERE rx_utc >= ? AND packet_ref IS NOT NULL")
        args: list[Any] = [since]
        if node is not None:
            sql += " AND sender = ?"
            args.append(node)
        return self.q(sql + " GROUP BY eui, hour ORDER BY hour", args)

    def packet_receptions(self, packet_ref: int) -> list[dict[str, Any]]:
        return self.q("SELECT r.id, r.rx_utc, r.eui, g.name AS gateway, r.rssi, r.snr, r.hop_limit, r.relay_node, "
                      "r.duplicate FROM rx r LEFT JOIN gateways g ON g.eui = r.eui WHERE r.packet_ref = ? "
                      "ORDER BY r.rx_utc", (packet_ref,))

    def import_record(self, path: str, lines: int, added: int, skipped: int, errors: int) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO imports (path, at_utc, lines, added, skipped, errors) "
                            "VALUES (?,?,?,?,?,?)", (path, iso(now()), lines, added, skipped, errors))


def _coarse(c: dict[str, Any], decimals: int) -> dict[str, Any]:
    out = {k: v for k, v in c.items() if k in ("lat", "lon", "precision_bits", "location_source")}
    for k in ("lat", "lon"):
        if out.get(k) is not None:
            out[k] = round(out[k], decimals)
    out["coarse"] = True
    return out


def _check_thresholds(warn: Any, lost: Any) -> None:
    if warn is None and lost is None:
        return
    if warn is None or lost is None:
        raise ValueError("set both loss-of-contact thresholds, or neither (the defaults)")
    if not 0 < float(warn) < float(lost):
        raise ValueError("the warning threshold must be positive and below the lost threshold")
