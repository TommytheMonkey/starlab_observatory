"""/radio-health: functional checks, each PASS / WARNING / FAIL / UNKNOWN with an action.

Nothing here opens an SDR. The SDRplay enumeration (through the API service, no device open)
is skipped while any RSP1B recording may be active.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import shutil
import tempfile
from typing import Any

from sqlalchemy import and_, select

from ..db.schema import artifacts, proc_attempts, sched_jobs
from .status import _run, holders, storage, unit_state
from .store import HOLDING, Scheduler


def check(area: str, name: str, status: str, detail: str, action: str = "") -> dict[str, str]:
    return {"area": area, "check": name, "status": status, "detail": detail, "action": action}


def health(s: Scheduler, service: Any = None, processor: Any = None) -> dict[str, Any]:
    now = s.clock()
    out: list[dict[str, str]] = []

    # ---------------------------------------------------------------- hardware
    from ..hardware import rtlsdr
    rtls = rtlsdr.list_rtl_devices()
    serials = [d.serial for d in rtls]
    for rx in s.receivers.values():
        p = s.presence(rx)
        out.append(check("hardware", f"{rx.id} detected", {True: "PASS", False: "FAIL"}.get(p.connected, "UNKNOWN"),
                         p.detail, {False: "check the USB cable/hub; the scheduler rejects jobs on it until it returns",
                                    None: "sudo systemctl restart sdrplay.service (only between recordings)"
                                    if p.reason == "api_unavailable" else ""}.get(p.connected, "")))
    dup = sorted({x for x in serials if serials.count(x) > 1})
    out.append(check("hardware", "unique RTL-SDR serials", "FAIL" if dup else "PASS",
                     f"duplicates: {dup}" if dup else f"{len(serials)} RTL-SDR(s): {', '.join(serials)}",
                     "reprogram one EEPROM serial (rtl_eeprom -s) so locks cannot be confused" if dup else ""))
    rc, klog = _run(["journalctl", "-k", "--since", "-24h", "--no-pager", "-q"], 15)
    if rc == 0:
        errs = [ln for ln in klog.splitlines() if re.search(r"usb .*(error|reset|disconnect)|over-current", ln, re.I)]
        out.append(check("hardware", "USB errors (24 h kernel log)", "WARNING" if errs else "PASS",
                         f"{len(errs)} USB error/reset/disconnect line(s)" + (f"; last: {errs[-1][-120:]}" if errs else ""),
                         "look for a failing hub, cable or power supply" if errs else ""))
    else:
        out.append(check("hardware", "USB errors", "UNKNOWN", "kernel log not readable"))
    rsps = [rx for rx in s.receivers.values() if rx.driver == "sdrplay" and not rx.remote]
    busy = [rx.id for rx in rsps if s.sdrplay_recording_window(rx)]
    if busy:
        out.append(check("hardware", "SDRplay API enumeration", "UNKNOWN", f"skipped: a {', '.join(busy)} recording window is open"))
    elif rsps:
        from ..hardware import sdrplay
        found = sdrplay.api_find(20)
        missing = [rx.id for rx in rsps if rx.serial not in found.serials]
        out.append(check("hardware", "SDRplay API enumeration", "PASS" if found.ok and not missing else
                         "FAIL" if found.ok else "UNKNOWN", found.detail + (f"; missing {missing}" if found.ok and missing else ""),
                         "" if found.ok and not missing else "sudo systemctl restart sdrplay.service between recordings; "
                         "check SoapySDRPlay3 is installed"))

    from .rxhealth import latest
    for rx in s.receivers.values():
        if rx.driver != "rtlsdr":
            continue
        h = latest(s, rx.id)
        if h is None:
            out.append(check("hardware", f"{rx.id} IQ health", "UNKNOWN", "no assessed recording in the last 14 days"))
            continue
        st = {"healthy": "PASS", "weak_signal": "WARNING", "possible_overload": "WARNING",
              "suspicious_flat": "FAIL", "capture_failure": "FAIL"}.get(h["status"], "WARNING")   # basic-check statuses
        gr = (h.get("gain_response") or {}).get("note")
        out.append(check("hardware", f"{rx.id} IQ health", st,
                         f"{h['label']} in {h['recording_id']} ({h['at'][:16]}Z): RMS {h['rms_dbfs']} dBFS, spread "
                         f"{h['sample_spread_lsb']} LSB, {h['unique_codes'][0]}/{h['unique_codes'][1]} I/Q codes, gain "
                         f"{h['requested_gain_db']} dB" + (f"; {gr}" if gr else "")
                         + (f"; tuner {h['tuner_status']}" if h.get("tuner_status") else ""),
                         {"FAIL": "possible stuck receiver: record ~30 s at two gains when idle; if unchanged, reconnect its "
                                  "USB cable between passes (never during one)",
                          "WARNING": "check gain and RF path"}.get(st, "")))

    # ---------------------------------------------------------------- software
    out.append(check("software", "broker", "PASS", f"answering (pid {os.getpid()})"))
    if service is not None:
        lp = getattr(service, "last_plan", {}) or {}
        age = (now - dt.datetime.fromisoformat(lp["at"])).total_seconds() if lp.get("at") else None
        errs = [k for k, v in lp.items() if isinstance(v, dict) and v.get("error")]
        st = "PASS" if age is not None and age < 900 and not errs else ("UNKNOWN" if age is None else "WARNING")
        out.append(check("software", "planner", st, f"last plan {int(age)} s ago" if age is not None else "not run yet"
                         + (f"; errors for {errs}" if errs else ""), "see broker log" if st == "WARNING" else ""))
        lc = getattr(service, "last_calendar", {}) or {}
        if not lc:
            out.append(check("software", "Google Calendar sync", "UNKNOWN", "no sync yet (or calendar disabled)"))
        elif isinstance(lc.get("error"), str):
            out.append(check("software", "Google Calendar sync", "FAIL", lc["error"][:200],
                             "check the OAuth token (aobs calendar auth) and network"))
        else:
            out.append(check("software", "Google Calendar sync", "WARNING" if lc.get("error") else "PASS",
                             f"last sync {lc.get('at', '')[:19]}: {lc.get('create', 0)} created, {lc.get('update', 0)} "
                             f"updated, {lc.get('error', 0)} errors"))
    sd = shutil.which(s.settings.get("satdump_path", "satdump")) or s.settings.get("satdump_path")
    rc, txt = _run([sd], 20) if sd and os.path.exists(sd) else (127, "missing")
    ok = "Usage" in txt or "pipeline name" in txt
    out.append(check("software", "SatDump runs", "PASS" if ok else "FAIL",
                     "binary starts and answers (no-argument run)" if ok else txt[-160:],
                     "" if ok else "reinstall satdump"))
    need = {"meteor_m2-x_lrpt", "noaa_apt"}
    miss = need - set(s.pipelines)
    out.append(check("software", "SatDump pipelines", "WARNING" if miss else "PASS",
                     f"{len(s.pipelines)} pipelines" + (f"; missing {sorted(miss)}" if miss else "")))
    out.append(check("software", "SatDump invocation", "PASS",
                     "decodes run with --disable_multi_modules (the fused mode skips the demodulator in this build)"))
    for b in ("rtl_sdr", "SoapySDRUtil"):
        out.append(check("software", f"{b} installed", "PASS" if shutil.which(b) else "FAIL", shutil.which(b) or "not on PATH"))
    out.append(check("software", "sdrplay.service", "PASS" if unit_state("sdrplay.service") == "active" else "FAIL",
                     unit_state("sdrplay.service")))
    for rx in s.receivers.values():
        if rx.legacy_unit:
            st = unit_state(rx.legacy_unit)
            want = rx.mode in ("legacy", "shadow")
            good = (st == "active") == want
            out.append(check("software", rx.legacy_unit, "PASS" if good else "FAIL",
                             f"{st} ({'expected active' if want else 'expected stopped'}: {rx.id} is {rx.mode})",
                             "" if good else ("start it, or migrate the receiver" if want else
                                              "it must not run after migration: sudo systemctl disable --now " + rx.legacy_unit)))
    rc, ntp = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], 5)
    out.append(check("scheduling", "clock synchronized (NTP)", "PASS" if ntp.strip() == "yes" else "FAIL",
                     f"NTPSynchronized={ntp.strip()}", "" if ntp.strip() == "yes" else "check systemd-timesyncd/chrony"))

    # ---------------------------------------------------------------- storage
    st = storage(s)
    out.append(check("storage", "/data mounted", "PASS" if st.get("mounted") else "FAIL", "/data is a mount point"
                     if st.get("mounted") else "/data is NOT mounted; recordings would fill the system disk",
                     "" if st.get("mounted") else "mount the data disk before scheduling anything"))
    if "free_gb" in st:
        out.append(check("storage", "free space", {"HEALTHY": "PASS", "LOW": "WARNING"}.get(st["state"], "FAIL"),
                         f"{st['free_gb']} GB free of {st['total_gb']} GB", "" if st["state"] == "HEALTHY" else "free space under /data"))
        out.append(check("storage", "inodes", "PASS" if st["inodes_free_pct"] > 5 else "FAIL", f"{st['inodes_free_pct']}% free"))
    for d in {s.settings.get("default_output_root"), s.settings.get("auto_output_root"), str(s.config.data_root)} - {None}:
        p = pathlib.Path(d)
        try:
            p.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=p, prefix=".healthcheck-"):
                pass
            out.append(check("storage", f"{d} writable", "PASS", "test file created and removed"))
        except OSError as exc:
            out.append(check("storage", f"{d} writable", "FAIL", str(exc), "fix ownership/permissions"))

    # ---------------------------------------------------------------- scheduling
    with s.engine.connect() as conn:
        rows = conn.execute(select(sched_jobs).where(and_(sched_jobs.c.status.in_(HOLDING),
                                                         sched_jobs.c.reservation_end > now))).mappings().all()
        overlaps = []
        by_dev: dict[str, list[Any]] = {}
        for r in rows:
            by_dev.setdefault(r["device_serial"], []).append(r)
        for dev, rs in by_dev.items():
            rs.sort(key=lambda r: r["reservation_start"])
            for a, b in zip(rs, rs[1:]):
                if b["reservation_start"] < a["reservation_end"] and not (a["source"] == b["source"] == "legacy"):
                    overlaps.append(f"{a['job_id']}/{b['job_id']} on {dev}")
        stuck = conn.execute(select(sched_jobs.c.job_id).where(and_(
            sched_jobs.c.status.in_(("preparing", "recording")), sched_jobs.c.record_end < now - dt.timedelta(minutes=10)))).scalars().all()
        unmarked = conn.execute(select(sched_jobs.c.job_id).where(and_(
            sched_jobs.c.executes.is_(True), sched_jobs.c.status == "scheduled",
            sched_jobs.c.record_end < now - dt.timedelta(minutes=2)))).scalars().all()
        missed = conn.execute(select(sched_jobs.c.job_id).where(and_(
            sched_jobs.c.status == "missed", sched_jobs.c.record_end > now - dt.timedelta(hours=24)))).scalars().all()
    out.append(check("scheduling", "overlapping reservations", "FAIL" if overlaps else "PASS",
                     "; ".join(overlaps) if overlaps else f"{len(rows)} reservations, none overlap on a device",
                     "cancel one of them" if overlaps else ""))
    out.append(check("scheduling", "stale running jobs", "FAIL" if stuck else "PASS",
                     f"{stuck} still preparing/recording >10 min after their end" if stuck else "none",
                     "restart the broker (it reconciles them) and check the capture log" if stuck else ""))
    out.append(check("scheduling", "unexecuted past jobs", "FAIL" if unmarked else "PASS",
                     f"{unmarked} passed without being executed" if unmarked else "none",
                     "the runner is not running" if unmarked else ""))
    out.append(check("scheduling", "missed recordings (24 h)", "WARNING" if missed else "PASS", f"{len(missed)} missed"))
    try:
        state = json.loads((s.config.data_root / "broker" / "lease.json").read_text() or "{}")
        stale = [r for r, l in state.items() if dt.datetime.fromisoformat(l["expires_at"]) < now - dt.timedelta(minutes=5)]
        out.append(check("scheduling", "stale receiver locks", "FAIL" if stale else "PASS",
                         f"expired leases still recorded: {stale}" if stale else f"{len(state)} active lease(s)"))
    except (OSError, ValueError, KeyError):
        out.append(check("scheduling", "stale receiver locks", "UNKNOWN", "lease state unreadable"))
    for rx in s.receivers.values():
        if rx.driver != "rtlsdr" or rx.remote or not s._present(rx):
            continue
        hs, unreadable = holders(rx)
        foreign = [h for h in hs if h["owner"] in ("SDR++", "other")]
        if foreign:
            out.append(check("scheduling", f"{rx.id} foreign users", "WARNING",
                             ", ".join(f"{h['owner']}[{h['pid']}]" for h in foreign),
                             "a process outside the broker holds it; scheduled jobs on it will fail until it exits"))

    # ---------------------------------------------------------------- processing
    with s.engine.connect() as conn:
        long = conn.execute(select(proc_attempts.c.attempt_id).where(and_(
            proc_attempts.c.status == "running",
            proc_attempts.c.started_at < now - dt.timedelta(seconds=int(s.settings.get("satdump_timeout_seconds", 3600)) * 2)))).scalars().all()
        failed = conn.execute(select(proc_attempts.c.attempt_id).where(and_(
            proc_attempts.c.status == "failed", proc_attempts.c.queued_at > now - dt.timedelta(hours=24)))).scalars().all()
        missing = conn.execute(select(artifacts.c.path).where(artifacts.c.deleted_at.is_(None))).scalars().all()
        badmeta = conn.execute(select(sched_jobs.c.job_id, sched_jobs.c.spec).where(
            sched_jobs.c.status.in_(("recorded", "completed")))).all()
    gone = [p for p in missing if not os.path.exists(p)]
    out.append(check("processing", "stuck attempts", "FAIL" if long else "PASS", f"{long}" if long else "none",
                     "/radio-cancel processing=<id>" if long else ""))
    out.append(check("processing", "failed attempts (24 h)", "WARNING" if failed else "PASS", f"{len(failed)} failed",
                     "/radio-results <recording> shows why" if failed else ""))
    out.append(check("processing", "indexed artifacts present", "WARNING" if gone else "PASS",
                     f"{len(gone)} indexed file(s) missing on disk" if gone else f"{len(missing)} artifacts present",
                     "retention removed them; the index marks them deleted on its next sweep" if gone else ""))
    bad = [j for j, sp in badmeta if not ((sp or {}).get("radio") or {}).get("sample_rate_hz")]
    out.append(check("processing", "recording metadata", "WARNING" if bad else "PASS",
                     f"{len(bad)} recording(s) without a sample rate: {bad[:5]}" if bad else "every recording has rate/format",
                     "correct with /radio-recordings ... or the metadata API" if bad else ""))
    order = {"FAIL": 0, "WARNING": 1, "UNKNOWN": 2, "PASS": 3}
    overall = min((c["status"] for c in out), key=lambda x: order[x]) if out else "UNKNOWN"
    return {"at": now.isoformat(), "overall": overall, "checks": sorted(out, key=lambda c: (order[c["status"]], c["area"]))}
