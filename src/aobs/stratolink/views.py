"""What the Mission Control page shows, assembled from the store. Every figure says where it came from; nothing is
filled in when the data is absent (None stays None, and the page shows "—")."""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

from . import config as cfgmod
from . import meshtastic as mt
from . import track
from .store import Store, iso, parse_t

UTC = dt.timezone.utc
DEFAULT_WINDOW_H = 24


def _age(t: str | None, now: dt.datetime) -> float | None:
    return (now - parse_t(t)).total_seconds() if t else None


def gateway_view(g: dict[str, Any], store: Store, s: cfgmod.Settings, now: dt.datetime) -> dict[str, Any]:
    beat = max([t for t in (g.get("last_pull"), g.get("last_stat"), g.get("last_push")) if t], default=None)
    age = _age(beat, now)
    st = g.get("last_stats") or {}
    tot = store.one("SELECT SUM(rxnb) AS rxnb, SUM(rxok) AS rxok, SUM(rxfw) AS rxfw, COUNT(*) AS reports, "
                    "MIN(rx_utc) AS since FROM gateway_stats WHERE eui = ?", (g["eui"],)) or {}
    ours = store.one("SELECT COUNT(*) AS receptions, SUM(crc = 1) AS crc_ok, SUM(status = 'decoded') AS decoded, "
                     "MAX(rx_utc) AS last_rx FROM rx WHERE eui = ?", (g["eui"],)) or {}
    last = store.one("SELECT freq_mhz, datr, sf, bw_khz, codr FROM rx WHERE eui = ? ORDER BY id DESC LIMIT 1",
                     (g["eui"],))
    return {
        "eui": g["eui"], "name": g.get("name") or g["eui"], "registered": bool(g.get("registered")),
        "online": age is not None and age <= s.gateway_offline_s, "last_heartbeat": beat, "heartbeat_age_s": age,
        "last_stat": g.get("last_stat"), "last_pull": g.get("last_pull"), "ip": g.get("last_ip") or g.get("ip"),
        "mgmt_ip": g.get("mgmt_ip"), "lat": g.get("lat"), "lon": g.get("lon"), "alt_m": g.get("alt_m"),
        "hardware": g.get("hardware"), "firmware": g.get("firmware"), "notes": g.get("notes"),
        # the last status report (each covers the gateway's stat_interval, normally 30 s)
        "temp_c": st.get("temp"), "ack_pct": st.get("ackr"), "rxnb": st.get("rxnb"), "rxok": st.get("rxok"),
        "rxfw": st.get("rxfw"), "stat_gw_time": st.get("gw_time"),
        "gps": ({"lat": st.get("lati"), "lon": st.get("long"), "alt_m": st.get("alti")}
                if st.get("lati") not in (None, 0) else None),
        # totals over every report this server has filed
        "totals": {"rxnb": tot.get("rxnb"), "rxok": tot.get("rxok"), "rxfw": tot.get("rxfw"),
                   "reports": tot.get("reports") or 0, "since": tot.get("since")},
        "received": {"receptions": ours.get("receptions") or 0, "crc_ok": ours.get("crc_ok") or 0,
                     "decoded": ours.get("decoded") or 0, "last_rx": ours.get("last_rx")},
        "datagrams": g.get("datagrams"), "acks_sent": g.get("acks_sent"),
        "radio_configured": g.get("radio"),
        "radio_observed": ({"freq_mhz": last["freq_mhz"], "datr": last["datr"], "sf": last["sf"],
                            "bw_khz": last["bw_khz"], "codr": last["codr"]} if last else None),
    }


def station_for(store: Store, node: int | None = None) -> dict[str, Any] | None:
    """The ground station distances are measured from: the located gateway that heard this tracker last, else the
    first located registered gateway."""
    if node is not None:
        g = store.one("SELECT g.* FROM rx r JOIN gateways g ON g.eui = r.eui WHERE r.sender = ? AND g.lat IS NOT NULL "
                      "ORDER BY r.id DESC LIMIT 1", (node,))
        if g:
            return g
    return store.one("SELECT * FROM gateways WHERE registered = 1 AND lat IS NOT NULL ORDER BY created_utc LIMIT 1")


def observed_interval(times: list[str]) -> float | None:
    if len(times) < 3:
        return None
    ts = [parse_t(t) for t in times]
    gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:]) if (b - a).total_seconds() > 1]
    return statistics.median(gaps) if gaps else None


def contact_state(age: float | None, warn: float, lost: float) -> str:
    if age is None:
        return "never"
    return "ok" if age < warn else "late" if age < lost else "lost"


