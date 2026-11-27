"""Tracker provisioning on the server side: bundles out, reports in.

A bundle is what the provisioning tool on your computer applies: tracker name, the profile's settings resolved for the
hardware and validated, and a one-off test message. The tool sends back a report (what it read from the device, every
setting read back, the GPS fix, the test packet id). Accepting a report registers the tracker under the node ID the
device itself reported, and the RF check passes only when a ground station actually decodes the test message.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import secrets
from typing import Any

from . import meshtastic as mt
from . import profiles as prof
from .store import Store, iso

UTC = dt.timezone.utc
TOOL = pathlib.Path(__file__).resolve().parents[3] / "deploy" / "stratolink" / "tracker" / "stratolink_provision.py"


class Provisioning:
    def __init__(self, root: pathlib.Path, store: Store, profiles: prof.Profiles):
        self.root, self.store, self.profiles = root, store, profiles
        root.mkdir(parents=True, exist_ok=True)

    def bundle(self, name: str, short_name: str, hardware: str, profile: str,
               overrides: dict[str, Any] | None = None, channel: str = "LongFast") -> dict[str, Any]:
        name, short_name = (name or "").strip(), (short_name or "").strip()
        if not name or len(name) > 39:
            raise ValueError("tracker name: 1-39 characters (Meshtastic's long-name limit)")
        if not short_name or len(short_name.encode()) > 4:
            raise ValueError("short name: 1-4 characters")
        if hardware not in prof.HARDWARE:
            raise ValueError(f"unknown hardware {hardware!r}")
        p = self.profiles.get(profile)
        r = prof.resolve(p, hardware, overrides)
        if r["errors"]:
            raise ValueError("; ".join(r["errors"]))
        for t in self.store.trackers():
            if t["name"].lower() == name.lower():
                raise ValueError(f"{name} is already the name of {t['node_id']}")
        bid = secrets.token_hex(8)
        b = {"kind": "stratolink-provision-bundle", "version": 1, "bundle_id": bid, "created_utc": iso(dt.datetime.now(
             UTC)), "tracker": {"name": name, "short_name": short_name, "hardware": hardware},
             "profile": {"id": profile, "name": p["name"]}, "channel": channel, "settings": r["settings"],
             "warnings": r["warnings"], "estimate": r["estimate"],
             "hardware": {k: v for k, v in r["hardware"].items() if k != "power"},
             "test_text": f"STRATOLINK TEST {name} {bid[:6]}"}
        (self.root / f"{bid}.bundle.json").write_text(json.dumps(b, indent=2) + "\n")
        return b

    def bundles(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.root.glob("*.bundle.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            b = json.loads(p.read_text())
            rp = self.root / f"{b['bundle_id']}.report.json"
            b["report"] = json.loads(rp.read_text()) if rp.exists() else None
            out.append(b)
        return out

    def accept(self, report: dict[str, Any]) -> dict[str, Any]:
        if report.get("kind") != "stratolink-provision-report":
            raise ValueError("not a Stratolink provisioning report")
        bid = str(report.get("bundle_id") or "")
        if not re.fullmatch(r"[0-9a-f]{16}", bid) or not (self.root / f"{bid}.bundle.json").exists():
            raise ValueError("this report is for a bundle this observatory did not make")
        b = json.loads((self.root / f"{bid}.bundle.json").read_text())
        ident = report.get("identity") or {}
        if report.get("status") not in ("ok", "settings_mismatch"):
            raise ValueError(f"the tool stopped before configuring the tracker ({report.get('status')}); fix that and "
                             "run it again")
        node = mt.node_num(str(ident.get("node_id", "")))
        hw = prof.HARDWARE[b["tracker"]["hardware"]]
        if ident.get("hw_model") not in hw["hw_models"]:
            raise ValueError(f"the device reported {ident.get('hw_model')}, not {hw['name']}")
        if (report.get("identity_after") or {}).get("node_id", ident.get("node_id")) != ident.get("node_id"):
            raise ValueError("the node ID changed during provisioning (a factory reset?): run the tool again")
        existing = self.store.tracker(node)
        if existing and existing["name"] != b["tracker"]["name"]:
            raise ValueError(f"{mt.node_id(node)} is already registered as {existing['name']}")
        (self.root / f"{bid}.report.json").write_text(json.dumps(report, indent=2) + "\n")
        provision = {"bundle_id": bid, "profile": b["profile"], "applied": report.get("applied"),
                     "mismatches": report.get("mismatches"), "firmware": ident.get("firmware_version"),
                     "hw_model": ident.get("hw_model"), "gps": report.get("gps"), "test": report.get("test"),
                     "reported_utc": report.get("finished_utc"), "tool_version": report.get("tool_version")}
        t = self.store.register_tracker(mt.node_id(node), b["tracker"]["name"],
                                        short_name=b["tracker"]["short_name"], hardware=b["tracker"]["hardware"],
                                        profile=b["profile"]["id"], channel=b.get("channel"), provision=provision,
                                        gps=hw.get("gps"))
        return {"tracker": t, "rf_test": self.rf_test(node)}

    def rf_test(self, node: int) -> dict[str, Any]:
        """Did a ground station decode the provisioning test message? Matched by packet id, else by text."""
        t = self.store.tracker(node)
        test = ((t or {}).get("provision") or {}).get("test") or {}
        if not test:
            return {"state": "no_test", "detail": "the report has no test transmission"}
        row = None
        if test.get("packet_id"):
            row = self.store.one("SELECT * FROM packets WHERE sender = ? AND packet_id = ?",
                                 (node, int(test["packet_id"], 16)))
        if row is None and test.get("text"):
            for r in self.store.q("SELECT * FROM packets WHERE sender = ? AND port = 'TEXT_MESSAGE_APP' AND "
                                  "first_rx_utc >= ? ORDER BY id", (node, test.get("sent_utc", "")[:19])):
                if (json.loads(r["content"] or "{}")).get("text") == test["text"]:
                    row = r
                    break
        if row is None:
            return {"state": "waiting", "detail": f"no ground station has decoded {test.get('text')!r} yet",
                    "test": test}
        rx = self.store.packet_receptions(row["id"])
        return {"state": "verified", "detail": f"decoded by {len({r['eui'] for r in rx})} ground station(s), best "
                                               f"RSSI {row['best_rssi']} dBm, SNR {row['best_snr']} dB",
                "test": test, "receptions": rx, "first_rx_utc": row["first_rx_utc"]}

    def tool(self) -> bytes:
        return TOOL.read_bytes()
