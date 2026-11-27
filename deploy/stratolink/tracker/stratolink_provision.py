#!/usr/bin/env python3
"""Stratolink tracker provisioning, run on the computer the tracker is plugged into (a Mac, usually: the observatory
cannot reach your USB ports).

    python3 -m venv ~/.stratolink && ~/.stratolink/bin/pip install "meshtastic>=2.7"
    ~/.stratolink/bin/python stratolink_provision.py detect
    ~/.stratolink/bin/python stratolink_provision.py provision stratolink-02.bundle.json
    -> stratolink-02.report.json  (upload it on Starlab's Stratolink > Trackers > Provision page)

The bundle comes from Starlab (Trackers > Provision a tracker): the tracker's name, the chosen profile's settings,
already merged with the hardware's overrides and validated, and a one-off test message.

What it does: finds the board on USB, reads its node ID, hardware model and firmware version, refuses hardware the
bundle was not made for and firmware older than the bundle's minimum (it points you to the official web flasher; it
never flashes by itself), shows every setting it will change, applies them in one settings transaction, reconnects
after the reboot and reads every setting back, waits for a GPS fix, and sends the test message.

It never reads, copies or writes the device's private key or channel keys other than position precision on the
primary channel: each tracker keeps the identity its own firmware generated.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time
from typing import Any

TOOL_VERSION = "1.0"
UTC = dt.timezone.utc


def need_meshtastic():
    try:
        import meshtastic  # noqa: F401
        import meshtastic.serial_interface  # noqa: F401
        from meshtastic.protobuf import mesh_pb2  # noqa: F401
    except ImportError:
        sys.exit('This needs the meshtastic package:  python3 -m venv ~/.stratolink && '
                 '~/.stratolink/bin/pip install "meshtastic>=2.7"')


def vtuple(v: str | None) -> tuple[int, ...]:
    out = []
    for part in (v or "0").split(".")[:3]:
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits or 0))
    return tuple(out)


# ------------------------------------------------------------------ USB detection
def ports() -> list[dict[str, Any]]:
    from serial.tools import list_ports
    out = []
    for p in list_ports.comports():
        if p.vid is None:
            continue
        out.append({"device": p.device, "usb_id": f"{p.vid:04x}:{p.pid:04x}", "description": p.description,
                    "serial_number": p.serial_number, "manufacturer": p.manufacturer})
    return out


def pick_port(want_ids: list[str], given: str | None) -> str:
    if given:
        return given
    found = ports()
    match = [p for p in found if p["usb_id"] in want_ids] or found
    if not match:
        sys.exit("No USB serial device found. Plug the tracker in with a data-capable USB cable.")
    if len(match) > 1:
        print("More than one serial device:")
        for i, p in enumerate(match):
            print(f"  [{i}] {p['device']}  {p['usb_id']}  {p['description']}")
        i = input("Which one is the tracker? ").strip()
        return match[int(i)]["device"]
    return match[0]["device"]


# ------------------------------------------------------------------ talking to the node
class Node:
    def __init__(self, port: str):
        import meshtastic.serial_interface
        self.port = port
        self.iface = meshtastic.serial_interface.SerialInterface(devPath=port)

    def close(self) -> None:
        try:
            self.iface.close()
        except Exception:  # noqa: BLE001
            pass

    def identity(self) -> dict[str, Any]:
        from meshtastic.protobuf import mesh_pb2
        info = self.iface.getMyNodeInfo() or {}
        user = info.get("user") or {}
        md = getattr(self.iface, "metadata", None)
        hw = None
        if md is not None and getattr(md, "hw_model", 0):
            hw = mesh_pb2.HardwareModel.Name(md.hw_model)
        hw = hw or user.get("hwModel")
        num = self.iface.myInfo.my_node_num
        return {"node_num": num, "node_id": f"!{num:08x}", "hw_model": hw,
                "firmware_version": getattr(md, "firmware_version", None) if md is not None else None,
                "long_name": user.get("longName"), "short_name": user.get("shortName"),
                "has_public_key": bool(user.get("publicKey"))}

    def _target(self, path: str):
        section, name = path.split(".", 1)
        n = self.iface.localNode
        if section == "channel":
            return n.channels[0].settings.module_settings, name
        if hasattr(n.localConfig, section):
            return getattr(n.localConfig, section), name
        if hasattr(n.moduleConfig, section):
            return getattr(n.moduleConfig, section), name
        raise KeyError(f"{path}: no such config section")

    def get(self, path: str) -> Any:
        msg, name = self._target(path)
        fd = msg.DESCRIPTOR.fields_by_name[name]
        v = getattr(msg, name)
        return fd.enum_type.values_by_number[v].name if fd.enum_type else v

    def set(self, path: str, value: Any) -> None:
        msg, name = self._target(path)
        fd = msg.DESCRIPTOR.fields_by_name.get(name)
        if fd is None:
            raise KeyError(f"{path}: this firmware's config has no {name}")
        if fd.enum_type is not None:
            value = fd.enum_type.values_by_name[value].number
        setattr(msg, name, value)

    def apply(self, changes: dict[str, Any], long_name: str, short_name: str) -> None:
        n = self.iface.localNode
        n.setOwner(long_name=long_name, short_name=short_name)
        time.sleep(1)
        sections, channel = set(), False
        for path, v in changes.items():
            self.set(path, v)
            if path.startswith("channel."):
                channel = True
            else:
                sections.add(path.split(".", 1)[0])
        n.beginSettingsTransaction()
        for s in sorted(sections):
            n.writeConfig(s)
            time.sleep(0.5)
        if channel:
            n.writeChannel(0)
        n.commitSettingsTransaction()

    def primary_channel(self) -> dict[str, Any]:
        ch = self.iface.localNode.channels[0].settings
        return {"name": ch.name or "(preset default)", "psk_is_default": ch.psk in (b"\x01", b"")}

    def position(self) -> dict[str, Any] | None:
        info = self.iface.getMyNodeInfo() or {}
        p = info.get("position") or {}
        if p.get("latitudeI") is None and p.get("latitude") is None:
            return None
        lat = p.get("latitude", (p.get("latitudeI") or 0) * 1e-7)
        lon = p.get("longitude", (p.get("longitudeI") or 0) * 1e-7)
        return {"lat": lat, "lon": lon, "alt_m": p.get("altitude"), "time": p.get("time"),
                "sats": p.get("satsInView"), "precision_bits": p.get("precisionBits")}

    def send_test(self, text: str) -> int | None:
        pkt = self.iface.sendText(text, wantAck=False, channelIndex=0)
        return getattr(pkt, "id", None)


# ------------------------------------------------------------------ commands
def cmd_detect(a: argparse.Namespace) -> int:
    need_meshtastic()
    found = ports()
    if not found:
        print("No USB serial devices.")
        return 1
    for p in found:
        print(f"{p['device']}  {p['usb_id']}  {p['description'] or ''}")
    if a.port or len(found) == 1:
        port = a.port or found[0]["device"]
        node = Node(port)
        try:
            print(json.dumps({"port": port, **node.identity(), "primary_channel": node.primary_channel()}, indent=2))
        finally:
            node.close()
    return 0


def confirm(q: str, yes: bool) -> bool:
    return yes or input(f"{q} [y/N] ").strip().lower() in ("y", "yes")


def reconnect(port: str, timeout: float = 90.0) -> Node:
    t0 = time.monotonic()
    last: Exception | None = None
    while time.monotonic() - t0 < timeout:
        time.sleep(5)
        try:
            return Node(port)
        except Exception as exc:  # noqa: BLE001 - still rebooting
            last = exc
    raise SystemExit(f"The tracker did not come back on {port} after rebooting ({last}). Unplug and replug it, then "
                     "run `provision` again: settings already applied are kept, and it checks them.")


def cmd_provision(a: argparse.Namespace) -> int:
    need_meshtastic()
    bundle = json.loads(pathlib.Path(a.bundle).read_text())
    if bundle.get("kind") != "stratolink-provision-bundle":
        sys.exit(f"{a.bundle} is not a Stratolink provisioning bundle (download one from Starlab)")
    tr, hw, settings = bundle["tracker"], bundle["hardware"], bundle["settings"]
    report: dict[str, Any] = {"kind": "stratolink-provision-report", "tool_version": TOOL_VERSION,
                              "bundle_id": bundle.get("bundle_id"), "started_utc": dt.datetime.now(UTC).isoformat(),
                              "tracker": tr, "profile": bundle.get("profile"), "warnings": []}
    out = pathlib.Path(a.report or pathlib.Path(a.bundle).with_suffix("").with_suffix(".report.json"))

    def save(status: str) -> None:
        report["status"] = status
        report["finished_utc"] = dt.datetime.now(UTC).isoformat()
        out.write_text(json.dumps(report, indent=2, default=str) + "\n")
        print(f"\nReport: {out}  ({status})")

    port = pick_port(hw.get("usb_ids") or [], a.port)
    print(f"Connecting to {port} ...")
    node = Node(port)
    try:
        ident = node.identity()
        report.update({"port": port, "identity": ident})
        print(f"Found {ident['node_id']}  hardware {ident['hw_model']}  firmware {ident['firmware_version']}")
        if ident["hw_model"] not in hw["hw_models"]:
            print(f"This is a {ident['hw_model']}, but the bundle is for {hw['name']} ({', '.join(hw['hw_models'])}). "
                  "Make a bundle for this hardware on Starlab.")
            save("wrong_hardware")
            return 2
        if vtuple(ident["firmware_version"]) < vtuple(hw.get("min_firmware")):
            print(f"Firmware {ident['firmware_version']} is older than {hw['min_firmware']}. Update it with the "
                  f"official Meshtastic web flasher ({hw.get('flasher')}), target {hw.get('firmware_target')}, using "
                  "the UPDATE option (a full erase gives the board a new identity and key), then run this again.")
            save("firmware_update_needed")
            return 3
        ch = node.primary_channel()
        report["primary_channel"] = ch
        if bundle.get("channel", "LongFast") == "LongFast" and not ch["psk_is_default"]:
            print("The primary channel has a private key, but this bundle is for the public LongFast channel. "
                  "Stratolink will only decode it if that key is in stratolink-keys.toml on the observatory.")
            report["warnings"].append("primary channel PSK is not the default")
        want = dict(settings)
        current = {}
        for k in want:
            try:
                current[k] = node.get(k)
            except KeyError as exc:
                report["warnings"].append(str(exc))
        changes = {k: v for k, v in want.items() if k in current and current[k] != v}
        name_change = (ident["long_name"], ident["short_name"]) != (tr["name"], tr["short_name"])
        print(f"\nTracker name: {ident['long_name']!r}/{ident['short_name']!r} -> {tr['name']!r}/{tr['short_name']!r}"
              if name_change else f"\nTracker name already {tr['name']!r}/{tr['short_name']!r}")
        for k, v in changes.items():
            print(f"  {k}: {current[k]!r} -> {v!r}")
        if not changes:
            print("  every profile setting is already in place")
        for w in bundle.get("warnings") or []:
            print(f"  note: {w}")
        if (changes or name_change) and not confirm("Apply these settings? The tracker reboots.", a.yes):
            save("cancelled")
            return 1
        if changes or name_change:
            node.apply(changes, tr["name"], tr["short_name"])
            print("Applied; waiting for the reboot ...")
            node.close()
            node = reconnect(port)
        readback = {}
        for k in want:
            try:
                readback[k] = node.get(k)
            except KeyError:
                readback[k] = None
        ident2 = node.identity()
        mismatches = {k: {"want": want[k], "got": readback[k]} for k in want if readback[k] != want[k]}
        if (ident2["long_name"], ident2["short_name"]) != (tr["name"], tr["short_name"]):
            mismatches["owner"] = {"want": [tr["name"], tr["short_name"]],
                                   "got": [ident2["long_name"], ident2["short_name"]]}
        if ident2["node_num"] != ident["node_num"]:
            mismatches["node_id"] = {"want": ident["node_id"], "got": ident2["node_id"]}
        report.update({"applied": readback, "mismatches": mismatches, "identity_after": ident2})
        if mismatches:
            print("These settings did not read back as set:")
            for k, m in mismatches.items():
                print(f"  {k}: wanted {m['want']!r}, device has {m['got']!r}")
        else:
            print(f"All {len(want)} settings read back as set.")
        # GPS
        print(f"Waiting up to {a.gps_timeout} s for a GPS fix (take it outdoors or near a window) ...")
        fix, t0 = None, time.monotonic()
        while time.monotonic() - t0 < a.gps_timeout:
            p = node.position()
            if p and p.get("time") and abs(time.time() - p["time"]) < 600 and (p["lat"] or p["lon"]):
                fix = p
                break
            time.sleep(5)
        report["gps"] = {"fix": fix, "waited_s": round(time.monotonic() - t0)}
        print(f"GPS fix: {fix['lat']:.5f}, {fix['lon']:.5f}, {fix.get('alt_m')} m, {fix.get('sats')} satellites"
              if fix else "No GPS fix yet (the report says so; check the antenna and sky view).")
        # test transmission
        if not a.no_test:
            text = bundle.get("test_text") or f"STRATOLINK TEST {tr['name']}"
            pid = node.send_test(text)
            report["test"] = {"text": text, "packet_id": f"{pid:08x}" if pid is not None else None,
                              "sent_utc": dt.datetime.now(UTC).isoformat()}
            print(f"Sent test message {text!r} (packet {report['test']['packet_id']}). Upload the report: Starlab "
                  "confirms when a ground station decodes it.")
        save("ok" if not mismatches else "settings_mismatch")
        return 0 if not mismatches else 4
    finally:
        node.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect", help="list USB serial devices and identify a connected tracker")
    d.add_argument("--port")
    d.set_defaults(fn=cmd_detect)
    p = sub.add_parser("provision", help="apply a Starlab bundle, verify, wait for GPS, send a test message")
    p.add_argument("bundle")
    p.add_argument("--port")
    p.add_argument("--report")
    p.add_argument("--yes", action="store_true", help="do not ask before applying")
    p.add_argument("--gps-timeout", type=int, default=300)
    p.add_argument("--no-test", action="store_true", help="do not send the test message")
    p.set_defaults(fn=cmd_provision)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
