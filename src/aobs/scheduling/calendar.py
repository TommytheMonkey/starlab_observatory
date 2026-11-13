"""Publish the unified schedule to the Satellite Observatory calendar.

One event per pass: jobs for the same target whose recording windows overlap (the same pass taken on
several receivers) share one event, id "aobsj" + the group's first job_id, covering the union of their
windows and colored by status. Receivers that were preempted, skipped or cancelled are named in the
description (and the title only when no receiver is left), so every decision stays visible. Legacy windows are published only where the receiver's
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
from ..db.schema import proc_attempts, sched_calendar
from ..timeutil import fmt_local
from .spec import human_size
from .store import Scheduler

LOG = logging.getLogger("aobs.scheduler.calendar")
COLORS = {"scheduled": "7", "preparing": "5", "recording": "5", "recorded": "2", "processing": "2",
          "completed": "10", "failed": "11", "missed": "11", "preempted": "8", "skipped": "8", "cancelled": "8"}
ICON = {"manual": "🎙", "auto": "🛰", "legacy": "🛰"}
DONE = ("recorded", "processing", "completed")       # the recording itself is finished
DECODE_FAILED_COLOR = "6"


def event_id(job_id: str) -> str:
    return "aobsj" + job_id


def decode_note(job: dict[str, Any]) -> str:
    """The latest decode attempt of a job, as sync() attaches it: "" when none was made."""
    d = job.get("decode") or {}
    ins = (d.get("result") or {}).get("inputs") or []
    if d.get("status") == "completed" and any(i.get("decoder") == "sstv" for i in ins):
        n = sum(int(i.get("images") or 0) for i in ins)
        return f"{n} SSTV image(s) ✓" if n else "no SSTV images"
    return {"queued": "decoding…", "running": "decoding…", "paused": "decoding…",
            "completed": "decoded ✓", "failed": "decode failed ✗"}.get(d.get("status") or "", "")


def state(job: dict[str, Any]) -> str:
    """What the calendar shows for one receiver: SCHEDULED, RECORDING, COMPLETE · decoded ✓, MISSED, ..."""
    label = "COMPLETE" if job["status"] in DONE else job["status"].upper()
    note = decode_note(job)
    return f"{label} · {note}" if note else label


def _color(job: dict[str, Any]) -> str:
    return DECODE_FAILED_COLOR if (job.get("decode") or {}).get("status") == "failed" else COLORS.get(job["status"], "8")


def _decode_line(job: dict[str, Any]) -> str | None:
    d = job.get("decode")
    if not d:
        return None
    ins = (d.get("result") or {}).get("inputs") or []
    frames, imgs = sum(int(i.get("cadu_frames") or 0) for i in ins), sum(int(i.get("images") or 0) for i in ins)
    if d["status"] == "completed" and any(i.get("decoder") == "sstv" for i in ins):
        return f"Decode: SSTV · {imgs} image(s)" if imgs else "Decode: no SSTV images (normal when no SSTV event is running)"
    if d["status"] == "completed":
        return f"Decode: SUCCESS · {imgs} image(s), {frames} frames"
    if d["status"] == "failed":
        return f"Decode: FAILED · {(d.get('status_reason') or 'no reason given')[:300]}"
    return f"Decode: {d['status']}"


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
        f"STATUS: {state(job)}", f"Reason: {job.get('status_reason') or '-'}", "",
        f"Job: {job['job_name']} ({job['job_id']})", f"Source: {who}",
        f"Target: {job.get('target') or '-'}",
        f"Receiver: {job['receiver_id']} (serial {job['device_serial']})",
        f"Antenna / path: {(spec.get('receiver') or {}).get('antenna') or '-'} / {(spec.get('receiver') or {}).get('signal_path') or '-'}",
        f"Recording: {fmt_local(start, tz, '%-I:%M:%S %p')} to {fmt_local(end, tz, '%-I:%M:%S %p %Z')}",
        f"Reservation: {fmt_local(dt.datetime.fromisoformat(job['reservation_start']), tz, '%-I:%M:%S %p')} to "
        f"{fmt_local(dt.datetime.fromisoformat(job['reservation_end']), tz, '%-I:%M:%S %p %Z')}",
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
                     + (f" · rise {fmt_local(dt.datetime.fromisoformat(x['rise']), tz, '%-I:%M:%S %p')}" if x.get("rise") else "")
                     + (f" · set {fmt_local(dt.datetime.fromisoformat(x['set']), tz, '%-I:%M:%S %p')}" if x.get("set") else ""))
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
    if _decode_line(job):
        lines.append(_decode_line(job))
    for f in res.get("legacy_files", [])[:6]:
        lines.append(f"  {f['raw']} ({f.get('status')})")
    if (job.get("paths") or {}).get("raw"):
        lines.append(f"File: observatory:{job['paths']['raw']}")
    lines += ["", "Managed by the observatory broker (changes here are overwritten)."]
    title = f"{ICON[job['source']]} {job.get('target') or job['job_name']} · {job['receiver_id']} · {state(job)}"
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
        "colorId": _color(job), "transparency": "transparent",
        "reminders": {"useDefault": False, "overrides": []}, "status": "confirmed",
        "extendedProperties": {"private": {"aobs_job_id": job["job_id"], "aobs_status": status}},
    }


INACTIVE = ("preempted", "skipped", "cancelled")


def group_jobs(jobs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Jobs for the same target with overlapping recording windows, i.e. one pass on several receivers."""
    groups: list[list[dict[str, Any]]] = []
    open_: dict[str, tuple[list[dict[str, Any]], dt.datetime]] = {}
    for job in sorted(jobs, key=lambda j: (j["record_start"], j["job_id"])):
        start, end = dt.datetime.fromisoformat(job["record_start"]), dt.datetime.fromisoformat(job["record_end"])
        key = (job.get("target") or "").strip().lower()
        cur = open_.get(key) if key else None
        if cur is not None and start < cur[1]:
            cur[0].append(job)
            open_[key] = (cur[0], max(cur[1], end))
            continue
        groups.append([job])
        if key:
            open_[key] = (groups[-1], end)
    return groups