def tracker_view(t: dict[str, Any], store: Store, s: cfgmod.Settings, now: dt.datetime,
                 flight: dict[str, Any] | None = None) -> dict[str, Any]:
    node = t["node"]
    lo, hi = store.window(flight, None, None)
    last_rx = store.one("SELECT r.rx_utc, r.rssi, r.snr, r.eui, p.port FROM rx r LEFT JOIN packets p ON "
                        "p.id = r.packet_ref WHERE r.sender = ? ORDER BY r.rx_utc DESC LIMIT 1", (node,))
    fix = store.one("SELECT * FROM positions WHERE node = ? AND valid = 1 ORDER BY rx_utc DESC LIMIT 1", (node,))
    batt = store.one("SELECT * FROM telemetry WHERE node = ? AND kind = 'device' ORDER BY rx_utc DESC LIMIT 1",
                     (node,))
    env = store.one("SELECT * FROM telemetry WHERE node = ? AND kind = 'environment' ORDER BY rx_utc DESC LIMIT 1",
                    (node,))
    rejected = store.one("SELECT COUNT(*) AS n FROM positions WHERE node = ? AND valid = 0 AND rx_utc >= ? AND "
                         "rx_utc <= ?", (node, lo, hi)) or {}
    recent = [r["rx_utc"] for r in store.q("SELECT rx_utc FROM positions WHERE node = ? AND valid = 1 ORDER BY "
                                           "rx_utc DESC LIMIT 12", (node,))][::-1]
    warn = (flight or {}).get("loss_warn_s") or s.loss_warn_s
    lost = (flight or {}).get("loss_lost_s") or s.loss_lost_s
    age = _age(last_rx["rx_utc"] if last_rx else None, now)
    station = station_for(store, node)
    window_positions = store.positions(node, lo, hi) if flight else []
    st = track.stats(window_positions, station) if flight else None
    prov = t.get("provision") or {}
    return {
        **{k: t.get(k) for k in ("node_id", "name", "short_name", "hardware", "gps", "channel", "color", "profile",
                                 "notes", "active", "registered_utc")},
        "node": node,
        "last_contact": last_rx["rx_utc"] if last_rx else None, "last_contact_age_s": age,
        "last_contact_port": last_rx["port"] if last_rx else None,
        "contact": contact_state(age, warn, lost), "loss_warn_s": warn, "loss_lost_s": lost,
        "rssi": last_rx["rssi"] if last_rx else None, "snr": last_rx["snr"] if last_rx else None,
        "last_gateway": last_rx["eui"] if last_rx else None,
        "last_fix": fix, "last_fix_age_s": _age(fix["rx_utc"] if fix else None, now),
        "battery": ({"level": batt["battery_level"], "external_power": batt["battery_level"] == 101,
                     "voltage": batt["voltage"], "at": batt["rx_utc"], "channel_utilization":
                     batt["channel_utilization"], "air_util_tx": batt["air_util_tx"], "uptime_s": batt["uptime_s"]}
                    if batt else None),
        "environment": ({"temperature": env["temperature"], "humidity": env["humidity"], "pressure": env["pressure"],
                         "at": env["rx_utc"]} if env else None),
        "counts": store.counts(node, lo, hi), "counts_all": store.counts(node),
        "rejected_fixes": rejected.get("n") or 0,
        "observed_position_interval_s": observed_interval(recent),
        "configured_position_interval_s": (prov.get("applied") or {}).get("position.position_broadcast_secs"),
        "look": track.look(station, fix) if station and fix else None,
        "flight_stats": st,
    }


def status(store: Store, s: cfgmod.Settings, receiver: dict[str, Any], now: dt.datetime | None = None
           ) -> dict[str, Any]:
    now = now or dt.datetime.now(UTC)
    active = {f["tracker_node"]: f for f in store.active_flights()}
    last = store.one("SELECT MAX(id) AS id FROM rx") or {}
    return {
        "at": iso(now), "receiver": receiver, "last_rx_id": last.get("id") or 0,
        "gateways": [gateway_view(g, store, s, now) for g in store.gateways()],
        "trackers": [tracker_view(t, store, s, now, active.get(t["node"])) for t in store.trackers()],
        "active_flights": list(active.values()),
        "totals": store.counts(),
        "defaults": {"loss_warn_s": s.loss_warn_s, "loss_lost_s": s.loss_lost_s,
                     "gateway_offline_s": s.gateway_offline_s},
    }


