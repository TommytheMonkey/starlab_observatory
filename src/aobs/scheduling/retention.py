"""Raw IQ retention (brief 8): the only code that deletes recordings.

Which policy applies to a recording, first match wins:

  1. kept by hand (archive.keep)                                   -> keep
  2. imported files and legacy-recorder files                      -> keep (Starlab did not record them;
                                                                      the legacy recorder prunes its own)
  3. the booking asked for retain_raw_iq=false                      -> after_decode
  4. booked by a rule: the rule's policy, as stamped at booking     -> keep / hours / after_decode
  5. the class default in Settings -> Storage (manual, automatic, signal-path tests)

`after_decode` deletes only once a decode produced an image with content. `hours` counts from the end of
the recording. Nothing is ever deleted while the recording is still recording or processing, while any
attempt on it is queued or running, when the file lies outside the allowed output roots, or while
retention is switched off (Settings -> Storage). Every deletion is logged on the recording (archive.deletions)
and on its artifact rows (deleted_at, deleted_reason). Sidecars, logs, plots and decoded products are kept.

Raw IQ kept on a receiver host (transfer policy keep_on_host) follows the same policy and is removed there
over SSH.
"""

from __future__ import annotations

import datetime as dt
import logging
import pathlib
from typing import Any

from sqlalchemy import and_, select

from ..db.schema import artifacts, proc_attempts, sched_jobs
from ..logjson import log_event
from .archive import PENDING, local_raw, remote_copy
from .store import row_dict

LOG = logging.getLogger("aobs.retention")
POLICIES = ("keep", "hours", "after_decode")
TERMINAL = ("completed", "failed", "partial")
CLASS_LABEL = {"manual": "manual recordings", "automatic": "automatic recordings", "test": "signal-path tests"}


def job_class(job: dict[str, Any]) -> str:
    meta = (job.get("spec") or {}).get("metadata") or {}
    if meta.get("rf_test"):
        return "test"
    return "automatic" if job["source"] == "auto" or job.get("rule_id") else "manual"


def class_policy(settings: dict[str, Any], cls: str) -> tuple[str, float | None]:
    if cls == "automatic":
        pol = settings.get("auto_raw_policy") or "hours"
        return pol, float(settings.get("auto_retention_hours") or 48) if pol == "hours" else None
    if cls == "test":
        pol = settings.get("test_raw_policy") or "keep"
        return pol, float(settings.get("test_raw_hours") or 720) if pol == "hours" else None
    pol = settings.get("manual_raw_policy") or "keep"
    return pol, float(settings.get("manual_raw_hours") or 720) if pol == "hours" else None