def render_group(jobs: list[dict[str, Any]], tz: str) -> dict[str, Any]:
    """One event for a pass recorded on one or more receivers (a single job renders exactly as render())."""
    bodies = [render(j, tz) for j in jobs]
    if len(jobs) == 1:
        return bodies[0]
    lead = min(jobs, key=lambda j: j["job_id"])
    live = sorted([j for j in jobs if j["status"] not in INACTIVE] or jobs, key=lambda j: j["receiver_id"])
    states = {state(j) for j in live}
    if len(states) == 1:
        shown = f"{', '.join(j['receiver_id'] for j in live)} · {next(iter(states))}"
    else:
        shown = " | ".join(f"{j['receiver_id']} {state(j)}" for j in live)
    # the color of the earliest stage any receiver is in (scheduled before recording before completed), decode
    # failures first
    order = list(COLORS)
    status = min((j["status"] for j in live), key=lambda st: order.index(st) if st in order else len(order))
    color = DECODE_FAILED_COLOR if any((j.get("decode") or {}).get("status") == "failed" for j in live) \
        else COLORS.get(status, "8")
    first = bodies[jobs.index(lead)]
    icon = ICON["manual"] if any(j["source"] == "manual" for j in jobs) else ICON[lead["source"]]
    title = f"{icon} {lead.get('target') or lead['job_name']} · {shown}"
    peak = max((x["max_elevation_deg"] for j in jobs for x in ((j["spec"].get("target") or {}).get("passes") or [])
                if x.get("max_elevation_deg") is not None), default=None)
    if peak is not None:
        title += f" · max {peak:.0f}°"
        if peak > HIGH_PASS_DEG:
            title = f"{HIGH_PASS_MARK} {title}"
    if any((j["spec"].get("metadata") or {}).get("simulated") for j in jobs):
        title = f"[SIMULATED] {title}"
    ordered = sorted(zip(jobs, bodies), key=lambda jb: (jb[0]["status"] in INACTIVE, jb[0]["receiver_id"]))
    parts = [f"{len(jobs)} receivers:"] + [f"  {j['receiver_id']}: {state(j)}" for j, _ in ordered]
    for j, b in ordered:
        parts += ["", f"——— {j['receiver_id']} ———", b["description"].removesuffix(
            "\n\nManaged by the observatory broker (changes here are overwritten).")]
    parts += ["", "Managed by the observatory broker (changes here are overwritten)."]
    return {**first, "summary": title, "description": "\n".join(parts),
            "location": "observatory · " + ", ".join(sorted(j["receiver_id"] for j in jobs)),
            "start": {"dateTime": min(b["start"]["dateTime"] for b in bodies)},
            "end": {"dateTime": max(b["end"]["dateTime"] for b in bodies)},
            "colorId": color,
            "extendedProperties": {"private": {"aobs_job_id": lead["job_id"], "aobs_status": status,
                                               "aobs_job_ids": ",".join(sorted(j["job_id"] for j in jobs))[:1000]}}}


