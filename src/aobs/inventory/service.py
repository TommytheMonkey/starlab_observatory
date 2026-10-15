"""Receivers in the web UI: discover, identify, add, edit, enable/disable, remove, verify.

The rules this module enforces:

  * Discovery is passive (sysfs and /proc only) and can run during recordings.
  * Identify opens the device (SoapySDRUtil --probe), so it runs only when the device is visibly
    free (no process holds it, no claimed USB interface) and Starlab has nothing booked on it.
  * A receiver can be added only from hardware that is on USB now and has a working capture driver;
    SoapySDR receivers must have been identified first, so their capabilities are the driver's.
  * New receivers never record automatically. They start "untested": only tests may run on them.
  * Readiness changes only through a hardware test (readiness.on_test_capture).
"""

from __future__ import annotations

import datetime as dt
import threading
import types
from typing import Any, Callable

from ..hardware.rtlsdr import SYSFS_USB
from . import discovery, probe
from .drivers import DRIVERS
from .store import EDITABLE, InventoryError

PROBE_VALID = dt.timedelta(hours=6)
NEAR_RESERVATION = dt.timedelta(minutes=3)


class InventoryService:
    def __init__(self, s: Any, discover: Callable[..., list[discovery.Discovered]] = discovery.discover,
                 run_probe: Callable[[str, str], dict[str, Any]] = probe.run_probe, sysfs: Any = SYSFS_USB):
        self.s, self._discover, self._probe, self.sysfs = s, discover, run_probe, sysfs
        self._probes: dict[tuple[str, str], dict[str, Any]] = {}       # (usb_path, serial) -> latest probe
        self._probe_lock = threading.Lock()

    # ------------------------------------------------------------ reading
    def discovered(self) -> list[discovery.Discovered]:
        rows = [types.SimpleNamespace(**r) for r in self.s.inventory.rows()]
        return self._discover(rows, sysfs=self.sysfs)

    def overview(self) -> dict[str, Any]:
        found = self.discovered()
        by_rx = {d.configured_as: d for d in found if d.configured_as}
        receivers = []
        for row in self.s.inventory.listing():
            rx = self.s.receivers.get(row["id"])
            d = by_rx.get(row["id"])
            entry = {k: row[k] for k in ("id", "name", "host", "driver", "serial", "identity", "usb_path", "manufacturer",
                                         "model", "origin", "enabled", "state", "readiness", "readiness_stale",
                                         "readiness_detail", "revision", "updated_at", "updated_by")}
            entry.update(notes=(row["config"] or {}).get("notes", ""), mode=(row["config"] or {}).get("mode", "broker"),
                         automatic=bool(((row["config"] or {}).get("auto") or {}).get("enabled")),
                         driver_label=DRIVERS[row["driver"]].label, capabilities=row.get("capabilities"),
                         in_service=row["origin"] == "toml", schedulable=bool(rx and rx.schedulable),
                         test_kinds=list(DRIVERS[row["driver"]].test_kinds), on_usb=d is not None,
                         usage=d.usage if d else None, editable=row["origin"] == "ui")
            if rx is not None:
                try:
                    entry["presence"] = self.s.presence(rx).as_dict()
                except Exception as exc:  # noqa: BLE001 - the page must render
                    entry["presence"] = {"state": "unknown", "detail": str(exc)}
            receivers.append(entry)
        disc = []
        for d in found:
            item = d.as_dict()
            item["state"] = "configured" if d.configured_as else "discovered"
            p = self._probes.get((d.device.usb_path, d.device.serial))
            if p:
                item["identified"] = {k: p.get(k) for k in ("ok", "error", "problems", "at")}
                if p.get("ok"):
                    item["identified"]["capabilities"] = p["capabilities"]
            disc.append(item)
        return {"receivers": receivers, "discovered": disc, "drivers": {k: {"label": v.label, "tests": list(v.test_kinds),
                "automatic": v.automatic, "needs_identify": v.needs_probe} for k, v in DRIVERS.items()},
                "checked_at": self.s.clock().isoformat()}

    def _find(self, usb_path: str) -> discovery.Discovered:
        d = next((x for x in self.discovered() if x.device.usb_path == usb_path), None)
        if d is None:
            raise InventoryError(f"nothing is plugged into USB port {usb_path} now; press Refresh", 404)
        return d

    def _booked(self, serial: str) -> list[str]:
        """Starlab jobs holding this serial now or within a few minutes."""
        if not serial:
            return []
        from ..scheduling.store import HOLDING
        now = self.s.clock()
        out = []
        for rx in self.s.receivers.values():
            if rx.serial != serial:
                continue
            for j in self.s.jobs(receiver=rx.id, status=list(HOLDING), start=now - dt.timedelta(minutes=1),
                                 end=now + NEAR_RESERVATION):
                out.append(f"{j['target'] or j['job_name']} ({j['status']}, from {j['reservation_start'][11:19]}Z)")
        return out

    # ------------------------------------------------------------ identify (opens the device)
    def identify(self, usb_path: str, actor: str) -> dict[str, Any]:
        d = self._find(usb_path)
        sup = d.support
        if sup.status != "supported":
            raise InventoryError(f"{d.as_dict()['model']}: {sup.detail}", 400)
        drv = DRIVERS[sup.driver]
        if not drv.needs_probe:
            return {"ok": True, "not_needed": True, "detail": f"{drv.label}: Starlab's capability tables for this "
                    "driver were read from real hardware; no probe is needed"}
        if not d.device.serial:
            raise InventoryError("this device reports no serial number, so the driver cannot be told which device to "
                                 "open", 400)
        reasons = []
        if not d.usage["free"]:
            reasons.append(f"it is in use: {d.usage['detail']}")
        booked = self._booked(d.device.serial)
        if booked:
            reasons.append(f"Starlab has it booked: {', '.join(booked)}")
        if reasons:
            raise InventoryError("Not identifying it now, because identifying opens the device: "
                                 + "; ".join(reasons) + ". Nothing was opened.", 409)
        if not self._probe_lock.acquire(blocking=False):
            raise InventoryError("another device is being identified; try again in a moment", 409)
        try:
            again = self._find(usb_path)               # last look right before opening it
            if not again.usage["free"]:
                raise InventoryError(f"it just became busy ({again.usage['detail']}); nothing was opened", 409)
            res = self._probe(sup.soapy, d.device.serial)
        finally:
            self._probe_lock.release()
        now = self.s.clock()
        res["at"] = now.isoformat()
        if res.get("ok"):
            res["capabilities"] = {**res["capabilities"], "source": "SoapySDRUtil --probe", "probed_at": now.isoformat(),
                                   "soapy_driver": sup.soapy, "probed_by": actor}
        self._probes[(usb_path, d.device.serial)] = res
        if res.get("ok") and d.configured_as:
            self.s.inventory.set_capabilities(d.configured_as, res["capabilities"], actor)
            self.s.reload_receivers()
        return {k: res.get(k) for k in ("ok", "error", "problems", "capabilities", "at", "command")}

    # ------------------------------------------------------------ add / edit / remove
    def add(self, body: dict[str, Any], actor: str) -> dict[str, Any]:
        usb_path = str(body.get("usb_path") or "")
        rid = str(body.get("id") or "").strip()
        d = self._find(usb_path)
        info, sup = d.as_dict(), d.support
        if d.configured_as:
            raise InventoryError(f"this device is already receiver {d.configured_as}")
        if sup.status == "not_a_receiver":
            raise InventoryError(f"{info['model']} is not an IQ receiver, so Starlab cannot record with it. "
                                 "Document it in the signal-path layout instead (+ Other receiver).", 400)
        if sup.status != "supported":
            raise InventoryError(f"{info['model']} cannot be added: {sup.detail}", 400)
        drv = DRIVERS[sup.driver]
        serial, identity = d.device.serial, "serial"
        if not serial:
            if d.device.vid == "1df7":
                raise InventoryError("the SDRplay device has no serial yet (firmware not loaded); try again later", 409)
            identity = "usb_path"
        caps = None
        if drv.needs_probe:
            p = self._probes.get((usb_path, serial))
            if not p or not p.get("ok"):
                raise InventoryError("identify the device first: its capabilities must come from its driver", 409)
            if self.s.clock() - dt.datetime.fromisoformat(p["at"]) > PROBE_VALID:
                raise InventoryError("the identification is too old; identify the device again", 409)
            caps = p["capabilities"]
        rf = getattr(self.s, "rf", None)
        lanes = (rf.current or rf.load())["layout"]["receivers"] if rf is not None else {}
        lane = lanes.get(rid)
        if lane is not None and (lane.get("managed") or not body.get("from_layout")):
            raise InventoryError(f"{rid} is already used in the signal-path layout; choose another ID", 409)
        config = {"hardware": info["model"], "antenna": "", "signal_path": "", "mode": "broker", "auto": {},
                  "notes": str(body.get("notes") or "")[:2000]}
        if sup.driver == "soapy":
            config["soapy_driver"] = sup.soapy
        row = self.s.inventory.create(
            rid=rid, name=str(body.get("name") or "").strip() or rid, driver=sup.driver, serial=serial,
            identity=identity, usb_path=usb_path, usb_vid=d.device.vid, usb_pid=d.device.pid,
            manufacturer=info["manufacturer"] or None, model=info["model"], config=config, capabilities=caps,
            actor=actor)
        self.s.reload_receivers()
        if rf is not None:
            rf.set_lane(rid, {"id": rid, "notes": lane.get("notes", "") if lane else ""}, actor,
                        f"receiver {rid} added ({info['model']}, serial {serial or usb_path})")
        return row

    def update(self, rid: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
        changes = {k: body[k] for k in (*EDITABLE, "notes") if k in body}
        if "enabled" in changes and not changes["enabled"] and self._booked_rid(rid):
            raise InventoryError(f"{rid} has a recording booked or running now; disable it afterwards", 409)
        row = self.s.inventory.update(rid, changes, int(body.get("revision") or 0), actor)
        self.s.reload_receivers()
        return row

    def _booked_rid(self, rid: str) -> list[str]:
        rx = self.s.receivers.get(rid)
        return self._booked(rx.serial) if rx else []

    def delete(self, rid: str, revision: int, actor: str) -> dict[str, Any]:
        from ..scheduling.store import HOLDING
        now = self.s.clock()
        if self.s.jobs(receiver=rid, status=list(HOLDING), start=now - dt.timedelta(minutes=1)):
            raise InventoryError(f"{rid} has scheduled or running jobs; cancel them first", 409)
        row = self.s.inventory.public(rid)
        self.s.inventory.delete(rid, revision, actor)
        self.s.reload_receivers()
        rf = getattr(self.s, "rf", None)
        if rf is not None and rid in (rf.current or rf.load())["layout"]["receivers"]:
            # keep the lane and its parts, as documentation of what was there
            rf.set_lane(rid, {"managed": False, "label": row["name"], "serial": row["serial"],
                              "hardware": row["model"] or "", "host": row["host"], "status": "unknown"},
                        actor, f"receiver {rid} removed from the inventory; kept as documentation")
        return {"ok": True, "deleted": rid}

    # ------------------------------------------------------------ verify (a hardware test)
    def verify_body(self, options: dict[str, Any]) -> dict[str, Any]:
        """A short quick test with the receiver's defaults: proves it delivers real IQ samples."""
        d = options["defaults"]
        body = {"test_type": "quick", "frequency": d["frequency_hz"], "sample_rate_hz": d["sample_rate_hz"],
                "duration_s": 10, "name": "Hardware verification", "tags": ["verify"],
                "description": "Readiness check: proves the receiver opens, tunes and delivers real IQ samples.",
                "gain_mode": d.get("gain_mode") or "manual", "gain_db": d.get("gain_db"),
                "ifgr_db": d.get("ifgr_db"), "rfgr": d.get("rfgr"), "agc": d.get("agc"),
                "bandwidth_hz": d.get("bandwidth_hz"), "gain_elements": d.get("gain_elements")}
        if options.get("switched"):
            body["antenna"] = d.get("antenna") or next(iter(options.get("switch_ports", {}).values()), None)
        return body