def track_view(store: Store, s: cfgmod.Settings, node: int, flight: dict[str, Any] | None, since: str | None,
               until: str | None, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(UTC)
    if flight is None and since is None:
        since = iso(now - dt.timedelta(hours=DEFAULT_WINDOW_H))
    lo, hi = store.window(flight, since, until)
    pos = store.positions(node, lo, hi)
    station = station_for(store, node)
    return {"node": node, "node_id": mt.node_id(node), "since": lo, "until": hi, "flight": flight,
            "positions": pos,
            "rejected": [p for p in store.positions(node, lo, hi, valid_only=False) if not p["valid"]],
            "stats": track.stats(pos, station), "station": station,
            "telemetry": store.telemetry_rows(node, lo, hi), "receptions": store.receptions(node, lo, hi)}


# ------------------------------------------------------------------ preflight
MANUAL_ITEMS = {
    "airspace": "Airspace reviewed (FAA 14 CFR Part 101 for unmanned free balloons: payload weight/density limits, "
                "NOTAM/ATC notice where required, launch-site airspace class)",
    "launch": "Launch site permission, weather and recovery plan reviewed",
    "radio": "Radio compliance reviewed (US915 ISM band, Part 15 device limits; Part 97 rules if operating under an "
             "amateur licence; encryption is not permitted on amateur frequencies)",
}


def preflight(store: Store, s: cfgmod.Settings, flight: dict[str, Any], now: dt.datetime | None = None
              ) -> dict[str, Any]:
    now = now or dt.datetime.now(UTC)
    node = flight["tracker_node"]
    t = store.tracker(node)
    tv = tracker_view(t, store, s, now, flight if flight["status"] == "active" else None) if t else None
    gws = [gateway_view(g, store, s, now) for g in store.gateways()]
    checks: list[dict[str, Any]] = []

    def auto(item: str, label: str, ok: bool | None, detail: str) -> None:
        checks.append({"item": item, "label": label, "kind": "verified", "ok": ok, "detail": detail})

    auto("tracker_registered", "Tracker registered", bool(t),
         f"{t['name']} ({t['node_id']})" if t else f"{mt.node_id(node)} is not registered")
    fix = tv["last_fix"] if tv else None
    fix_age = tv["last_fix_age_s"] if tv else None
    sats = fix.get("sats") if fix else None
    auto("gps_fix", "GPS fix acquired",
         bool(fix and fix_age is not None and fix_age < 900),
         (f"valid fix {fix_age:.0f} s ago, {sats if sats is not None else 'unknown'} satellites, "
          f"precision {fix.get('precision_bits')} bits") if fix else "no valid GPS fix received yet")
    interval = (tv or {}).get("observed_position_interval_s") or 0
    recent_limit = max(300.0, 2.5 * interval)
    auto("gps_over_rf", "Recent GPS packet decoded over RF", bool(fix and fix_age is not None
                                                                   and fix_age < recent_limit),
         f"last POSITION_APP decoded {fix_age:.0f} s ago (limit {recent_limit:.0f} s)" if fix
         else "no POSITION_APP packet from this tracker")
    b = (tv or {}).get("battery")
    auto("battery", "Battery voltage available", bool(b and b.get("voltage")),
         (f"{b['voltage']:.2f} V, {'external power' if b['external_power'] else str(b['level']) + ' %'}, "
          f"reported {_age(b['at'], now):.0f} s ago") if b and b.get("voltage") else
         "no device telemetry with a voltage received")
    conf, obs = (tv or {}).get("configured_position_interval_s"), (tv or {}).get("observed_position_interval_s")
    auto("position_interval", "Position reporting interval", obs is not None,
         (f"observed {obs:.0f} s between fixes" if obs else "not enough fixes to measure the interval")
         + (f"; configured {conf} s (provisioning report)" if conf else "; configured value unknown (no "
            "provisioning report)"))
    online = [g for g in gws if g["online"]]
    auto("ground_station", "Ground station online", bool(online),
         ", ".join(f"{g['name']} (heartbeat {g['heartbeat_age_s']:.0f} s ago)" for g in online)
         or "no ground station has checked in recently")
    age = (tv or {}).get("last_contact_age_s")
    auto("rf_reception", "RF packet reception verified", age is not None and age < recent_limit,
         f"last packet {age:.0f} s ago via {(tv or {}).get('last_gateway')}, RSSI {tv.get('rssi')} dBm, "
         f"SNR {tv.get('snr')} dB" if age is not None and tv else "nothing received from this tracker")
    auto("recording", "Flight recording active", flight["status"] == "active",
         f"recording since {flight['started_utc']}" if flight["status"] == "active" else
         f"flight is {flight['status']}")
    auto("last_position", "Last known position available", bool(fix),
         f"{fix['lat']:.5f}, {fix['lon']:.5f}, {fix.get('alt_m')} m at {fix['rx_utc']}" if fix else "none")
    conf_rows = store.confirmations(flight["id"])
    for item, label in MANUAL_ITEMS.items():
        c = conf_rows.get(item)
        checks.append({"item": item, "label": label, "kind": "manual", "ok": bool(c and c["confirmed"]),
                       "detail": (f"confirmed by {c['by_who']} at {c['at_utc']}" + (f": {c['note']}" if c.get("note")
                                                                                   else ""))
                       if c and c["confirmed"] else "not confirmed"})
    verified = [c for c in checks if c["kind"] == "verified"]
    return {"flight": flight, "checks": checks, "at": iso(now),
            "verified_ok": sum(1 for c in verified if c["ok"]), "verified_total": len(verified),
            "manual_ok": sum(1 for c in checks if c["kind"] == "manual" and c["ok"]),
            "manual_total": len(MANUAL_ITEMS), "go": all(c["ok"] for c in checks)}
