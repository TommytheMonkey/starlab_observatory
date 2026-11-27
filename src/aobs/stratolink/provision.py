"""Tracker provisioning on the server side: bundles out, reports in.

A bundle is what the provisioning tool on your computer applies: tracker name, the profile's settings resolved for the
hardware and validated, and a one-off test message. The tool sends back a report (what it read from the device, every
setting read back, the GPS fix, the test packet id). Accepting a report registers the tracker under the node ID the
device itself reported, and the RF check passes only when a ground station actually decodes the test message.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
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
    def __init__(self, root: pathlib.Path, store: Store, profiles: prof.Profiles,
                 keys_file: pathlib.Path | None = None, private_channel: str = "Stratolink"):
        self.root, self.store, self.profiles = root, store, profiles
        self.keys_file, self.private_channel = keys_file, private_channel
        root.mkdir(parents=True, exist_ok=True)

    # ---- the optional private channel (one for all Stratolink trackers; its key lives only in the keys file)
    def private_channel_info(self) -> dict[str, Any]:
        from . import config as cfgmod
        keys = cfgmod.read_keys(self.keys_file) if self.keys_file else {}
        psk = keys.get(self.private_channel)
        out: dict[str, Any] = {"name": self.private_channel, "exists": bool(psk), "slot": prof.US_LONG_FAST_SLOT}
        if psk:
            ch = mt.Channel.from_psk(self.private_channel, psk, public=False)
            out.update(hash=f"0x{ch.hash:02x}", key_bits=len(ch.key or b"") * 8,
                       fingerprint=hashlib.sha256(base64.b64decode(psk)).hexdigest()[:12])
        return out

    def create_private_channel(self) -> dict[str, Any]:
        """Make the channel key once (AES-256, from the OS random source). Refuses to replace an existing one: every
        tracker already on it would go dark."""
        from . import config as cfgmod
        if not self.keys_file:
            raise ValueError("no keys file configured")
        if self.private_channel_info()["exists"]:
            raise ValueError(f"the {self.private_channel} channel already has a key; it is never replaced from here")
        cfgmod.write_key(self.keys_file, self.private_channel, base64.b64encode(secrets.token_bytes(32)).decode())
        return self.private_channel_info()

    def bundle(self, name: str, short_name: str, hardware: str, profile: str,
               overrides: dict[str, Any] | None = None, channel: str = "LongFast", channel_mode: str = "public",
               keep_public_secondary: bool = False) -> dict[str, Any]:
        name, short_name = (name or "").strip(), (short_name or "").strip()
        if not name or len(name) > 39:
            raise ValueError("tracker name: 1-39 characters (Meshtastic's long-name limit)")
        if not short_name or len(short_name.encode()) > 4:
            raise ValueError("short name: 1-4 characters")
        if hardware not in prof.HARDWARE:
            raise ValueError(f"unknown hardware {hardware!r}")
        p = self.profiles.get(profile)
        r = prof.resolve(p, hardware, overrides, channel_mode, keep_public_secondary)
        if r["errors"]:
            raise ValueError("; ".join(r["errors"]))
        chan: dict[str, Any] = {"mode": "public", "name": "LongFast"}
        if channel_mode == "private":
            info = self.private_channel_info()
            if not info["exists"]:
                raise ValueError(f"create the {self.private_channel} channel key first")
            from . import config as cfgmod
            chan = {"mode": "private", "name": self.private_channel, "slot": prof.US_LONG_FAST_SLOT,
                    "psk": cfgmod.read_keys(self.keys_file)[self.private_channel],
                    "keep_public_secondary": bool(keep_public_secondary), "fingerprint": info["fingerprint"]}
            channel = self.private_channel
        # re-provisioning a registered tracker (e.g. onto the private channel) keeps its name; the report must then
        # come from that same node
        same = next((t for t in self.store.trackers() if t["name"].lower() == name.lower()), None)
        bid = secrets.token_hex(8)
        b = {"kind": "stratolink-provision-bundle", "version": 1, "bundle_id": bid, "created_utc": iso(dt.datetime.now(
             UTC)), "tracker": {"name": name, "short_name": short_name, "hardware": hardware},
             "profile": {"id": profile, "name": p["name"]}, "channel": channel, "channel_config": chan,
             "settings": r["settings"], "firmware_effect": r["firmware_effect"],
             "firmware_rules_version": r["firmware_rules_version"],
             "warnings": r["warnings"], "estimate": r["estimate"],
             "hardware": {k: v for k, v in r["hardware"].items() if k != "power"},
             "test_text": f"STRATOLINK TEST {name} {bid[:6]}",
             "reprovision_of": same["node_id"] if same else None}
        path = self.root / f"{bid}.bundle.json"
        path.write_text(json.dumps(b, indent=2) + "\n")
        if chan.get("psk"):
            path.chmod(0o600)                      # it holds the private channel key
        return b

    def bundles(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.root.glob("*.bundle.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            b = json.loads(p.read_text())
            if (b.get("channel_config") or {}).get("psk"):
                b["channel_config"] = {**b["channel_config"], "psk": "(in the bundle file only)"}
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
        if b.get("reprovision_of") and mt.node_id(node) != b["reprovision_of"]:
            raise ValueError(f"{b['tracker']['name']} is {b['reprovision_of']}; this report is from {mt.node_id(node)} "
                             "(a different board): give the new one its own name")
        existing = self.store.tracker(node)
        if existing and existing["name"] != b["tracker"]["name"]:
            raise ValueError(f"{mt.node_id(node)} is already registered as {existing['name']}")
        (self.root / f"{bid}.report.json").write_text(json.dumps(report, indent=2) + "\n")
        requested = b.get("settings") or {}
        persisted = report.get("applied") or {}
        explained = {k: {"requested": requested.get(k), "persisted": persisted.get(k),
                         "rule": (b.get("firmware_effect") or {}).get(k, {}).get("rule")}
                     for k in requested if k in persisted and persisted.get(k) != requested.get(k)}
        provision = {"bundle_id": bid, "profile": b["profile"], "requested": requested, "applied": persisted,
                     "persisted_differs": explained, "channel": {k: v for k, v in (b.get("channel_config") or {}).items()
                                                                 if k != "psk"},
                     "firmware_rules_version": b.get("firmware_rules_version"),
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
