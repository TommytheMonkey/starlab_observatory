"""python -m aobs.stratolink {run,status,import,decode,packets,tracker,gateway,flight,firmware}"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import sys

from . import config, gwprov
from . import meshtastic as mt
from .store import Store

UTC = dt.timezone.utc


def _store(s: config.Settings) -> Store:
    return Store(s.db_path)


def cmd_run(s: config.Settings, a: argparse.Namespace) -> int:
    from .service import main
    return main(s)


def cmd_status(s: config.Settings, a: argparse.Namespace) -> int:
    from . import views
    try:
        rx = json.loads(s.path("status.json").read_text())
    except (OSError, ValueError):
        rx = {"state": "not running (no status.json)"}
    st = views.status(_store(s), s, rx)
    if a.json:
        print(json.dumps(st, indent=2, default=str))
        return 0
    age = (dt.datetime.now(UTC) - dt.datetime.fromisoformat(rx["at"].replace("Z", "+00:00"))).total_seconds() \
        if rx.get("at") else None
    print(f"receiver: {rx.get('state')}{' (stale: is starlab-stratolink running?)' if age and age > 30 else ''} "
          f"{rx.get('detail') or ''}  {rx.get('listen', '')}  {rx.get('counters', '')}")
    for g in st["gateways"]:
        print(f"gateway {g['name']} [{g['eui']}] {'ONLINE' if g['online'] else 'offline'} heartbeat "
              f"{g['last_heartbeat']} temp {g['temp_c']} ack {g['ack_pct']}% from {g['ip']}")
    for t in st["trackers"]:
        f = t["last_fix"]
        print(f"tracker {t['name']} {t['node_id']} contact {t['contact']} last {t['last_contact']} RSSI {t['rssi']} "
              f"SNR {t['snr']} fix {(f['lat'], f['lon'], f['alt_m']) if f else None} battery {t['battery']}")
    print(f"totals: {st['totals']}")
    return 0


def cmd_import(s: config.Settings, a: argparse.Namespace) -> int:
    from .receiver import import_log
    st = _store(s)
    for p in a.paths or s.import_paths:
        print(p, import_log(st, s, pathlib.Path(p).expanduser(), a.timezone))
    return 0


def cmd_decode(s: config.Settings, a: argparse.Namespace) -> int:
    d = mt.decode(bytes.fromhex(a.hex), config.channels(s))
    print(json.dumps(d.public(), indent=2, default=lambda b: b.hex() if isinstance(b, bytes) else str(b)))
    return 0 if d.status == "decoded" else 1


def cmd_packets(s: config.Settings, a: argparse.Namespace) -> int:
    rows = _store(s).console(limit=a.limit, node=mt.node_num(a.node) if a.node else None)
    for r in rows[::-1]:
        print(f"{r['rx_utc']} {r['eui']} rssi={r['rssi']} snr={r['snr']} {r['rx_status']:<9} "
              f"{r.get('sender_id') or '-':<10} {r.get('packet_id') or '-':<8} {r.get('port') or '':<17} "
              f"{'dup ' if r['duplicate'] else ''}{json.dumps(r['content']) if r.get('content') else r.get('rx_error') or ''}")
    return 0


def cmd_tracker(s: config.Settings, a: argparse.Namespace) -> int:
    st = _store(s)
    if a.action == "list":
        for t in st.trackers():
            print(f"{t['node_id']} {t['name']!r} {t.get('short_name') or ''} {t.get('hardware') or ''} "
                  f"{'active' if t['active'] else 'retired'} profile={t.get('profile')}")
        return 0
    t = st.register_tracker(a.node_id, a.name, short_name=a.short_name, hardware=a.hardware, gps=a.gps,
                            channel=a.channel)
    print(json.dumps(t, indent=2))
    return 0


def cmd_flight(s: config.Settings, a: argparse.Namespace) -> int:
    st = _store(s)
    if a.action == "list":
        for f in st.flights():
            print(f"{f['id']:>4} {f['status']:<8} {f['mode']:<11} {mt.node_id(f['tracker_node'])} {f['name']!r} "
                  f"{f['started_utc'] or ''} - {f['ended_utc'] or ''}")
        return 0
    if a.action == "create":
        f = st.create_flight(a.name, mt.node_num(a.tracker), a.mode, start=a.start)
    elif a.action == "start":
        f = st.start_flight(a.id)
    else:
        f = st.end_flight(a.id)
    print(json.dumps(f, indent=2))
    return 0


def cmd_gateway(s: config.Settings, a: argparse.Namespace) -> int:
    gwprov.env_password_refused()
    st = _store(s)
    if a.action == "register":
        g = st.register_gateway(a.eui, name=a.name, lat=a.lat, lon=a.lon, alt_m=a.alt, ip=a.ip, mgmt_ip=a.mgmt_ip,
                                notes=a.notes)
        print(json.dumps(g, indent=2, default=str))
        return 0
    if a.action == "verify":
        since = dt.datetime.now(UTC) - dt.timedelta(seconds=a.lookback)
        gwprov.verify(st, a.target.upper(), since, rf_timeout=a.timeout)
        return 0
    gw = gwprov.Gateway(a.target, user=a.user)
    if a.action == "trust":
        print("Installing the Stratolink SSH key; ssh will ask for the gateway's root password.")
        print(gwprov.trust(gw))
    elif a.action == "inspect":
        print(json.dumps(gw.inspect(), indent=2))
    elif a.action == "capture":
        print(json.dumps(gwprov.capture(gw, s), indent=2))
    elif a.action == "deploy":
        r = gwprov.deploy(gw, s, st, a.name, a.lat, a.lon, a.alt, server=a.server, dry_run=a.dry_run)
        if not a.dry_run:
            gwprov.verify(st, r["eui"], dt.datetime.fromisoformat(r["started"].replace("Z", "+00:00")),
                          rf_timeout=a.timeout)
        else:
            print(json.dumps(r["config"], indent=2))
    elif a.action == "rollback":
        print(gwprov.rollback(gw))
    elif a.action == "flash":
        gwprov.flash(gw, s, a.release)
    return 0


def cmd_firmware(s: config.Settings, a: argparse.Namespace) -> int:
    from . import firmware
    for r in firmware.releases(pathlib.Path(s.firmware_dir).expanduser(), verify=True):
        print(f"{r['id']}: {r['title']}")
        for f in r["files"]:
            print(f"  {'ok ' if f.get('verified') else 'BAD' if f['present'] else 'MISSING'} {f['sha256'][:16]} "
                  f"{f['path']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m aobs.stratolink", description="Stratolink balloon tracking")
    ap.add_argument("--config", type=pathlib.Path)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="the daemon (systemd: starlab-stratolink)").set_defaults(fn=cmd_run)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("import", help="import packets.jsonl logs (idempotent)")
    p.add_argument("paths", nargs="*")
    p.add_argument("--timezone", help="zone of the log's naive timestamps (default import_timezone)")
    p.set_defaults(fn=cmd_import)
    p = sub.add_parser("decode", help="decode one frame given as hex")
    p.add_argument("hex")
    p.set_defaults(fn=cmd_decode)
    p = sub.add_parser("packets", help="recent receptions")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--node")
    p.set_defaults(fn=cmd_packets)
    p = sub.add_parser("tracker")
    p.add_argument("action", choices=["list", "register"])
    p.add_argument("node_id", nargs="?")
    p.add_argument("name", nargs="?")
    p.add_argument("--short-name")
    p.add_argument("--hardware")
    p.add_argument("--gps")
    p.add_argument("--channel")
    p.set_defaults(fn=cmd_tracker)
    p = sub.add_parser("flight")
    p.add_argument("action", choices=["list", "create", "start", "end"])
    p.add_argument("--name")
    p.add_argument("--tracker")
    p.add_argument("--mode", default="flight", choices=["flight", "ground_test"])
    p.add_argument("--start", action="store_true")
    p.add_argument("--id", type=int)
    p.set_defaults(fn=cmd_flight)
    p = sub.add_parser("gateway", help="ground stations: trust, inspect, capture, deploy, rollback, verify, flash, "
                                       "register")
    p.add_argument("action", choices=["trust", "inspect", "capture", "deploy", "rollback", "verify", "flash",
                                      "register"])
    p.add_argument("target", nargs="?", help="gateway address (verify/register: its EUI)")
    p.add_argument("--user", default="root")
    p.add_argument("--name")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--alt", type=float)
    p.add_argument("--eui")
    p.add_argument("--ip")
    p.add_argument("--mgmt-ip")
    p.add_argument("--notes")
    p.add_argument("--server", help="observatory address the gateway forwards to (default udp_host)")
    p.add_argument("--release", help="flash: archived firmware release id")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--timeout", type=float, default=600, help="seconds to wait for an RF packet")
    p.add_argument("--lookback", type=float, default=0, help="verify: also accept packets this many seconds old")
    p.set_defaults(fn=cmd_gateway)
    sub.add_parser("firmware", help="archived ground-station firmware, checksums verified").set_defaults(
        fn=cmd_firmware)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = config.load(a.config)
    if a.cmd == "gateway":
        if a.action == "register":
            a.eui = a.eui or a.target
            if not a.eui:
                ap.error("gateway register EUI --name ...")
        elif a.action == "deploy" and (not a.name or a.lat is None or a.lon is None):
            ap.error("gateway deploy HOST --name NAME --lat LAT --lon LON [--alt M]")
        elif a.action == "flash" and not a.release:
            ap.error("gateway flash HOST --release RELEASE (see `firmware`)")
        elif not a.target:
            ap.error(f"gateway {a.action} needs a target")
    if a.cmd == "tracker" and a.action == "register" and not (a.node_id and a.name):
        ap.error("tracker register !NODEID NAME")
    try:
        return a.fn(s, a)
    except (ValueError, gwprov.GatewayError, config.KeyFileError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