def body_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _upsert(client: Any, calendar_id: str, body: dict[str, Any], exists: bool) -> str:
    plain = {k: v for k, v in body.items() if k != "id"}
    if not exists:
        try:
            client.insert_event(calendar_id, body)
            return "create"
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) != 409:
                raise
            client.update_event(calendar_id, body["id"], plain)
            return "update"
    try:
        client.update_event(calendar_id, body["id"], plain)
        return "update"
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "status", None) not in (404, 410):
            raise
        # A remembered event that is not there: its first insert failed (e.g. no access yet). Create it.
        client.insert_event(calendar_id, body)
        return "create"


def sync(s: Scheduler, client: Any, calendar_id: str, past_hours: float = 24, ahead_hours: float = 72) -> dict[str, int]:
    now = s.clock()
    counts = {"create": 0, "update": 0, "noop": 0, "error": 0, "delete": 0}
    jobs = s.jobs(start=now - dt.timedelta(hours=past_hours), end=now + dt.timedelta(hours=ahead_hours), limit=1000)
    with s.engine.connect() as conn:
        known = {r.job_id: r for r in conn.execute(select(sched_calendar))}
    publish = []
    for job in jobs:
        rx = s.receivers.get(job["receiver_id"])
        if job["status"] in ("shadow", "predicted") or job["source"] == "import" or \
                (job["source"] == "legacy" and rx and not rx.publish_legacy_to_calendar):
            continue
        if job.get("rule_id") and job["status"] == "skipped" and job["job_id"] not in known:
            continue                      # a rule's pass that was never booked: the Scheduler page shows why
        if job["source"] == "legacy" and rx:
            job["spec"]["legacy_unit"] = rx.legacy_unit
        publish.append(job)
    if publish:                           # the latest decode attempt of each job: shown as "decoded ✓" / "decode failed ✗"
        with s.engine.connect() as conn:
            rows = conn.execute(select(proc_attempts.c.recording_id, proc_attempts.c.status, proc_attempts.c.status_reason,
                                       proc_attempts.c.result)
                                .where(proc_attempts.c.kind == "decode",
                                       proc_attempts.c.recording_id.in_([j["job_id"] for j in publish]))
                                .order_by(proc_attempts.c.queued_at)).mappings().all()
        latest = {r["recording_id"]: dict(r) for r in rows if r["status"] != "cancelled"}
        for job in publish:
            job["decode"] = latest.get(job["job_id"])

    groups = group_jobs(publish)
    wanted = {event_id(min(j["job_id"] for j in g)) for g in groups}
    for group in groups:
        body = render_group(group, s.config.display_tz)
        h = body_hash(body)
        prev = [known.get(j["job_id"]) for j in group]
        if all(p is not None and p.event_id == body["id"] and p.body_hash == h and p.calendar_id == calendar_id
               for p in prev):
            counts["noop"] += 1
            continue
        try:
            exists = any(p is not None and p.event_id == body["id"] and p.calendar_id == calendar_id for p in prev)
            counts[_upsert(client, calendar_id, body, exists)] += 1
            error = None
        except Exception as exc:  # noqa: BLE001 - one bad event must not stop the others
            error = str(exc)[:500]
            counts["error"] += 1
            LOG.warning("calendar sync failed for %s: %s", body["id"], error)
        # Events these jobs had before they were grouped (one per receiver): remove them once the pass event exists.
        stale = {(p.calendar_id, p.event_id) for p in prev
                 if p is not None and p.event_id != body["id"] and p.event_id not in wanted}
        for cal, eid in sorted(stale) if error is None else ():
            try:
                client.delete_event(cal, eid)
                counts["delete"] += 1
            except Exception as exc:  # noqa: BLE001 - left for the next cycle
                LOG.warning("calendar delete failed for %s: %s", eid, exc)
                error = f"stale event {eid} not deleted: {exc}"[:500]
        with s.engine.begin() as conn:
            for j in group:
                conn.execute(sched_calendar.delete().where(sched_calendar.c.job_id == j["job_id"]))
                conn.execute(sched_calendar.insert().values(job_id=j["job_id"], calendar_id=calendar_id,
                                                            event_id=body["id"], body_hash=None if error else h,
                                                            last_error=error, synced_at=now))
    return counts
