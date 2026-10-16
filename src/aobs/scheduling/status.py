"""Live observatory status and health, built from real state only.

A receiver is never reported idle just because the broker has no job: USB file-handle owners,
legacy recorder windows and active capture files are inspected too.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import re
import shutil
import subprocess
import time
from typing import Any, Callable

from sqlalchemy import and_, func, select

from ..db.schema import proc_attempts, sched_calendar, sched_jobs
from .hardware import Receiver
from .store import HOLDING, Scheduler

STARTED = time.time()


def _run(cmd: list[str], timeout: float = 10) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def unit_state(unit: str) -> str:
    return _run(["systemctl", "is-active", unit], 5)[1].splitlines()[-1] if unit else "n/a"


def _proc_cmdlines() -> list[tuple[int, str]]:
    out = []
    for d in pathlib.Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                out.append((int(d.name), (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()))
            except OSError:
                continue
    return out


def holders(rx: Receiver) -> tuple[list[dict[str, Any]], int]:
    """Processes using the device. RTL: open USB file handles. SDRplay: the API service always holds the
    USB device, so look for processes that open it through SoapySDR (captures, legacy scripts)."""
    found: list[dict[str, Any]] = []
    unreadable = 0
    if rx.driver == "rtlsdr":
        from ..hardware import rtlsdr
        dev = next((d for d in rtlsdr.list_rtl_devices() if d.serial == rx.serial), None)
        if dev is not None:
            hs, unreadable = rtlsdr.usb_holders(dev.dev_node)
            found = [{"pid": h.pid, "process": h.comm, "cmdline": h.cmdline[:200]} for h in hs]
    elif rx.driver != "sdrplay":                 # other SoapySDR receivers: open handles on the USB node
        from ..hardware import rtlsdr
        from ..inventory.discovery import find_device
        dev = find_device(rx.serial, usb_path=rx.usb_path if rx.identity == "usb_path" else None)
        if dev is not None:
            hs, unreadable = rtlsdr.usb_holders(dev.dev_node)
            found = [{"pid": h.pid, "process": h.comm, "cmdline": h.cmdline[:200]} for h in hs]
    else:
        for pid, cmd in _proc_cmdlines():
            if pid == os.getpid():
                continue
            if (rx.serial in cmd and "aobs.scheduling.capture" in cmd) or re.search(r"(meteor-auto|record-meteor[^ ]*)\.py", cmd):
                found.append({"pid": pid, "process": cmd.split()[1].rsplit("/", 1)[-1] if " " in cmd else cmd, "cmdline": cmd[:200]})
    for h in found:
        c = h["cmdline"]
        h["owner"] = ("broker capture" if "aobs.scheduling.capture" in c else "ADS-B (dump1090)" if "dump1090" in c
                      else "legacy scheduler (waits between passes)" if "meteor-auto" in c
                      else "manual SDRplay script" if "record-meteor" in c
                      else "SDR++" if "sdrpp" in c else "legacy recorder" if ("record.py" in c or "rtl_sdr" in c or "meteor" in c)
                      else "broker radio op" if any(k in c for k in ("rtl_power", "rtl_fm")) else "other")
    return found, unreadable


def _sdrplay_writing(rx: Receiver) -> str | None:
    """The legacy RSP1B recorder writes auto_*.cs16; a file modified in the last 5 s means it is recording."""
    d = pathlib.Path(rx.legacy_output or "/nonexistent")
    if not d.is_dir():
        return None
    now = time.time()
    for f in d.glob("*.cs16"):
        try:
            if now - f.stat().st_mtime < 5:
                return f.name
        except OSError:
            continue
    return None


class StatusService:
    def __init__(self, s: Scheduler, broker_state: Callable[[], dict[str, Any]] | None = None, processor: Any = None,
                 service: Any = None):
        self.s, self.broker_state, self.processor, self.service = s, broker_state, processor, service

    # ------------------------------------------------------------ receivers
    def receiver(self, rx: Receiver, leases: dict[str, Any] | None = None) -> dict[str, Any]:
        now = self.s.clock()
        presence = self.s.presence(rx)
        connected = presence.connected                   # None: on USB or not cannot be confirmed right now
        hs, unreadable = holders(rx) if connected is not False else ([], 0)
        with self.s.engine.connect() as conn:
            cur = conn.execute(select(sched_jobs).where(and_(
                sched_jobs.c.device_serial == rx.serial, sched_jobs.c.status.in_(HOLDING),
                sched_jobs.c.reservation_start <= now, sched_jobs.c.reservation_end > now))
                .order_by(sched_jobs.c.reservation_start)).mappings().first()
            nxt = conn.execute(select(sched_jobs).where(and_(
                sched_jobs.c.device_serial == rx.serial, sched_jobs.c.status == "scheduled",
                sched_jobs.c.reservation_start > now)).order_by(sched_jobs.c.reservation_start).limit(1)).mappings().first()
            last_ok = conn.execute(select(sched_jobs).where(and_(
                sched_jobs.c.device_serial == rx.serial, sched_jobs.c.status.in_(("recorded", "processing", "completed"))))
                .order_by(sched_jobs.c.record_start.desc()).limit(1)).mappings().first()
            last_err = conn.execute(select(sched_jobs).where(and_(
                sched_jobs.c.device_serial == rx.serial, sched_jobs.c.status.in_(("failed", "missed")),
                sched_jobs.c.record_end > now - dt.timedelta(days=2)))
                .order_by(sched_jobs.c.record_start.desc()).limit(1)).mappings().first()
        lease = (leases or {}).get(rx.id)
        writing = _sdrplay_writing(rx) if rx.driver == "sdrplay" else None
        foreign = [h for h in hs if h["owner"] not in ("broker capture", "legacy scheduler (waits between passes)")]
        if connected is False:
            state = "UNAVAILABLE"
        elif cur is not None and cur["status"] == "recording":
            state = "RECORDING"
        elif cur is not None and cur["source"] == "legacy" and cur["record_start"] <= now < cur["record_end"]:
            state = "RECORDING (legacy service)"
        elif writing:
            state = "RECORDING (legacy service)"
        elif cur is not None or (lease and lease.get("owner", "").startswith("scheduler:")):
            state = "RESERVED"
        elif lease:
            state = f"IN USE (lease: {lease.get('owner')})"
        elif foreign:
            state = f"IN USE ({', '.join(sorted({h['owner'] for h in foreign}))})"
        elif connected is None:
            state = "UNKNOWN (USB presence not confirmed)"
        elif rx.mode in ("legacy", "shadow"):
            state = "IDLE (legacy service controls it)"
        else:
            state = "IDLE"
        settings_src = (cur or {}).get("spec") or {}
        radio = settings_src.get("radio") or {}
        if not radio and rx.auto:
            a = rx.auto
            radio = {"center_frequency_hz": a.get("center_frequency_hz"), "sample_rate_hz": a.get("sample_rate_hz"),
                     "gain_db": a.get("gain_db"), "gain_elements": {"IFGR": a["ifgr_db"], "RFGR": a["rfgr"]} if "ifgr_db" in a else None,
                     "bias_tee": False, "source": "automatic-pass settings"}
        d: dict[str, Any] = {
            "receiver_id": rx.id, "driver": rx.driver, "serial": rx.serial, "hardware": rx.hardware,
            "connected": connected, "presence": presence.as_dict(), "state": state, "mode": rx.mode.upper(), "antenna": rx.antenna,
            "signal_path": rx.signal_path, "legacy_unit": rx.legacy_unit or None,
            "legacy_unit_state": unit_state(rx.legacy_unit) if rx.legacy_unit else None,
            "processes": hs, "unreadable_processes": unreadable, "lease": lease,
            "current_job": _brief(cur, now) if cur is not None else None,
            "settings": {k: radio.get(k) for k in ("center_frequency_hz", "target_frequency_hz", "sample_rate_hz",
                                                   "gain_db", "gain_elements", "bias_tee") if radio.get(k) is not None},
            "next_job": _brief(nxt, now) if nxt is not None else None,
            "last_successful_recording": _brief(last_ok, now) if last_ok is not None else None,
            "last_error": {**_brief(last_err, now), "reason": last_err["status_reason"]} if last_err is not None else None,
        }
        if writing:
            d["legacy_file_growing"] = writing
        if rx.driver == "rtlsdr":
            from .rxhealth import latest
            try:
                d["iq_health"] = latest(self.s, rx.id)
            except Exception:  # noqa: BLE001 - status must render without it
                d["iq_health"] = None
        if rx.notes:
            d["notes"] = rx.notes
        return d

    # ------------------------------------------------------------ overall
    def status(self) -> dict[str, Any]:
        now = self.s.clock()
        try:
            bstate = self.broker_state() if self.broker_state else {}
        except Exception as exc:  # noqa: BLE001
            bstate = {"error": str(exc)}
        leases = bstate.get("leases", {}) if isinstance(bstate, dict) else {}
        receivers = [self.receiver(rx, leases) for rx in self.s.receivers.values()]
        with self.s.engine.connect() as conn:
            proc = dict(conn.execute(select(proc_attempts.c.status, func.count()).group_by(proc_attempts.c.status)).all())
            cal_err = conn.execute(select(func.count()).select_from(sched_calendar)
                                   .where(sched_calendar.c.last_error.is_not(None))).scalar()
            recent_fail = conn.execute(select(sched_jobs).where(and_(
                sched_jobs.c.status.in_(("failed", "missed")), sched_jobs.c.record_end > now - dt.timedelta(hours=24)))
                .order_by(sched_jobs.c.record_end.desc()).limit(5)).mappings().all()
        upcoming = self.s.jobs(status=["scheduled"], start=now, end=now + dt.timedelta(hours=24), limit=400)
        seen, nxt = set(), []
        for j in upcoming:   # one line per pass even when several receivers record it
            key = (j["target"], j["record_start"][:16])
            if key in seen:
                continue
            seen.add(key)
            same = [x["receiver_id"] for x in upcoming if (x["target"], x["record_start"][:16]) == key]
            nxt.append({"start": j["record_start"], "target": j["target"] or j["job_name"], "receivers": same,
                        "source": j["source"]})
        svc = self.service
        last_cal = getattr(svc, "last_calendar", {}) if svc else {}
        return {
            "at": now.isoformat(), "host": self.s.config.host,
            "broker": {"state": "ONLINE", "pid": os.getpid(), "uptime_s": int(time.time() - STARTED)},
            "calendar": {"last_sync": last_cal, "events_with_errors": cal_err,
                         "state": "UNKNOWN" if not last_cal else "FAILING" if isinstance(last_cal.get("error"), str)
                         else "DEGRADED" if last_cal.get("error") or cal_err else "HEALTHY"},
            "receivers": receivers,
            "processing": {k: proc.get(k, 0) for k in ("queued", "running", "paused", "failed", "completed", "cancelled")},
            "storage": storage(self.s),
            "next_jobs": nxt[:12],
            "recent_failures": [{**_brief(r, now), "reason": r["status_reason"]} for r in recent_fail],
        }


def storage(s: Scheduler) -> dict[str, Any]:
    root = str(s.config.data_root)
    out: dict[str, Any] = {"path": root, "mounted": os.path.ismount(root)}
    try:
        du = shutil.disk_usage(root)
        st = os.statvfs(root)
        reserve = float(s.settings.get("reserve_free_gib", 64)) * 1024**3
        out.update(free_gb=round(du.free / 1e9, 1), total_gb=round(du.total / 1e9, 1),
                   used_pct=round(100 * du.used / du.total, 1), inodes_free_pct=round(100 * st.f_favail / max(1, st.f_files), 1),
                   state="HEALTHY" if du.free > 2 * reserve else "LOW" if du.free > reserve else "CRITICAL")
    except OSError as exc:
        out.update(state="ERROR", error=str(exc))
    return out


def _brief(r: Any, now: dt.datetime) -> dict[str, Any]:
    d = {"job_id": r["job_id"], "job_name": r["job_name"], "source": r["source"], "status": r["status"],
         "target": r["target"], "record_start": r["record_start"].isoformat(), "record_end": r["record_end"].isoformat()}
    if r["record_start"] <= now < r["record_end"]:
        d["elapsed_s"] = int((now - r["record_start"]).total_seconds())
        d["remaining_s"] = int((r["record_end"] - now).total_seconds())
    return d
