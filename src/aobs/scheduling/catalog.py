"""Recording catalog: queries, job detail/clone, cancellation, historical import, metadata
correction and expiring download links. The recording id is the sched_jobs id of the job that
produced it (m…, a…, l… for legacy windows, r… for imported files)."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import signal
import zoneinfo
from typing import Any

from sqlalchemy import and_, or_, select

from ..db.schema import artifacts, proc_attempts, sched_calendar, sched_jobs
from . import archive
from .store import ACTIVE, Scheduler, SchedulerError, new_job_id, row_dict

RECORDED = ("recorded", "processing", "completed", "partial")
REC_STATES = (*RECORDED, "failed")
IQ_EXT = {".cu8": "cu8", ".cs8": "cs8", ".cs16": "cs16", ".cf32": "cf32"}


def _confirm_code(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:6]


class Catalog:
    def __init__(self, s: Scheduler, processor: Any = None):
        self.s, self.p = s, processor
        self.tz = zoneinfo.ZoneInfo(s.config.display_tz)

    # ================================================================ recordings
    def _processing_state(self, recording_id: str, job: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.s.engine.connect() as conn:
            atts = conn.execute(select(proc_attempts).where(proc_attempts.c.recording_id == recording_id)
                                .order_by(proc_attempts.c.queued_at)).mappings().all()
            kinds = [r[0] for r in conn.execute(select(artifacts.c.kind).where(and_(
                artifacts.c.recording_id == recording_id, artifacts.c.deleted_at.is_(None))))]

        def state(kind: str) -> str:
            mine = [a for a in atts if a["kind"] == kind]
            if not mine:
                return "none"
            if any(a["status"] in ("queued", "running", "paused") for a in mine):
                return "in progress"
            if kind == "decode":
                if any(any(i.get("decoded") for i in (a["result"] or {}).get("inputs", [])) for a in mine):
                    return "decoded"
                return "no images" if any(a["status"] == "completed" for a in mine) else "failed"
            return "done" if any(a["status"] == "completed" for a in mine) else "failed"
        out = {"decode": state("decode"), "analysis": state("analyze"), "attempts": len(atts),
               "artifacts": {k: kinds.count(k) for k in sorted(set(kinds))}}
        if job is not None:
            out["outcome"] = archive.outcome(job, [dict(a) for a in atts])
        return out

    def describe(self, j: dict[str, Any]) -> dict[str, Any]:
        spec = j.get("spec") or {}
        radio, rec = spec.get("radio") or {}, spec.get("recording") or {}
        cap = (j.get("result") or {}).get("capture") or {}
        legacy = (j.get("result") or {}).get("legacy_files") or []
        size = cap.get("bytes") or sum(int(f.get("bytes") or 0) for f in legacy) or None
        start, end = dt.datetime.fromisoformat(j["record_start"]), dt.datetime.fromisoformat(j["record_end"])
        rx = self.s.receivers.get(j["receiver_id"])
        host = ((cap.get("remote") or {}).get("host") or (j.get("execution") or {}).get("host")
                or (getattr(rx, "host", None) if rx is not None else None) or "local")
        if cap.get("actual_start") and cap.get("actual_end"):
            try:
                start = dt.datetime.fromisoformat(cap["actual_start"])
                end = dt.datetime.fromisoformat(cap["actual_end"])
            except ValueError:
                pass
        d = {"recording_id": j["job_id"], "target": j.get("target"), "receiver": j["receiver_id"], "source": j["source"],
             "start": start.isoformat(), "start_local": start.astimezone(self.tz).strftime("%Y-%m-%d %-I:%M %p %Z"),
             "end": end.isoformat(), "end_local": end.astimezone(self.tz).strftime("%Y-%m-%d %-I:%M %p %Z"),
             "duration_s": int((end - start).total_seconds()), "host": host, "job_name": j.get("job_name"),
             "location": archive.location(j), "keep": bool((j.get("archive") or {}).get("keep")),
             "status_reason": j.get("status_reason"), "center_frequency_hz": radio.get("center_frequency_hz"),
             "target_frequency_hz": radio.get("target_frequency_hz"), "sample_rate_hz": radio.get("sample_rate_hz"),
             "format": rec.get("format"), "size_bytes": size, "status": j["status"],
             "experiment": (spec.get("metadata") or {}).get("experiment") or None,
             "uncertain_metadata": (spec.get("import") or {}).get("uncertain") or None,
             "signal_path": ((spec.get("resolved") or {}).get("rf_path") or {}).get("text"),
             "rf_path": (spec.get("resolved") or {}).get("rf_path")}
        d.update(self._processing_state(j["job_id"], j))
        return d

    def recordings(self, receiver: str | None = None, satellite: str | None = None, date: str | None = None,
                   since: str | None = None, until: str | None = None, frequency_hz: int | None = None,
                   fmt: str | None = None, status: str | None = None, decoded: bool | None = None,
                   analyzed: bool | None = None, min_size_gb: float | None = None, experiment: str | None = None,
                   recording_id: str | None = None, page: int = 1, per_page: int = 10, q: str | None = None,
                   host: str | None = None, outcome: str | None = None, where: str | None = None,
                   source: str | None = None, sort: str = "newest") -> dict[str, Any]:
        query = select(sched_jobs).where(or_(sched_jobs.c.status.in_(RECORDED),
                                        and_(sched_jobs.c.status == "failed", sched_jobs.c.source != "legacy")))
        if recording_id:
            query = select(sched_jobs).where(sched_jobs.c.job_id == recording_id)
        if receiver:
            rx = self.s.receivers.get(receiver.upper()) or next((r for r in self.s.receivers.values()
                                                                 if r.serial == receiver or receiver.upper().replace("SDRPLAY-", "") == r.id), None)
            query = query.where(sched_jobs.c.receiver_id == (rx.id if rx else receiver))
        if satellite:
            query = query.where(or_(sched_jobs.c.target.ilike(f"%{satellite.replace('-', '%')}%"), sched_jobs.c.job_name.ilike(f"%{satellite}%")))
        if date:
            day = dt.date.fromisoformat(date)
            a = dt.datetime.combine(day, dt.time(), self.tz)
            query = query.where(and_(sched_jobs.c.record_start >= a, sched_jobs.c.record_start < a + dt.timedelta(days=1)))
        if since:
            query = query.where(sched_jobs.c.record_start >= self._when(since))
        if until:
            query = query.where(sched_jobs.c.record_start < self._when(until, end_of_day=True))
        if status:
            query = query.where(sched_jobs.c.status.in_(status.split(",")))
        if source:
            query = query.where(sched_jobs.c.source.in_(source.split(",")))
        if q:
            like = f"%{q.strip()[:80]}%"
            query = query.where(or_(sched_jobs.c.job_id.ilike(like), sched_jobs.c.target.ilike(like),
                                    sched_jobs.c.job_name.ilike(like), sched_jobs.c.receiver_id.ilike(like)))
        order = sched_jobs.c.record_start.asc() if sort == "oldest" else sched_jobs.c.record_start.desc()
        with self.s.engine.connect() as conn:
            rows = [row_dict(r) for r in conn.execute(query.order_by(order).limit(2000)).mappings()]
        out = []
        for j in rows:
            if j["source"] == "legacy" and not (j.get("result") or {}).get("legacy_files"):
                continue
            d = self.describe(j)
            if frequency_hz and not (d["center_frequency_hz"] and d["sample_rate_hz"]
                                     and abs(frequency_hz - d["center_frequency_hz"]) < d["sample_rate_hz"] / 2):
                continue
            if fmt and d["format"] != fmt:
                continue
            if decoded is not None and (d["decode"] == "decoded") != decoded:
                continue
            if analyzed is not None and (d["analysis"] == "done") != analyzed:
                continue
            if min_size_gb and (d["size_bytes"] or 0) < min_size_gb * 1e9:
                continue
            if experiment and (d["experiment"] or "").lower() != experiment.lower():
                continue
            if host and d["host"] != host:
                continue
            if outcome and d["outcome"]["level"] not in outcome.split(","):
                continue
            if where and d["location"]["where"] not in where.split(","):
                continue
            out.append(d)
        if sort == "largest":
            out.sort(key=lambda d: -(d["size_bytes"] or 0))
        per_page = max(1, min(per_page, 50))
        pages = max(1, -(-len(out) // per_page))
        page = max(1, min(page, pages))
        return {"total": len(out), "page": page, "pages": pages, "per_page": per_page,
                "recordings": out[(page - 1) * per_page: page * per_page]}

    def _when(self, value: str, end_of_day: bool = False) -> dt.datetime:
        """ISO time or date; without a zone it is the observatory's display time zone. A bare date as an upper
        bound means the end of that day."""
        try:
            if len(value) == 10:
                day = dt.datetime.combine(dt.date.fromisoformat(value), dt.time(), self.tz)
                return day + dt.timedelta(days=1) if end_of_day else day
            t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise SchedulerError(f"{value!r} is not a date or ISO-8601 time", 400) from None
        return t if t.tzinfo else t.replace(tzinfo=self.tz)

    def detail(self, recording_id: str) -> dict[str, Any]:
        """Everything the archive page shows for one recording: its details and the four outcomes, where its
        files are, images, decoder logs and other files, the processing history (attempts, transfers, deletions,
        state changes) and what retention will do with its raw IQ."""
        j = self.s.get(recording_id)
        if j is None:
            raise SchedulerError(f"no recording {recording_id}", 404)
        if j["status"] not in REC_STATES:
            raise SchedulerError(f"{recording_id} is {j['status']}; it has no recording yet", 409)
        d = self.describe(j)
        atts = self.p.attempts(recording_id) if self.p else []
        with self.s.engine.connect() as conn:
            rows = conn.execute(select(artifacts).where(artifacts.c.recording_id == recording_id)
                                .order_by(artifacts.c.created_at, artifacts.c.path)).mappings().all()
        files = []
        for a in rows:
            f = {k: (v.isoformat() if isinstance(v, dt.datetime) else v) for k, v in dict(a).items()}
            f["name"] = pathlib.Path(a["path"]).name
            f["available"] = a["deleted_at"] is None and a["host"] is None and os.path.exists(a["path"])
            f["viewable"] = f["available"] and a["kind"] in ("image", "waterfall", "spectrum") and                 (a["mime"] or "").startswith("image/")
            f["readable"] = f["available"] and a["kind"] in ("log", "capture_log", "diagnostics", "metadata", "product") \
                and pathlib.Path(a["path"]).suffix.lower() in (".log", ".json", ".txt", ".csv")
            files.append(f)
        history: list[dict[str, Any]] = [{"at": e["at"], "what": "state", "text": f"{e.get('old_status') or '—'} → "
                                          f"{e['new_status']}" + (f": {e['reason']}" if e.get("reason") else ""),
                                          "by": e.get("actor")} for e in j.get("events", [])]
        for a in atts:
            b = _attempt_brief(a)
            history.append({"at": a["queued_at"], "what": a["kind"], "attempt_id": a["attempt_id"], "status": a["status"],
                            "text": f"{a['kind']} {a['attempt_id']} {a['status']}" + (f": {a['status_reason']}"
                                                                                     if a.get("status_reason") else ""),
                            "by": a.get("requested_by") or a["origin"], "brief": b})
        arch = j.get("archive") or {}
        for t in arch.get("transfers", []):
            history.append({"at": t["at"], "what": "transfer", "text": f"copied from {t['host']}: "
                            f"{(t.get('bytes') or 0) / 1e9:.2f} GB, verified by {t.get('verified')}"
                            + ("" if t.get("host_copy_removed") else "; the host copy is still there"), "by": "transfer"})
        for x in arch.get("deletions", []):
            history.append({"at": x["at"], "what": "deleted", "text": f"raw IQ deleted ({x['host']}:{x['path']}): "
                            f"{x['basis']}" + (f" — FAILED: {x['error']}" if x.get("error") else ""), "by": "retention"})
        history.sort(key=lambda h: h["at"] or "")
        from .retention import Retention
        ret = Retention(self.s).assess(j, atts, self.s.clock())
        store = getattr(self.s, "profiles", None)
        profiles = []
        if store is not None:
            best = store.match(j)
            profiles = [{**{k: p[k] for k in ("profile_id", "name", "pipeline", "installed", "enabled", "revision")},
                         "suggested": bool(best and best["profile_id"] == p["profile_id"])} for p in store.list()]
        spec = j.get("spec") or {}
        cap = (j.get("result") or {}).get("capture") or {}
        return {"recording": d, "outcome": d.get("outcome"), "files": files, "attempts": [_attempt_brief(a) for a in atts],
                "history": history, "retention": ret, "archive": arch, "profiles": profiles,
                "capture": {k: cap.get(k) for k in ("bytes", "expected_bytes", "samples", "overflows", "actual_start",
                                                       "actual_end", "validation", "iq_health", "transfer", "remote_file")},
                "request": {"radio": spec.get("radio"), "recording": spec.get("recording"), "satdump": spec.get("satdump"),
                            "postprocess": spec.get("postprocess"), "metadata": spec.get("metadata")},
                "can_process": bool(self.p and self.p.inputs(j)), "transfer_pending": bool(self.p and self.p._pending(
                    recording_id, "transfer"))}

    def results(self, recording_id: str, kind: str | None = None, attempt_id: str | None = None) -> dict[str, Any]:
        j = self.s.get(recording_id)
        if j is None:
            raise SchedulerError(f"no recording {recording_id}", 404)
        atts = self.p.attempts(recording_id) if self.p else []
        kinds = {"images": "image", "image": "image", "waterfall": "waterfall", "waterfalls": "waterfall",
                 "spectrum": "spectrum", "plots": "waterfall,spectrum,diagnostics", "diagnostics": "diagnostics",
                 "logs": "log,capture_log", "log": "log,capture_log", "metadata": "metadata", "raw": "raw_iq",
                 "products": "product", "all": None}
        if kind and kind not in kinds:
            raise SchedulerError(f"type must be one of {sorted(kinds)}", 400)
        arts = self.p.artifacts(recording_id, kinds.get(kind) if kind else None, attempt_id) if self.p else []
        latest_ok = {}
        for a in atts:
            if a["status"] == "completed":
                ok = a["kind"] != "decode" or any(i.get("decoded") for i in (a.get("result") or {}).get("inputs", []))
                if ok:
                    latest_ok[a["kind"]] = a["attempt_id"]
        checksums = [i.get("sha256") for a in atts for i in (a.get("result") or {}).get("inputs", []) if i.get("sha256")]
        return {"recording": self.describe(j), "attempts": [_attempt_brief(a) for a in atts],
                "latest_successful": latest_ok, "artifacts": arts, "checksums_sha256": sorted(set(checksums))}

    # ================================================================ jobs
    def job_detail(self, job_id: str) -> dict[str, Any]:
        j = self.s.get(job_id)
        if j is None:
            raise SchedulerError(f"no job {job_id}", 404)
        with self.s.engine.connect() as conn:
            cal = conn.execute(select(sched_calendar).where(sched_calendar.c.job_id == job_id)).mappings().first()
        j["calendar"] = ({"event_id": cal["event_id"], "state": "error" if cal["last_error"] else "synced",
                          "error": cal["last_error"], "synced_at": cal["synced_at"].isoformat() if cal["synced_at"] else None}
                         if cal else {"state": "not published"})
        j["processing"] = self._processing_state(job_id) if j["status"] in REC_STATES else None
        return j

    def jobs(self, when: str | None = None, state: str | None = None, receiver: str | None = None,
             limit: int = 40) -> list[dict[str, Any]]:
        now = self.s.clock()
        start, end = now - dt.timedelta(hours=6), now + dt.timedelta(hours=48)
        if when in ("today", "tomorrow"):
            day = now.astimezone(self.tz).date() + dt.timedelta(days=1 if when == "tomorrow" else 0)
            start = dt.datetime.combine(day, dt.time(), self.tz)
            end = start + dt.timedelta(days=1)
        statuses = {"active": ["preparing", "recording"], "queued": ["scheduled"], "scheduled": ["scheduled"],
                    "failed": ["failed", "missed"], "preempted": ["preempted", "skipped"], None: None}.get(state)
        if state == "failed":
            start = now - dt.timedelta(days=7)
        rows = self.s.jobs(receiver=receiver, status=statuses, start=start, end=end, limit=1000)
        with self.s.engine.connect() as conn:
            cal = {r.job_id: r for r in conn.execute(select(sched_calendar))}
        out = []
        for j in rows:
            if j["status"] == "shadow":
                continue
            c = cal.get(j["job_id"])
            out.append({"job_id": j["job_id"], "receiver": j["receiver_id"], "target": j["target"] or j["job_name"],
                        "start": j["record_start"], "end": j["record_end"], "source": j["source"],
                        "priority": "manual (high)" if j["source"] == "manual" else
                        "automatic (locked: legacy service)" if j["source"] == "legacy" else "automatic (preemptible)",
                        "status": j["status"],
                        "conflict": (f"preempted by {j['preempted_by']}" if j.get("preempted_by") else
                                     j["status_reason"] if j["status"] == "skipped" else "none"),
                        "calendar": "error" if c is not None and c.last_error else "synced" if c is not None else "not published",
                        "recording_id": j["job_id"] if j["status"] in REC_STATES else None})
        return out[:limit]

    def clone(self, job_id: str, start: str, end: str | None = None, duration_seconds: int | None = None,
              receiver_id: str | None = None, job_name: str | None = None, dry_run: bool = True,
              requested_by: str = "") -> dict[str, Any]:
        j = self.s.get(job_id)
        if j is None:
            raise SchedulerError(f"no job {job_id}", 404)
        src = copy.deepcopy(j["spec"])
        if j["source"] in ("legacy", "import"):
            raise SchedulerError("only broker-run jobs carry complete settings to clone", 400)
        for k in ("resolved", "job_id", "estimated_bytes", "legacy_unit"):
            src.pop(k, None)
        src["job_name"] = job_name or f"{j['job_name'][:60]}-clone"
        sch = src.get("schedule") or {}
        sch.update({"start": start, "end": end, "duration_seconds": duration_seconds})
        src["schedule"] = {k: v for k, v in sch.items() if v is not None}
        if receiver_id:
            src["receiver"] = {**src["receiver"], "receiver_id": receiver_id, "serial": None}
        src.setdefault("metadata", {})["cloned_from"] = job_id
        return self.s.submit([src], "all_or_nothing", requested_by, dry_run)

    # ================================================================ cancellation
    def cancel(self, job_id: str | None = None, attempt_id: str | None = None, confirm: str | None = None,
               reason: str = "cancelled from Slack", actor: str = "slack") -> dict[str, Any]:
        if attempt_id:
            return self.cancel_attempt(attempt_id)
        j = self.s.get(job_id or "")
        if j is None:
            raise SchedulerError(f"no job {job_id}", 404)
        if j["status"] in ("cancelled",):
            return {"job_id": job_id, "status": "cancelled", "already": True}
        if j["status"] == "scheduled":
            return self.s.cancel(job_id, reason, actor)
        if j["status"] == "preparing":
            self.s.transition(job_id, "cancelled", reason, actor, expect=("preparing",))
            return {"job_id": job_id, "status": "cancelled", "note": "preparation stopped; the receiver is released"}
        if j["status"] == "recording":
            code = _confirm_code(job_id, "terminate")
            if confirm != code:
                return {"job_id": job_id, "needs_confirmation": True, "confirm": code,
                        "warning": f"{job_id} is RECORDING on {j['receiver_id']}. Terminating keeps what was captured "
                                   f"and marks it partial. Repeat with confirm={code} to stop it."}
            pid = j.get("pid")
            if not pid:
                raise SchedulerError("no capture process recorded for this job")
            runner = getattr(self.s, "runner", None)
            if runner is not None:            # local or remote: through the backend that started it (brief 7)
                runner.terminate(j)
            else:
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            self.s.update(job_id, status_reason=f"termination requested by {actor}: {reason}")
            return {"job_id": job_id, "status": "terminating", "note": "captured IQ is kept; the job ends as partial"}
        raise SchedulerError(f"{job_id} is {j['status']}; nothing to cancel")

    def cancel_attempt(self, attempt_id: str) -> dict[str, Any]:
        a = self.p.attempt(attempt_id)
        if a["status"] in ("completed", "failed", "cancelled"):
            return {"attempt_id": attempt_id, "status": a["status"], "already": True}
        if a["status"] == "queued":
            return self.p.cancel(attempt_id)
        cur = self.p.current
        if cur and cur["attempt_id"] == attempt_id:
            try:
                os.killpg(cur["pid"], signal.SIGCONT)
                os.killpg(cur["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.p._set(attempt_id, status="cancelled", status_reason="cancelled while running; partial outputs kept",
                    finished_at=self.s.clock())
        return {"attempt_id": attempt_id, "status": "cancelled", "note": "the source recording is untouched"}

    def cancel_batch(self, receiver: str | None = None, source: str | None = None, start: str | None = None,
                     end: str | None = None, job_ids: list[str] | None = None, confirm: str | None = None,
                     reason: str = "batch cancel", actor: str = "slack") -> dict[str, Any]:
        now = self.s.clock()
        rows = self.s.jobs(receiver=receiver, status=["scheduled"], source=source,
                           start=dt.datetime.fromisoformat(start) if start else now,
                           end=dt.datetime.fromisoformat(end) if end else now + dt.timedelta(days=31), limit=500)
        rows = [r for r in rows if r["executes"] and (not job_ids or r["job_id"] in job_ids)]
        ids = sorted(r["job_id"] for r in rows)
        if not ids:
            return {"matched": 0, "cancelled": []}
        code = _confirm_code(*ids)
        if confirm != code:
            return {"matched": len(ids), "needs_confirmation": True, "confirm": code,
                    "preview": [{"job_id": r["job_id"], "receiver": r["receiver_id"], "target": r["target"],
                                 "start": r["record_start"], "source": r["source"]} for r in rows],
                    "note": "only scheduled broker jobs are included; running recordings must be cancelled one by one"}
        done = []
        for jid in ids:
            try:
                self.s.cancel(jid, reason, actor)
                done.append(jid)
            except SchedulerError:
                continue
        return {"matched": len(ids), "cancelled": done}

    # ================================================================ import
    def import_recordings(self, roots: list[str] | None = None, dry_run: bool = True) -> dict[str, Any]:
        allowed = [pathlib.Path(r) for r in self.s.settings.get("import_roots", [])]
        roots_p = [pathlib.Path(r) for r in (roots or [str(a) for a in allowed])]
        for r in roots_p:
            if not any(r == a or a in r.parents for a in allowed):
                raise SchedulerError(f"{r} is not under a configured import root {', '.join(map(str, allowed))}", 400)
        with self.s.engine.connect() as conn:
            known = set(conn.execute(select(artifacts.c.path)).scalars())
            for res in conn.execute(select(sched_jobs.c.result).where(sched_jobs.c.source == "legacy")).scalars():
                known |= {f["raw"] for f in (res or {}).get("legacy_files", [])}
            for pth in conn.execute(select(sched_jobs.c.paths)).scalars():
                if pth and pth.get("raw"):
                    known.add(pth["raw"])
        found, skipped = [], []
        now = dt.datetime.now(dt.timezone.utc).timestamp()
        for root in roots_p:
            if not root.is_dir():
                skipped.append({"path": str(root), "why": "not a directory on this host"})
                continue
            for f in sorted(root.rglob("*")):
                rel = f.relative_to(root)
                if len(rel.parts) > 3 or any(p in ("processing", "pass-cache", "decoded") or p.startswith(".") for p in rel.parts[:-1]):
                    continue
                if f.suffix not in IQ_EXT or not f.is_file() or f.name.startswith("."):
                    continue
                if str(f) in known:
                    continue
                if now - f.stat().st_mtime < 120:
                    skipped.append({"path": str(f), "why": "modified in the last 2 minutes (may still be recording)"})
                    continue
                found.append(self._describe_file(f))
        created = []
        if not dry_run:
            for item in found:
                created.append(self._insert_import(item))
        return {"dry_run": dry_run, "roots": [str(r) for r in roots_p], "found": len(found),
                "files": found[:200], "skipped": skipped[:50], "created": created}

    def _describe_file(self, f: pathlib.Path) -> dict[str, Any]:
        fmt = IQ_EXT[f.suffix]
        ev: dict[str, Any] = {"format": f"file extension {f.suffix}"}
        meta: dict[str, Any] = {"format": fmt}
        side = f.with_suffix(".json")
        if side.is_file():
            try:
                d = json.loads(side.read_text())
                for k_src, k in (("frequency_hz", "center_frequency_hz"), ("sample_rate", "sample_rate_hz"),
                                 ("sample_rate_hz", "sample_rate_hz"), ("center_frequency_hz", "center_frequency_hz")):
                    if d.get(k_src):
                        meta[k] = int(d[k_src])
                        ev[k] = f"sidecar {side.name}:{k_src}"
                for k_src in ("started_at", "start_utc"):
                    if d.get(k_src):
                        meta["start"] = d[k_src]
                        ev["start"] = f"sidecar {side.name}:{k_src}"
                for k_src in ("finished_at", "end_utc"):
                    if d.get(k_src):
                        meta["end"] = d[k_src]
                sats = d.get("satellites") or [s.get("name") for s in (d.get("pass") or {}).get("satellites", [])]
                if sats:
                    meta["target"] = " + ".join(s for s in sats if s)
                    ev["target"] = f"sidecar {side.name}"
                if d.get("receiver"):
                    meta["receiver"] = d["receiver"]
                    ev["receiver"] = f"sidecar {side.name}:receiver"
                meta["sidecar"] = str(side)
            except (OSError, ValueError):
                pass
        m = re.match(r"^rsp1b_(\d{9,11})_(\d{6,10})_(\d{5,9})$", f.stem)
        if m:
            meta.setdefault("start", dt.datetime.fromtimestamp(int(m.group(1)), dt.timezone.utc).isoformat())
            ev.setdefault("start", "filename epoch (record-meteor.py naming)")
            meta.setdefault("center_frequency_hz", int(m.group(2)))
            ev.setdefault("center_frequency_hz", "filename (record-meteor.py naming)")
            meta.setdefault("sample_rate_hz", int(m.group(3)))
            ev.setdefault("sample_rate_hz", "filename (record-meteor.py naming)")
            meta.setdefault("receiver", "RSP1B")
            ev.setdefault("receiver", "filename prefix rsp1b_")
        m = re.match(r"^(OBS-R0\d)_", f.name)
        if m and "receiver" not in meta:
            meta["receiver"], ev["receiver"] = m.group(1), "filename prefix"
        if "receiver" not in meta and "/sdrplay/" in str(f):
            meta["receiver"], ev["receiver"] = "RSP1B", "directory /data/sdrplay"
        uncertain = [k for k in ("center_frequency_hz", "sample_rate_hz", "start", "receiver") if k not in meta]
        size = f.stat().st_size
        if "start" not in meta:
            meta["start"] = dt.datetime.fromtimestamp(f.stat().st_mtime, dt.timezone.utc).isoformat()
            ev["start"] = "file modification time (end of writing, approximate)"
        return {"path": str(f), "bytes": size, "metadata": meta, "evidence": ev, "uncertain": uncertain}

    def _insert_import(self, item: dict[str, Any]) -> str:
        m = item["metadata"]
        start = dt.datetime.fromisoformat(m["start"].replace("Z", "+00:00"))
        rate = m.get("sample_rate_hz")
        bpsamp = {"cu8": 2, "cs8": 2, "cs16": 4, "cf32": 8}[m["format"]]
        if m.get("end"):
            end = dt.datetime.fromisoformat(m["end"].replace("Z", "+00:00"))
        elif rate:
            end = start + dt.timedelta(seconds=item["bytes"] / bpsamp / rate)
        else:
            end = start + dt.timedelta(seconds=1)
        if item["evidence"].get("start", "").startswith("file modification") and rate:
            start, end = start - dt.timedelta(seconds=item["bytes"] / bpsamp / rate), start
        rx = self.s.receivers.get(m.get("receiver") or "")
        jid = new_job_id("r", start)
        spec = {"job_name": pathlib.Path(item["path"]).name, "target": {"type": "manual", "name": m.get("target") or "unknown"},
                "radio": {"center_frequency_hz": m.get("center_frequency_hz"), "sample_rate_hz": rate,
                          "target_frequency_hz": m.get("target_frequency_hz")},
                "recording": {"format": m["format"]},
                "receiver": {"receiver_id": rx.id if rx else m.get("receiver"), "antenna": rx.antenna if rx else None,
                             "signal_path": rx.signal_path if rx else None},
                "import": {"evidence": item["evidence"], "uncertain": item["uncertain"], "source_file": item["path"],
                           "imported_at": self.s.clock().isoformat(), "corrections": []},
                "resolved": {"receiver_id": rx.id if rx else m.get("receiver"), "record_start": start.isoformat(),
                             "record_end": end.isoformat()}}
        now = self.s.clock()
        with self.s.engine.begin() as conn:
            conn.execute(sched_jobs.insert().values(
                job_id=jid, job_name=spec["job_name"][:128], source="import", receiver_id=(rx.id if rx else m.get("receiver") or "UNKNOWN"),
                device_serial=rx.serial if rx else "unknown", status="recorded",
                status_reason="imported historical recording" + (f"; uncertain: {', '.join(item['uncertain'])}" if item["uncertain"] else ""),
                executes=False, reservation_start=start, reservation_end=end, record_start=start, record_end=end,
                target=m.get("target"), estimated_bytes=item["bytes"], spec=spec,
                paths={"raw": item["path"], "sidecar": m.get("sidecar")}, result={"capture": {"bytes": item["bytes"], "file": item["path"]}},
                created_at=now, updated_at=now))
        if self.p:
            self.p.index(jid, None, [{"kind": "raw_iq", "path": item["path"], "label": "raw IQ (imported)"}]
                         + ([{"kind": "metadata", "path": m["sidecar"], "label": "original sidecar"}] if m.get("sidecar") else []))
        return jid

    def correct_metadata(self, recording_id: str, fields: dict[str, Any], by: str) -> dict[str, Any]:
        allowed = {"center_frequency_hz", "sample_rate_hz", "target_frequency_hz", "format", "target"}
        if set(fields) - allowed:
            raise SchedulerError(f"only {sorted(allowed)} can be corrected", 400)
        j = self.s.get(recording_id)
        if j is None:
            raise SchedulerError(f"no recording {recording_id}", 404)
        if j["source"] not in ("import", "legacy"):
            raise SchedulerError("only imported or legacy recordings can be corrected; broker recordings are exact", 400)
        spec = j["spec"]
        before = {}
        for k, v in fields.items():
            if k == "format":
                if v not in IQ_EXT.values():
                    raise SchedulerError("format must be cu8, cs8, cs16 or cf32", 400)
                before[k] = spec.setdefault("recording", {}).get("format")
                spec["recording"]["format"] = v
            elif k == "target":
                before[k] = j.get("target")
            else:
                if not isinstance(v, int) or v <= 0:
                    raise SchedulerError(f"{k} must be a positive integer (Hz)", 400)
                before[k] = spec.setdefault("radio", {}).get(k)
                spec["radio"][k] = v
        imp = spec.setdefault("import", {"corrections": [], "uncertain": []})
        imp.setdefault("corrections", []).append({"at": self.s.clock().isoformat(), "by": by, "before": before, "after": fields})
        imp["uncertain"] = [u for u in imp.get("uncertain", []) if u not in fields and not (u == "start" and False)]
        values: dict[str, Any] = {"spec": spec}
        if "target" in fields:
            values["target"] = fields["target"]
        self.s.update(recording_id, **values)
        return {"recording_id": recording_id, "corrected": fields, "before": before}

    # ================================================================ download links
    def _key(self) -> bytes:
        p = self.s.config.data_root / "secrets" / "download_link_key"
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(secrets.token_hex(32))
            os.chmod(p, 0o600)
        return bytes.fromhex(p.read_text().strip())

    def link(self, artifact_id: str, ttl_s: int = 3600, base_url: str = "") -> dict[str, Any]:
        ttl_s = max(60, min(int(ttl_s), 24 * 3600))
        with self.s.engine.connect() as conn:
            row = conn.execute(select(artifacts).where(artifacts.c.artifact_id == artifact_id)).mappings().first()
        if row is None or row["deleted_at"] is not None or not os.path.exists(row["path"]):
            raise SchedulerError(f"no artifact {artifact_id} on disk", 404)
        exp = int(self.s.clock().timestamp()) + ttl_s
        sig = hmac.new(self._key(), f"{artifact_id}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return {"url": f"{base_url.rstrip('/')}/dl/{artifact_id}/{exp}/{sig}", "expires_at":
                dt.datetime.fromtimestamp(exp, dt.timezone.utc).isoformat(), "bytes": row["bytes"],
                "note": "works only from the tailnet; expires; one artifact only"}

    def resolve_link(self, artifact_id: str, exp: int, sig: str) -> pathlib.Path:
        good = hmac.new(self._key(), f"{artifact_id}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(good, sig):
            raise SchedulerError("invalid link", 403)
        if exp < self.s.clock().timestamp():
            raise SchedulerError("this link has expired", 410)
        with self.s.engine.connect() as conn:
            row = conn.execute(select(artifacts).where(artifacts.c.artifact_id == artifact_id)).mappings().first()
        if row is None or not os.path.exists(row["path"]):
            raise SchedulerError("the file no longer exists", 410)
        return pathlib.Path(row["path"])


def _attempt_brief(a: dict[str, Any]) -> dict[str, Any]:
    res = a.get("result") or {}
    ins = res.get("inputs") or []
    d = {"attempt_id": a["attempt_id"], "kind": a["kind"], "status": a["status"], "origin": a["origin"],
         "reason": a.get("status_reason"), "queued_at": a["queued_at"], "finished_at": a.get("finished_at"),
         "parameters": a.get("resolved")}
    if a["kind"] == "decode":
        d["images"] = sum(int(i.get("images") or 0) for i in ins)
        d["decoded"] = any(i.get("decoded") for i in ins)
        d["snr_db_max"] = max((i.get("snr_db_max") or 0 for i in ins), default=None) or None
        d["return_codes"] = [i.get("return_code") for i in ins]
        d["duration_s"] = sum(float(i.get("duration_s") or 0) for i in ins)
        d["frames"] = sum(int(i.get("cadu_frames") or 0) for i in ins)
        d["profile"] = (a.get("resolved") or {}).get("profile")
        d["pipeline"] = (a.get("resolved") or {}).get("pipeline")
    elif a["kind"] == "transfer":
        d["transfer"] = ins[0] if ins else None
    elif ins:
        first = ins[0]
        d["summary"] = {k: first.get(k) for k in ("target", "spectrum", "comparison", "warnings") if first.get(k)}
        if first.get("overload"):
            d["summary"]["findings"] = [f for f in first["overload"]["findings"] if f["status"] in ("present", "suspected")]
        if first.get("integrity"):
            d["summary"]["integrity"] = {k: first["integrity"][k] for k in ("consistent", "issues", "duration_s")}
        if first.get("signals"):
            d["summary"]["signals"] = first["signals"][:5]
    return d