def policy(job: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """{policy, hours, basis} for a recording's raw IQ."""
    arch = job.get("archive") or {}
    spec = job.get("spec") or {}
    meta = spec.get("metadata") or {}
    if arch.get("keep"):
        return {"policy": "keep", "hours": None, "basis": f"kept by {arch.get('keep_by') or 'hand'}"}
    if job["source"] in ("import", "legacy"):
        return {"policy": "keep", "hours": None,
                "basis": "imported files are never deleted" if job["source"] == "import"
                else "legacy recorder files (its own retention applies)"}
    if (spec.get("postprocess") or {}).get("retain_raw_iq") is False:
        return {"policy": "after_decode", "hours": None, "basis": "the booking (retain raw IQ: no)"}
    if job.get("rule_id") and "raw_iq_hours" in meta:
        name = meta.get("rule_name") or job["rule_id"]
        hours = meta.get("raw_iq_hours")
        return {"policy": "hours" if hours else "keep", "hours": float(hours) if hours else None,
                "basis": f"rule {name}"}
    cls = job_class(job)
    pol, hours = class_policy(settings, cls)
    return {"policy": pol, "hours": hours, "basis": f"Settings → Storage ({CLASS_LABEL[cls]})"}


class Retention:
    def __init__(self, s: Any):
        self.s = s

    @property
    def enabled(self) -> bool:
        return bool(self.s.settings.get("retention_enabled", True))

    def _decoded(self, attempts: list[dict[str, Any]]) -> bool:
        return any(a["kind"] == "decode" and a["status"] == "completed"
                   and any(i.get("decoded") for i in (a.get("result") or {}).get("inputs", [])) for a in attempts)

    def assess(self, job: dict[str, Any], attempts: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any]:
        """What the policy says about this recording right now (never deletes)."""
        pol = policy(job, self.s.settings)
        here, rf = local_raw(job), remote_copy(job)
        out = {**pol, "due": False, "due_at": None, "blocked": None,
               "files": ([{"host": "local", "path": str(here), "bytes": here.stat().st_size}] if here else []) +
                        ([{"host": rf["host"], "path": rf["path"], "bytes": rf.get("bytes")}] if rf else [])}
        if not out["files"]:
            out["blocked"] = "no raw IQ left"
            return out
        if pol["policy"] == "keep":
            return out
        end = dt.datetime.fromisoformat(job["record_end"])
        if pol["policy"] == "hours":
            out["due_at"] = (end + dt.timedelta(hours=pol["hours"])).isoformat()
            due = now >= end + dt.timedelta(hours=pol["hours"])
        else:
            due = self._decoded(attempts)
            if not due:
                out["blocked"] = "waiting for a decode that produces an image"
        if job["status"] not in TERMINAL:
            out["blocked"] = f"the recording is {job['status']}"
            due = False
        elif any(a["status"] in PENDING for a in attempts):
            out["blocked"] = "processing is queued or running on it"
            due = False
        elif here is not None and not self._allowed(here):
            out["blocked"] = "the file is outside the allowed output folders"
            due = False
        if due and not self.enabled:
            out["blocked"] = "retention is switched off (Settings → Storage)"
            due = False
        out["due"] = due
        return out

    def _allowed(self, p: pathlib.Path) -> bool:
        roots = [pathlib.Path(r).resolve() for r in self.s.settings.get("allowed_output_roots", ["/data"])]
        try:
            real = p.resolve(strict=True)
        except OSError:
            return False
        return any(r in real.parents for r in roots)

    def _candidates(self, job_ids: list[str] | None = None) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        q = select(sched_jobs).where(and_(sched_jobs.c.status.in_(TERMINAL),
                                          sched_jobs.c.source.not_in(("import", "legacy"))))
        if job_ids is not None:
            q = q.where(sched_jobs.c.job_id.in_(job_ids))
        with self.s.engine.connect() as conn:
            jobs = [row_dict(r) for r in conn.execute(q).mappings()]
            ids = [j["job_id"] for j in jobs]
            atts: dict[str, list[dict[str, Any]]] = {}
            for i in range(0, len(ids), 500):
                for a in conn.execute(select(proc_attempts.c.recording_id, proc_attempts.c.kind, proc_attempts.c.status,
                                             proc_attempts.c.result).where(proc_attempts.c.recording_id.in_(ids[i:i + 500]))):
                    atts.setdefault(a.recording_id, []).append(dict(a._mapping))
        return [(j, atts.get(j["job_id"], [])) for j in jobs]

    def plan(self, horizon_hours: float = 168) -> dict[str, Any]:
        """Due now, and due within the horizon (hours policies), with the policy behind each. Read-only."""
        now = self.s.clock()
        due, upcoming, waiting = [], [], []
        for job, atts in self._candidates():
            a = self.assess(job, atts, now)
            if not a["files"] or a["policy"] == "keep":
                continue
            row = {"recording_id": job["job_id"], "target": job.get("target"), "receiver": job["receiver_id"],
                   "record_end": job["record_end"], **{k: a[k] for k in ("policy", "hours", "basis", "due_at", "blocked", "files")},
                   "bytes": sum(int(f.get("bytes") or 0) for f in a["files"])}
            if a["due"]:
                due.append(row)
            elif a["blocked"]:
                waiting.append(row)
            elif a["due_at"] and dt.datetime.fromisoformat(a["due_at"]) <= now + dt.timedelta(hours=horizon_hours):
                upcoming.append(row)
        upcoming.sort(key=lambda r: r["due_at"] or "")
        return {"enabled": self.enabled, "at": now.isoformat(), "due": due, "upcoming": upcoming, "waiting": waiting,
                "due_bytes": sum(r["bytes"] for r in due), "upcoming_bytes": sum(r["bytes"] for r in upcoming),
                "defaults": {c: dict(zip(("policy", "hours"), class_policy(self.s.settings, c))) for c in CLASS_LABEL}}

    def apply(self, job_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Delete the raw IQ that is due. Returns what was deleted (and what could not be)."""
        if not self.enabled:
            return []
        now = self.s.clock()
        done = []
        for job, atts in self._candidates(job_ids):
            a = self.assess(job, atts, now)
            if not a["due"]:
                continue
            basis = f"retention: {a['basis']}, " + (f"{a['hours']:g} h after the recording" if a["policy"] == "hours"
                                                     else "deleted after a successful decode")
            for f in a["files"]:
                res = self._delete(job, f, basis, now)
                if res:
                    done.append(res)
        return done

    def _delete(self, job: dict[str, Any], f: dict[str, Any], basis: str, now: dt.datetime) -> dict[str, Any] | None:
        jid = job["job_id"]
        entry = {"at": now.isoformat(), "kind": "raw_iq", "host": f["host"], "path": f["path"], "bytes": f.get("bytes"),
                 "basis": basis}
        if f["host"] == "local":
            p = pathlib.Path(f["path"])
            if not self._allowed(p):
                return None
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                LOG.warning("could not delete %s: %s", p, exc)
                return {**entry, "error": str(exc)}
            paths = [str(p)]
        else:
            backends = getattr(self.s, "backends", None)
            try:
                if backends is None:
                    raise RuntimeError("no recording backends on this broker")
                backends.for_host(f["host"]).remove_recording(jid)
            except Exception as exc:  # noqa: BLE001 - the host may be offline; try again next cycle
                LOG.warning("could not delete %s on %s: %s", f["path"], f["host"], exc)
                return {**entry, "error": str(exc)}
            paths = [f["path"]]
        with self.s.engine.begin() as conn:
            conn.execute(artifacts.update().where(and_(artifacts.c.recording_id == jid, artifacts.c.path.in_(paths),
                                                       artifacts.c.deleted_at.is_(None)))
                         .values(deleted_at=now, deleted_reason=basis))
            arch = conn.execute(select(sched_jobs.c.archive).where(sched_jobs.c.job_id == jid)).scalar() or {}
            arch = dict(arch)
            arch["deletions"] = [*arch.get("deletions", []), entry]
            if f["host"] != "local" and arch.get("remote"):
                arch["remote"] = {**arch["remote"], "removed_at": now.isoformat(), "removed_reason": basis}
            conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == jid).values(archive=arch))
        log_event(LOG, "raw IQ deleted", job_id=jid, host=f["host"], path=f["path"], bytes=f.get("bytes"), basis=basis)
        return entry


def set_keep(s: Any, job_id: str, keep: bool, who: str) -> dict[str, Any]:
    """Keep a recording's raw IQ regardless of policy (or hand it back to the policy)."""
    from .store import SchedulerError
    with s.engine.begin() as conn:
        row = conn.execute(select(sched_jobs.c.archive, sched_jobs.c.status).where(sched_jobs.c.job_id == job_id)).first()
        if row is None:
            raise SchedulerError(f"no recording {job_id}", 404)
        arch = dict(row.archive or {})
        if keep:
            arch.update(keep=True, keep_by=who, keep_at=s.clock().isoformat())
        else:
            for k in ("keep", "keep_by", "keep_at"):
                arch.pop(k, None)
        conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == job_id).values(archive=arch))
    log_event(LOG, "keep flag changed", job_id=job_id, keep=keep, by=who)
    return {"recording_id": job_id, "keep": keep}
