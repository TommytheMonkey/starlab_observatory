"""Recording archive (brief 8): outcome levels, where each recording's files are, and storage usage.

Four outcomes are kept apart, and each needs its own evidence:

  recorded        the capture was validated (complete), or kept as partial; not "failed"
  signal          the decoder locked (frames or SYNCED reports), or the analysis measured the target
                  frequency's time-averaged power at least SIGNAL_DB above the noise floor (peak hold is not
                  used: over a whole pass, noise alone peaks ~10 dB up); "not detected" when it was measured
                  lower; "unknown" when nothing measured it
  frames          CADU frames from the best decode attempt (none until a decode ran)
  image           images with real content (a blank or unreadable image does not count)

A recording is never "decoded" because its IQ capture succeeded, and a failed decode never turns a valid
recording into a failed one: the job stays completed and the decode outcome says what happened.
"""

from __future__ import annotations

import os
import pathlib
import shutil
from typing import Any

from sqlalchemy import and_, func, select

from ..timeutil import fmt_clock
from ..db.schema import artifacts, proc_attempts, sched_jobs

SIGNAL_DB = 3.0
PENDING = ("queued", "running", "paused")
LEVELS = ("failed", "recorded", "signal", "frames", "image")


def outcome(job: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """The four separate outcomes of a recording, each with the evidence behind it."""
    status = job["status"]
    rec = {"completed": "complete", "processing": "complete", "recorded": "complete", "partial": "partial"}.get(
        status, "failed" if status == "failed" else status)
    validation = (((job.get("result") or {}).get("capture") or {}).get("validation") or {})
    out: dict[str, Any] = {"recording": rec, "recording_note": job.get("status_reason") if rec == "failed" else
                           ("validated" if validation.get("ok") else None),
                           "signal": "unknown", "signal_note": "nothing has measured it yet",
                           "frames": None, "images": None, "decode": "not run", "decode_note": None,
                           "pending": sorted({a["kind"] for a in attempts if a["status"] in PENDING})}
    decodes = [a for a in attempts if a["kind"] == "decode" and a["status"] not in ("cancelled",)]
    finished = [a for a in decodes if a["status"] not in PENDING]
    if finished:
        best = None
        for a in finished:
            for i in (a.get("result") or {}).get("inputs", []):
                key = (int(i.get("cadu_frames") or 0), int(i.get("images") or 0))
                if best is None or key > best[0]:
                    best = (key, i, a)
        frames, images = best[0] if best else (0, 0)
        out["frames"], out["images"] = frames, images
        a = best[2] if best else finished[-1]
        if images:
            out["decode"], out["decode_note"] = "image", f"{images} image(s) with content, {frames} frames ({a['attempt_id']})"
        elif frames:
            out["decode"], out["decode_note"] = "frames", f"{frames} frames but no image with content ({a['attempt_id']})"
        else:
            reason = next((x.get("status_reason") for x in reversed(finished) if x.get("status_reason")), None)
            out["decode"], out["decode_note"] = "no frames", reason or "the decoder never achieved frame sync"
        synced = max((int(i.get("synced_reports") or 0) for x in finished
                      for i in (x.get("result") or {}).get("inputs", [])), default=0)
        if frames or synced:
            out["signal"], out["signal_note"] = "detected", ("the decoder locked: " + (f"{frames} frames" if frames else
                                                                                       f"{synced} SYNCED reports"))
    elif decodes:
        out["decode"] = "in progress"
    elif (job.get("result") or {}).get("decode_not_started"):
        out["decode"], out["decode_note"] = "not started", job["result"]["decode_not_started"]
    if out["signal"] != "detected":
        best_db = None
        for a in attempts:
            if a["kind"] != "analyze" or a["status"] != "completed":
                continue
            for i in (a.get("result") or {}).get("inputs", []):
                t = i.get("target") or {}
                if t.get("mean_over_floor_db") is not None:
                    best_db = max(best_db if best_db is not None else -1e9, float(t["mean_over_floor_db"]))
        if best_db is not None:
            if best_db >= SIGNAL_DB:
                out["signal"], out["signal_note"] = "detected", (f"averaged power at the target frequency {best_db:.1f} dB over "
                                                                 "the noise floor (a candidate: the analysis does not identify it)")
            elif out["frames"] is not None:
                out["signal"], out["signal_note"] = "not detected", (f"no decoder lock, and the target averaged only "
                                                                     f"{best_db:.1f} dB over the floor")
            else:
                out["signal"], out["signal_note"] = "not detected", f"the target averaged only {best_db:.1f} dB over the floor"
        elif out["frames"] == 0:
            out["signal"], out["signal_note"] = "not detected", "the decoder found no frame sync"
    level = "recorded"
    if out["signal"] == "detected":
        level = "signal"
    if out["frames"]:
        level = "frames"
    if out["images"]:
        level = "image"
    out["level"] = "failed" if rec == "failed" else level     # a failed recording is listed as failed, whatever it holds
    return out


def archive_record(job: dict[str, Any]) -> dict[str, Any]:
    return dict(job.get("archive") or {})


def remote_copy(job: dict[str, Any]) -> dict[str, Any] | None:
    """The raw IQ a receiver host kept (transfer policy keep_on_host), if it is still only there."""
    rf = archive_record(job).get("remote")
    return rf if rf and not rf.get("removed_at") else None


def local_raw(job: dict[str, Any]) -> pathlib.Path | None:
    raw = (job.get("paths") or {}).get("raw")
    for p in ((pathlib.Path(raw), pathlib.Path(raw + ".part")) if raw else ()):
        if p.is_file():
            return p
    return None


def location(job: dict[str, Any]) -> dict[str, Any]:
    """Where the raw IQ is now: here, on a receiver host, both, or gone (and why)."""
    here = local_raw(job)
    rf = remote_copy(job)
    gone = [d for d in archive_record(job).get("deletions", []) if d.get("kind") == "raw_iq"]
    if job["source"] == "legacy":
        files = [f for f in (job.get("result") or {}).get("legacy_files", []) if pathlib.Path(f.get("raw", "")).is_file()]
        return {"where": "here" if files else "gone", "host": "local",
                "note": None if files else "the legacy recorder's own retention removes its files"}
    if here and rf:
        return {"where": "both", "host": rf["host"], "path": str(here), "remote_path": rf["path"]}
    if here:
        return {"where": "here", "host": "local", "path": str(here)}
    if rf:
        return {"where": "remote", "host": rf["host"], "remote_path": rf["path"], "bytes": rf.get("bytes")}
    return {"where": "gone", "host": None, "note": (f"deleted {fmt_clock(gone[-1]['at'], day=True)} by {gone[-1]['basis']}" if gone
                                                     else "no raw IQ on record")}


def storage_usage(s: Any) -> dict[str, Any]:
    """Disk space of every storage folder, what the archive holds by kind and by receiver host, and the raw IQ
    kept on remote hosts."""
    st = s.settings
    roots = {"manual": st.get("default_output_root"), "automatic": st.get("auto_output_root"),
             "tests": st.get("test_output_root") or (str(pathlib.Path(st.get("default_output_root") or "/data") / "tests")),
             "processing": st.get("processing_root") or str(s.config.data_root / "processing")}
    disks: dict[str, dict[str, Any]] = {}
    folders = []
    for label, root in roots.items():
        if not root:
            continue
        p = pathlib.Path(root)
        probe = next((q for q in (p, *p.parents) if q.exists()), None)
        entry: dict[str, Any] = {"label": label, "path": root, "exists": p.exists()}
        if probe is not None:
            try:
                du = shutil.disk_usage(probe)
                dev = os.stat(probe).st_dev
                entry.update(device=dev, total_bytes=du.total, free_bytes=du.free, used_bytes=du.used)
                disks.setdefault(str(dev), {"total_bytes": du.total, "free_bytes": du.free, "used_bytes": du.used,
                                            "folders": []})["folders"].append(label)
            except OSError as exc:
                entry["error"] = str(exc)
        folders.append(entry)
    with s.engine.connect() as conn:
        by_kind = {r.kind: {"files": int(r.n), "bytes": int(r.b or 0)} for r in conn.execute(
            select(artifacts.c.kind, func.count().label("n"), func.sum(artifacts.c.bytes).label("b"))
            .where(and_(artifacts.c.deleted_at.is_(None), artifacts.c.host.is_(None))).group_by(artifacts.c.kind))}
        remote = {r.host: {"files": int(r.n), "bytes": int(r.b or 0)} for r in conn.execute(
            select(artifacts.c.host, func.count().label("n"), func.sum(artifacts.c.bytes).label("b"))
            .where(and_(artifacts.c.deleted_at.is_(None), artifacts.c.host.is_not(None))).group_by(artifacts.c.host))}
        by_source = {r.source: {"files": int(r.n), "bytes": int(r.b or 0)} for r in conn.execute(
            select(sched_jobs.c.source, func.count().label("n"), func.sum(artifacts.c.bytes).label("b"))
            .select_from(artifacts.join(sched_jobs, artifacts.c.recording_id == sched_jobs.c.job_id))
            .where(and_(artifacts.c.deleted_at.is_(None), artifacts.c.host.is_(None), artifacts.c.kind == "raw_iq"))
            .group_by(sched_jobs.c.source))}
        pending = conn.execute(select(func.count()).select_from(proc_attempts)
                               .where(proc_attempts.c.status.in_(PENDING))).scalar() or 0
    total = sum(v["bytes"] for v in by_kind.values())
    return {"folders": folders, "disks": list(disks.values()), "by_kind": by_kind, "raw_iq_by_source": by_source,
            "remote_hosts": remote, "indexed_bytes": total, "pending_attempts": int(pending),
            "reserve_free_gib": st.get("reserve_free_gib")}
