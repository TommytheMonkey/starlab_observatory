"""Publish the unified schedule to the Satellite Observatory calendar.

One event per job (id "aobsj" + job_id), covering the recording window, colored
by status. Preempted, skipped and cancelled jobs stay on the calendar so every
decision is visible. Legacy windows are published only where the receiver's
config says so (the satops loop already publishes OBS-R01's passes).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from typing import Any

from sqlalchemy import select

from ..calendar.render import HIGH_PASS_DEG, HIGH_PASS_MARK
from ..db.schema import sched_calendar
from ..timeutil import fmt_local
from .spec import human_size
from .store import Scheduler

LOG = logging.getLogger("aobs.scheduler.calendar")
COLORS = {"scheduled": "7", "preparing": "5", "recording": "5", "recorded": "2", "processing": "2",
          "completed": "10", "failed": "11", "missed": "11", "preempted": "8", "skipped": "8", "cancelled": "8"}
ICON = {"manual": "🎙", "auto": "🛰", "legacy": "🛰"}


def event_id(job_id: str) -> str:
    return "aobsj" + job_id


def render(job: dict[str, Any], tz: str) -> dict[str, Any]:
    spec, r = job["spec"], job["spec"].get("resolved", {})
    radio = spec.get("radio") or {}
    start = dt.datetime.fromisoformat(job["record_start"])
    end = dt.datetime.fromisoformat(job["record_end"])
    status = job["status"]
    who = {"manual": "Manual (Slack)", "auto": "Automatic (broker)", "import": "Imported",
           "legacy": f"Automatic ({job['spec'].get('legacy_unit') or 'legacy recorder'})"}[job["source"]]
    gain = (f"{radio.get('gain_db')} dB" if radio.get("gain_db") is not None else
            ", ".join(f"{k} {v}" for k, v in (radio.get("gain_elements") or {}).items()) or radio.get("gain_mode") or "-")
    lines = [
        f"STATUS: {status.upper()}", f"Reason: {job.get('status_reason') or '-'}", "",
        f"Job: {job['job_name']} ({job['job_id']})", f"Source: {who}",
        f"Target: {job.get('target') or '-'}",
        f"Receiver: {job['receiver_id']} (serial {job['device_serial']})",
        f"Antenna / path: {(spec.get('receiver') or {}).get('antenna') or '-'} / {(spec.get('receiver') or {}).get('signal_path') or '-'}",
        f"Recording: {fmt_local(start, tz, '%H:%M:%S')} to {fmt_local(end, tz, '%H:%M:%S %Z')}",
        f"Reservation: {fmt_local(dt.datetime.fromisoformat(job['reservation_start']), tz, '%H:%M:%S')} to "
        f"{fmt_local(dt.datetime.fromisoformat(job['reservation_end']), tz, '%H:%M:%S %Z')}",
    ]
    if radio:
        lines += [f"Center: {radio.get('center_frequency_hz', 0) / 1e6:.4f} MHz"
                  + (f" · target {radio['target_frequency_hz'] / 1e6:.4f} MHz" if radio.get("target_frequency_hz") else ""),
                  f"Sample rate: {radio.get('sample_rate_hz', 0) / 1e6:.3f} MS/s · gain {gain}"
                  f" · bias tee {'ON' if radio.get('bias_tee') else 'off'}",
                  f"Format: {(spec.get('recording') or {}).get('format')} · estimated {human_size(job.get('estimated_bytes'))}"]
    passes = [x for x in ((spec.get("target") or {}).get("passes") or []) if x.get("max_elevation_deg") is not None]
    for x in passes:
        lines.append(f"Pass: {x.get('name')} max elevation {x['max_elevation_deg']:.1f}°"
                     + (f" · rise {fmt_local(dt.datetime.fromisoformat(x['rise']), tz, '%H:%M:%S')}" if x.get("rise") else "")
                     + (f" · set {fmt_local(dt.datetime.fromisoformat(x['set']), tz, '%H:%M:%S')}" if x.get("set") else ""))
    sd = spec.get("satdump") or {}
    if sd.get("enabled"):
        lines.append(f"SatDump: {sd.get('pipeline')} after recording")
    if job.get("preempted_by"):
        lines.append(f"Preempted by: {job['preempted_by']}")
    res = job.get("result") or {}
    cap = res.get("capture") or {}
    if cap:
        lines += ["", f"Recorded: {human_size(cap.get('bytes'))} ({cap.get('status')})"]
    pp = res.get("postprocess") or {}
    if pp.get("satdump"):
        lines.append(f"Decode: {pp['satdump'].get('images', 0)} image(s) in {pp['satdump'].get('output_directory')}")
    for f in res.get("legacy_files", [])[:6]:
        lines.append(f"  {f['raw']} ({f.get('status')})")
    if (job.get("paths") or {}).get("raw"):
        lines.append(f"File: observatory:{job['paths']['raw']}")
    lines += ["", "Managed by the observatory broker (changes here are overwritten)."]
    title = f"{ICON[job['source']]} {job.get('target') or job['job_name']} · {job['receiver_id']} · {status.upper()}"
    if (spec.get("metadata") or {}).get("simulated"):
        title = f"[SIMULATED] {title}"
        lines.insert(0, "SIMULATED: synthetic samples from Starlab's simulator, not a real recording.")
    if passes:
        peak = max(x["max_elevation_deg"] for x in passes)
        title += f" · max {peak:.0f}°"
        if peak > HIGH_PASS_DEG:
            title = f"{HIGH_PASS_MARK} {title}"
    return {
        "id": event_id(job["job_id"]), "summary": title, "description": "\n".join(lines),
        "location": f"observatory · {job['receiver_id']}",
        "start": {"dateTime": start.isoformat()}, "end": {"dateTime": max(end, start + dt.timedelta(minutes=1)).isoformat()},
        "colorId": COLORS.get(status, "8"), "transparency": "transparent",
        "reminders": {"useDefault": False, "overrides": []}, "status": "confirmed",
        "extendedProperties": {"private": {"aobs_job_id": job["job_id"], "aobs_status": status}},
    }


def body_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def sync(s: Scheduler, client: Any, calendar_id: str, past_hours: float = 24, ahead_hours: float = 72) -> dict[str, int]:
    now = s.clock()
    counts = {"create": 0, "update": 0, "noop": 0, "error": 0}
    jobs = s.jobs(start=now - dt.timedelta(hours=past_hours), end=now + dt.timedelta(hours=ahead_hours), limit=1000)
    with s.engine.connect() as conn:
        known = {r.job_id: r for r in conn.execute(select(sched_calendar))}
    for job in jobs:
        rx = s.receivers.get(job["receiver_id"])
        if job["status"] == "shadow" or job["source"] == "import" or \
                (job["source"] == "legacy" and rx and not rx.publish_legacy_to_calendar):
            continue
        if job["source"] == "legacy" and rx:
            job["spec"]["legacy_unit"] = rx.legacy_unit
        body = render(job, s.config.display_tz)
        h = body_hash(body)
        prev = known.get(job["job_id"])
        if prev is not None and prev.body_hash == h and prev.calendar_id == calendar_id:
            counts["noop"] += 1
            continue
        try:
            plain = {k: v for k, v in body.items() if k != "id"}
            if prev is None or prev.calendar_id != calendar_id:
                try:
                    client.insert_event(calendar_id, body)
                    kind = "create"
                except Exception as exc:  # noqa: BLE001
                    if getattr(exc, "status", None) != 409:
                        raise
                    client.update_event(calendar_id, body["id"], plain)
                    kind = "update"
            else:
                client.update_event(calendar_id, body["id"], plain)
                kind = "update"
            error = None
            counts[kind] += 1
        except Exception as exc:  # noqa: BLE001 - one bad event must not stop the others
            error = str(exc)[:500]
            counts["error"] += 1
            LOG.warning("calendar sync failed for %s: %s", job["job_id"], error)
        with s.engine.begin() as conn:
            conn.execute(sched_calendar.delete().where(sched_calendar.c.job_id == job["job_id"]))
            conn.execute(sched_calendar.insert().values(job_id=job["job_id"], calendar_id=calendar_id,
                                                        event_id=body["id"], body_hash=None if error else h,
                                                        last_error=error, synced_at=now))
    return counts
