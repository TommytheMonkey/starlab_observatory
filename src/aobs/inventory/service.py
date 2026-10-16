"""Receivers in the web UI: discover, identify, add, edit, enable/disable, remove, verify.

The rules this module enforces:

  * Discovery is passive (sysfs and /proc only, the same snapshot on every host) and can run during
    recordings, here or on a remote host.
  * Identify opens the device (vendor tools and SoapySDRUtil --probe, adapters.py), so it runs only when
    the device is visibly free (no process holds it, no claimed USB interface) and Starlab has nothing
    booked on it. Tools that open every device of a kind (airspy_info, hackrf_info) run only when all of
    those devices are free. On a remote host the snapshot is taken again right before anything is opened.
  * A receiver can be added only from hardware that is on USB now and has a working capture driver;
    SoapySDR receivers must have been identified first, so their capabilities are the driver's.
  * New receivers never record automatically. They start "untested": only tests may run on them.
    Receivers on remote hosts are discovered, identified and configured here, and record over SSH
    (scheduling.backends); on a remote host only the quick test runs, which is enough to verify one.
  * Readiness changes only through a hardware test (readiness.on_test_capture).
  * Nothing here claims to identify antennas, filters or amplifiers: they are passive or not on USB, and
    are documented in the signal-path layout instead.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import threading
import types
from typing import Any, Callable

from ..hardware.rtlsdr import SYSFS_USB
from . import adapters, discovery, probe, snapshot
from .drivers import DRIVERS
from .hosts import LOCAL, HostStore, LocalHost
from .store import EDITABLE, InventoryError

PROBE_VALID = dt.timedelta(hours=6)
NEAR_RESERVATION = dt.timedelta(minutes=3)
PASSIVE_NOTE = ("Antennas, filters, amplifiers and cables are not USB devices (or are passive), so they cannot be "
                "discovered or identified; document them in the signal-path diagram.")


class InventoryService:
    def __init__(self, s: Any, discover: Callable[..., list[discovery.Discovered]] = discovery.discover,
                 run_probe: Callable[[str, str], dict[str, Any]] | None = None, sysfs: Any = SYSFS_USB,
                 hosts: HostStore | None = None, local: LocalHost | None = None,
                 remote_paths: dict[str, tuple[str, str]] | None = None):
        self.s, self._discover, self._probe, self.sysfs = s, discover, run_probe, sysfs
        self.local = local or LocalHost()
        self.hosts = hosts if hosts is not None else self._default_hosts()
        self.remote_paths = remote_paths or {}                 # tests: fixture sysfs/proc on a remote host
        self._remote: dict[str, dict[str, Any]] = {}           # host id -> latest discovery
        self._ident: dict[tuple[str, str, str], dict[str, Any]] = {}   # (host, usb_path, serial) -> identify
        self._probe_lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}

    def _default_hosts(self) -> HostStore | None:
        cfg = getattr(self.s, "config", None)
        if cfg is None or not hasattr(self.s, "engine"):
            return None
        return HostStore(self.s.engine, cfg.secrets_dir, cfg.data_root / "run", clock=self.s.clock, local=self.local)

    # ------------------------------------------------------------ reading
    def _rows(self) -> list[types.SimpleNamespace]:
        return [types.SimpleNamespace(**r) for r in self.s.inventory.rows()]

    def discovered(self, host: str = LOCAL) -> list[discovery.Discovered]:
        if host != LOCAL:
            snap = (self._remote.get(host) or {}).get("snapshot")      # re-matched to the current receivers each time
            return discovery.discover_snapshot(snap, self._rows()) if snap is not None else []
        rows = [r for r in self._rows() if r.host == LOCAL]
        sim = ((self.s.config_store.current("simulation") or {}).get("data") or {}).get("enabled", False) \
            if getattr(self.s, "config_store", None) else False
        return self._discover(rows, sysfs=self.sysfs, simulation=bool(sim))

    def discover_host(self, hid: str, actor: str = "") -> dict[str, Any]:
        """Take a fresh snapshot on a remote host (passive; safe during recordings there)."""
        if hid == LOCAL:
            return {"host": LOCAL, "ok": True, "count": len(self.discovered()), "at": self.s.clock().isoformat()}
        if self.hosts is None:
            raise InventoryError("receiver hosts are not available here", 400)
        h = self.hosts.host(hid)
        lock = self._host_locks.setdefault(hid, threading.Lock())
        if not lock.acquire(blocking=False):
            raise InventoryError(f"discovery on {hid} is already running", 409)
        now = self.s.clock()
        try:
            sysfs, proc = self.remote_paths.get(hid, (None, None))
            snap = snapshot.take(h, sysfs, proc)
        except (RuntimeError, ValueError) as exc:
            prev = self._remote.get(hid) or {}
            self._remote[hid] = {**prev, "error": str(exc), "error_at": now.isoformat(),
                                 "stale": prev.get("snapshot") is not None}
            return {"host": hid, "ok": False, "error": str(exc), "at": now.isoformat()}
        finally:
            lock.release()
        items = discovery.discover_snapshot(snap, self._rows())
        self._remote[hid] = {"at": now.isoformat(), "snapshot": snap, "summary": snap.summary(), "tools": snap.tools,
                             "error": None, "stale": False}
        return {"host": hid, "ok": True, "count": len(items), "at": now.isoformat(), "summary": snap.summary()}

    def _host_view(self) -> list[dict[str, Any]]:
        out = [{"id": LOCAL, "name": "This computer", "remote": False, "enabled": True,
                "discovery": {"at": self.s.clock().isoformat(), "error": None, "stale": False, "live": True}}]
        for h in (self.hosts.listing() if self.hosts is not None else []):
            c = self._remote.get(h["id"]) or {}
            out.append({**h, "remote": True, "discovery": {k: c.get(k) for k in ("at", "error", "error_at", "stale",
                                                                                  "summary")}})
        return out

    def _all_discovered(self) -> list[discovery.Discovered]:
        found = list(self.discovered())
        for hid in list(self._remote):
            found += self.discovered(hid)
        return found

    def overview(self) -> dict[str, Any]:
        found = self._all_discovered()
        by_rx = {d.configured_as: d for d in found if d.configured_as}
        receivers = []
        for row in self.s.inventory.listing():
            rx = self.s.receivers.get(row["id"])
            d = by_rx.get(row["id"])
            remote = row["host"] != LOCAL
            entry = {k: row[k] for k in ("id", "name", "host", "driver", "serial", "identity", "usb_path", "manufacturer",
                                         "model", "origin", "enabled", "state", "readiness", "readiness_stale",
                                         "readiness_detail", "revision", "updated_at", "updated_by")}
            entry.update(notes=(row["config"] or {}).get("notes", ""), mode=(row["config"] or {}).get("mode", "broker"),
                         automatic=bool(((row["config"] or {}).get("auto") or {}).get("enabled")),
                         driver_label=DRIVERS[row["driver"]].label, capabilities=row.get("capabilities"),
                         identification=row.get("identification"),
                         in_service=row["origin"] == "toml", schedulable=bool(rx and rx.schedulable),
                         test_kinds=[k for k in DRIVERS[row["driver"]].test_kinds if k == "quick" or not remote],
                         on_usb=d is not None, usage=d.usage if d else None, editable=row["origin"] == "ui",
                         remote=remote,
                         remote_note=(f"on {row['host']}: records over SSH and copies each recording here; only the "
                                      "quick test runs on remote hosts") if remote else None)
            if remote:
                c = self._remote.get(row["host"]) or {}
                entry["presence"] = ({"state": "present" if d else "absent",
                                      "detail": f"in the discovery on {row['host']} at {c['at']}"} if c.get("at")
                                     else {"state": "unknown", "detail": f"no discovery on {row['host']} since the broker "
                                                                         "started; press Discover devices"})
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
            p = self._ident.get((d.host, d.device.usb_path, d.device.serial))
            if p:
                item["identified"] = {k: p.get(k) for k in ("ok", "error", "problems", "at", "fields", "family")}
                if p.get("probe_ok"):
                    item["identified"]["capabilities"] = p["capabilities"]
            disc.append(item)
        return {"receivers": receivers, "discovered": disc, "hosts": self._host_view(), "passive_note": PASSIVE_NOTE,
                "drivers": {k: {"label": v.label, "tests": list(v.test_kinds), "automatic": v.automatic,
                                "needs_identify": v.needs_probe} for k, v in DRIVERS.items()},
                "checked_at": self.s.clock().isoformat()}

    def _find(self, usb_path: str, host: str = LOCAL) -> discovery.Discovered:
        d = next((x for x in self.discovered(host) if x.device.usb_path == usb_path), None)
        if d is None:
            where = "this computer" if host == LOCAL else host
            raise InventoryError(f"nothing is plugged into USB port {usb_path} on {where} now; press Refresh"
                                 + ("" if host == LOCAL else " (Discover devices)"), 404)
        return d

    def _booked(self, serial: str) -> list[str]:
        """Starlab jobs holding this serial now or within a few minutes (this computer's receivers)."""
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
            for sess in getattr(self.s, "session_conflicts", lambda *a: [])(serial, now, now + NEAR_RESERVATION):
                out.append(f"an SDR++ session ({sess['created_by']})")
        return out

    def _host_obj(self, host: str) -> Any:
        if host == LOCAL:
            return self.local
        if self.hosts is None:
            raise InventoryError("receiver hosts are not available here", 400)
        return self.hosts.host(host)

    def _tools(self, host: str) -> Callable[[str], str | None]:
        if host == LOCAL:
            path = os.environ.get("PATH", "")
            if self.local.path_prefix:
                path = f"{self.local.path_prefix}:{path}"
            return lambda t: shutil.which(t, path=path)
        tools = (self._remote.get(host) or {}).get("tools") or {}
        return tools.get

    # ------------------------------------------------------------ identify (opens the device)
    def identify(self, usb_path: str, actor: str, host: str = LOCAL) -> dict[str, Any]:
        d = self._find(usb_path, host)
        info, sup = d.as_dict(), d.support
        if sup.status != "supported":
            raise InventoryError(f"{info['model']}: {sup.detail}", 400)
        serial = d.device.serial
        if not serial:
            raise InventoryError("this device reports no serial number, so the tools cannot be told which device to "
                                 "open", 400)
        if any("devices report serial" in w for w in d.warnings):
            raise InventoryError(f"more than one device on this host reports serial {serial}; the tools cannot be told "
                                 "which one to open. Nothing was opened.", 409)
        fam = adapters.family(info)
        reasons = []
        if fam == "sdrplay":
            # The SDRplay API service always holds RSPs; identify only asks it for its device list.
            if host == LOCAL and d.configured_as and d.configured_as in self.s.receivers:
                from ..scheduling.status import _sdrplay_writing
                try:
                    if _sdrplay_writing(self.s.receivers[d.configured_as]):
                        reasons.append("an RSP recording is being written right now")
                except Exception:  # noqa: BLE001
                    reasons.append("cannot confirm that no RSP recording is running")
        elif not d.usage["free"]:
            reasons.append(f"it is in use: {d.usage['detail']}")
        booked = self._booked(serial) if host == LOCAL else []
        if booked:
            reasons.append(f"Starlab has it booked: {', '.join(booked)}")
        if reasons:
            raise InventoryError("Not identifying it now, because identifying opens the device: "
                                 + "; ".join(reasons) + ". Nothing was opened.", 409)
        if not self._probe_lock.acquire(blocking=False):
            raise InventoryError("another device is being identified; try again in a moment", 409)
        try:
            if host != LOCAL:                              # a fresh look on the remote host right before opening
                self.discover_host(host, actor)
            again = self._find(usb_path, host)
            if fam != "sdrplay" and not again.usage["free"]:
                raise InventoryError(f"it just became busy ({again.usage['detail']}); nothing was opened", 409)
            skip = self._skip_for(fam, again, host)
            h = self._host_obj(host)
            if self._probe is not None and host == LOCAL:
                probe_fn = self._probe
            else:
                probe_fn = lambda soapy, ser: probe.run_probe(soapy, ser, run=probe.host_runner(h))  # noqa: E731
            res = adapters.identify(h.run, info, self._tools(host), probe_fn, skip)
        finally:
            self._probe_lock.release()
        now = self.s.clock()
        res["at"] = now.isoformat()
        if res.get("probe_ok"):
            res["capabilities"] = {**res["capabilities"], "source": "SoapySDRUtil --probe", "probed_at": now.isoformat(),
                                   "soapy_driver": sup.soapy, "probed_by": actor, "host": host}
        self._ident[(host, usb_path, serial)] = res
        if d.configured_as:
            self.s.inventory.set_identification(d.configured_as, {
                k: res.get(k) for k in ("ok", "family", "fields", "problems", "at")} | {
                "steps": [{k: v for k, v in x.items() if k != "output"} for x in res["steps"]]}, actor)
            if res.get("probe_ok") and DRIVERS[sup.driver].needs_probe:
                self.s.inventory.set_capabilities(d.configured_as, res["capabilities"], actor)
            self.s.reload_receivers()
        probe_step = next((x for x in res["steps"] if x.get("tool") == "SoapySDRUtil" and "exit" in x), None)
        return {"ok": res["ok"], "error": None if res["ok"] else "; ".join(res["problems"]) or "nothing was reported",
                "problems": res["problems"], "capabilities": res.get("capabilities") if res.get("probe_ok") else None,
                "at": res["at"], "fields": res["fields"], "family": res["family"], "host": host,
                "command": probe_step["command"].split() if probe_step else None,
                "steps": [{k: v for k, v in x.items() if k != "output"} for x in res["steps"]]}

    def _skip_for(self, fam: str, d: discovery.Discovered, host: str) -> dict[str, str]:
        """Tools that open every device of a kind: skipped unless every one of those devices is free."""
        tool = adapters.OPENS_ALL.get(fam)
        if not tool:
            return {}
        busy = []
        for x in self.discovered(host):
            if x.device.usb_path == d.device.usb_path or adapters.family(x.as_dict()) != fam:
                continue
            why = x.usage["detail"] if not x.usage["free"] else (
                ", ".join(self._booked(x.device.serial)) if host == LOCAL and self._booked(x.device.serial) else "")
            if why:
                busy.append(f"{x.configured_as or x.device.serial or x.device.usb_path} ({why})")
        if busy:
            return {tool: f"{tool} opens every {fam} device on the host, and {', '.join(busy)} is in use or booked"}
        return {}

    # ------------------------------------------------------------ add / edit / remove
    def add(self, body: dict[str, Any], actor: str) -> dict[str, Any]:
        usb_path = str(body.get("usb_path") or "")
        rid = str(body.get("id") or "").strip()
        host = str(body.get("host") or LOCAL)
        d = self._find(usb_path, host)
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
        if serial and any("devices report serial" in w for w in d.warnings):
            raise InventoryError(f"more than one device on this host reports serial {serial}, and the capture tools "
                                 "open receivers by serial, so Starlab could record from the wrong one. Give each a "
                                 "unique serial first" + (" (rtl_eeprom -s)" if sup.driver == "rtlsdr" else ""), 409)
        if not serial:
            if d.device.vid == "1df7":
                raise InventoryError("the SDRplay device has no serial yet (firmware not loaded); try again later", 409)
            identity = "usb_path"
        caps = None
        if sup.driver == "sim":
            from .drivers import SIM_CAPS
            caps = dict(SIM_CAPS)
        ident = self._ident.get((host, usb_path, serial))
        if drv.needs_probe:
            p = ident
            if not p or not p.get("probe_ok"):
                raise InventoryError("identify the device first: its capabilities must come from its driver", 409)
            if self.s.clock() - dt.datetime.fromisoformat(p["at"]) > PROBE_VALID:
                raise InventoryError("the identification is too old; identify the device again", 409)
            caps = p["capabilities"]
        rf = getattr(self.s, "rf", None) if host == LOCAL else None      # remote receivers: not in this station's diagram
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
            actor=actor, host=host,
            identification=({k: ident.get(k) for k in ("ok", "family", "fields", "problems", "at")} if ident else None))
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
