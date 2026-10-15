"""Automatic METEOR jobs and legacy-window import, using the old recorders' own predictor.

Per receiver mode:
  legacy  -> windows the old service will record are imported as locked `legacy` rows
  shadow  -> legacy rows as above, plus broker-planned `auto` rows with status `shadow` (never executed)
  broker  -> broker-planned `auto` rows that the runner executes; a manual job holding the
             device turns the pass into `skipped` (recorded with the reason, Slack is told)

Predictions jitter by a second or two between TLE refreshes, so a planned pass is matched to
an existing row by receiver + satellite + overlapping window, not by an exact key.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
from typing import Any, Callable

from sqlalchemy import and_, select

from ..db.schema import sched_jobs
from ..legacy import load_legacy, predict, refresh_tle
from ..logjson import log_event
from .hardware import Receiver, auto_radio, validate_radio
from .store import HOLDING, Scheduler, new_job_id

LOG = logging.getLogger("aobs.planner")
TLE_SEEDS = (pathlib.Path("/data/observatory-recordings/pass-cache/OBS-R01/observatory.tle"),
             pathlib.Path("/data/sdrplay/meteor/pass-cache/observatory.tle"))

Window = dict[str, Any]   # start, end, satellites [{name, norad_id, rise, set, max_el}]


def merge(predicted: list[Any]) -> list[Window]:
    """Same merge rule as pass_schedule.merge_windows / meteor-auto.py."""
    out: list[Window] = []
    for w in sorted(predicted, key=lambda x: x.record_start):
        sat = {"name": w.job.name, "norad_id": w.job.norad_id, "rise": w.rise.isoformat(), "set": w.set.isoformat(),
               "max_elevation_deg": round(w.max_elevation_degrees or 0, 1)}
        if out and w.record_start <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], w.record_end)
            out[-1]["satellites"].append(sat)
        else:
            out.append({"start": w.record_start, "end": w.record_end, "satellites": [sat]})
    return out


class Planner:
    def __init__(self, scheduler: Scheduler, predictor: Callable[[Receiver, float], list[Window]] | None = None):
        self.s = scheduler
        self.predictor = predictor or self._predict

    # ------------------------------------------------------------ prediction
    def _predict(self, rx: Receiver, hours: float) -> list[Window]:
        legacy = load_legacy(self.s.config)
        wanted = {int(n) for n in rx.auto["norad_ids"]}
        jobs = [legacy.scheduler.Job(
            id=str(raw["id"]), name=str(raw["name"]), kind=str(raw["kind"]), norad_id=int(raw["norad_id"]),
            receiver=rx.id, capability=str(raw["capability"]), enabled=True,
            min_elevation_degrees=float(raw.get("min_elevation_degrees", 20)), raw=dict(raw))
            for raw in legacy.station.get("jobs", [])
            if raw.get("receiver") == rx.auto["station_receiver"] and int(raw["norad_id"]) in wanted]
        if {j.norad_id for j in jobs} != wanted:
            raise RuntimeError(f"{rx.id}: station jobs for {rx.auto['station_receiver']} lack some of {sorted(wanted)}")
        tle = refresh_tle(legacy, self.s.config.data_root / "scheduler" / "tle", jobs, list(TLE_SEEDS))
        return merge(predict(legacy, tle, jobs, hours))

    # ------------------------------------------------------------ cycle
    def plan(self) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for rx in self.s.receivers.values():
            if not rx.auto.get("enabled") or not rx.schedulable:     # no automatic jobs without hardware evidence
                continue
            try:
                summary[rx.id] = self._plan_receiver(rx)
            except Exception as exc:  # noqa: BLE001 - one receiver's prediction failure must not stop the rest
                LOG.exception("planning %s failed", rx.id)
                summary[rx.id] = {"error": str(exc)}
        self.reconcile_legacy()
        self.prune_auto()
        return summary

    def prune_auto(self) -> int:
        """Delete raw IQ of finished automatic jobs older than auto_retention_hours (sidecar and images stay).

        Manual recordings are never pruned."""
        hours = float(self.s.settings.get("auto_retention_hours", 48))
        cutoff = self.s.clock() - dt.timedelta(hours=hours)
        pruned = 0
        for job in self.s.jobs(source="auto", status=["completed", "failed"], end=cutoff, limit=1000):
            if job.get("rule_id"):
                continue                      # rule recordings follow their rule's retention (rules.RulePlanner.prune)
            raw = pathlib.Path((job.get("paths") or {}).get("raw") or "")
            for p in (raw, raw.with_name(raw.name + ".part")):
                if p.name and p.is_file() and dt.datetime.fromisoformat(job["record_end"]) < cutoff:
                    p.unlink()
                    pruned += 1
                    log_event(LOG, "pruned automatic raw IQ", job_id=job["job_id"], path=str(p))
        return pruned

    def _plan_receiver(self, rx: Receiver) -> dict[str, int]:
        st = self.s.settings
        now = self.s.clock()
        counts = {"inserted": 0, "updated": 0, "skipped": 0, "retired": 0}
        legacy_mode = rx.mode in ("legacy", "shadow")
        hours = float(st.get("legacy_import_days", 7)) * 24 if legacy_mode else float(st.get("auto_horizon_hours", 48))
        windows = [w for w in self.predictor(rx, hours) if w["end"] > now]
        with self.s.locked(), self.s.engine.begin() as conn:
            self._retire_other_modes(conn, rx, now, counts)
            if legacy_mode:
                for w in windows:
                    guard = dt.timedelta(seconds=float(st.get("legacy_guard_seconds", 60))
                                         + 30 * max(0.0, (w["start"] - now).total_seconds() / 86400))
                    self._upsert(conn, rx, w, "legacy", False, "scheduled", now, counts, guard)
            if rx.mode in ("shadow", "broker"):
                for w in windows:
                    self._upsert(conn, rx, w, "auto", rx.mode == "broker",
                                 "shadow" if rx.mode == "shadow" else "scheduled", now, counts, dt.timedelta(0))
            self._retire_vanished(conn, rx, windows, now, counts)
        if any(counts.values()):
            log_event(LOG, "planned", receiver=rx.id, mode=rx.mode, **counts)
        return counts

    def _retire_other_modes(self, conn: Any, rx: Receiver, now: dt.datetime, counts: dict[str, int]) -> None:
        """After a mode change, future rows that belong to the previous mode stop holding the device."""
        soon = now + dt.timedelta(minutes=10)
        retire = []
        if rx.mode == "broker":
            retire.append((and_(sched_jobs.c.source == "legacy"), f"{rx.id} migrated to the broker"))
            retire.append((and_(sched_jobs.c.source == "auto", sched_jobs.c.status == "shadow"), "shadow plan superseded"))
        else:
            retire.append((and_(sched_jobs.c.source == "auto", sched_jobs.c.executes.is_(True)),
                           f"{rx.id} is recorded by {rx.legacy_unit} again"))
            if rx.mode == "legacy":
                retire.append((and_(sched_jobs.c.source == "auto", sched_jobs.c.status == "shadow"), "shadow mode ended"))
        for cond, reason in retire:
            rows = conn.execute(select(sched_jobs.c.job_id, sched_jobs.c.status).where(and_(
                cond, sched_jobs.c.receiver_id == rx.id, sched_jobs.c.status.in_(("scheduled", "shadow")),
                sched_jobs.c.reservation_start > soon))).all()
            for r in rows:
                conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == r.job_id)
                             .values(status="cancelled", status_reason=reason, updated_at=now))
                self.s._event(conn, r.job_id, r.status, "cancelled", reason, "planner", now)
                counts["retired"] += 1

    def _upsert(self, conn: Any, rx: Receiver, w: Window, source: str, executes: bool, status: str,
                now: dt.datetime, counts: dict[str, int], guard: dt.timedelta) -> None:
        target = " + ".join(s["name"] for s in w["satellites"])
        rs, re_ = w["start"] - guard, w["end"] + guard
        existing = conn.execute(select(sched_jobs).where(and_(
            sched_jobs.c.source == source, sched_jobs.c.receiver_id == rx.id, sched_jobs.c.target == target,
            sched_jobs.c.status.in_(("scheduled", "shadow", "skipped", "preempted", "preparing", "recording")),
            sched_jobs.c.record_start < w["end"], sched_jobs.c.record_end > w["start"]))).mappings().first()
        spec = self._spec(rx, w, source)
        if existing is not None:
            cutoff = now + dt.timedelta(seconds=int(self.s.settings.get("preempt_cutoff_seconds", 180)))
            if executes and existing["status"] in ("skipped", "preempted") and rs > cutoff \
                    and not self._blocked(conn, rx, rs, re_, existing["job_id"]):
                reason = "restored: the receiver is free again"
                conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == existing["job_id"]).values(
                    status="scheduled", status_reason=reason, preempted_by=None, reservation_start=rs,
                    reservation_end=re_, record_start=w["start"], record_end=w["end"], updated_at=now))
                self.s._event(conn, existing["job_id"], existing["status"], "scheduled", reason, "planner", now)
                counts["updated"] += 1
                return
            if existing["status"] in ("scheduled", "shadow") and existing["spec"].get("radio") != spec.get("radio"):
                conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == existing["job_id"]).values(spec=spec, updated_at=now))
            if existing["status"] in ("scheduled", "shadow") and existing["reservation_start"] > now + dt.timedelta(minutes=5) \
                    and (abs((existing["record_start"] - w["start"]).total_seconds()) >= 2
                         or abs((existing["record_end"] - w["end"]).total_seconds()) >= 2):
                if executes and self._blocked(conn, rx, rs, re_, existing["job_id"]):
                    return   # keep the old times rather than move into someone else's reservation
                paths = self._paths(rx, w, existing["job_id"]) if source == "auto" else None
                conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == existing["job_id"]).values(
                    reservation_start=rs, reservation_end=re_, record_start=w["start"], record_end=w["end"],
                    spec=spec, paths=paths, estimated_bytes=spec.get("estimated_bytes"), updated_at=now))
                counts["updated"] += 1
            return
        job_id = new_job_id("a" if source == "auto" else "l", w["start"])
        reason = {"legacy": f"recorded by {rx.legacy_unit}", "auto": "planned automatic pass"}[source]
        if status == "shadow":
            reason = "shadow plan (not executed; compared with the legacy recorder)"
        if executes:
            blocker = self._blocked(conn, rx, rs, re_, None)
            if blocker:
                status, reason = "skipped", (f"{rx.id} is reserved by {blocker['source']} job {blocker['job_name']} "
                                             f"({blocker['job_id']})")
                counts["skipped"] += 1
                if blocker["source"] in ("manual", "session"):
                    self.s.notify(conn, "skipped", job_id, f"Automatic recording {target} on {rx.id} at "
                                  f"{w['start']:%Y-%m-%d %H:%M} UTC is skipped: {reason}.", {"blocked_by": blocker["job_id"]}, now)
        conn.execute(sched_jobs.insert().values(
            job_id=job_id, job_name=f"{source}-{target.replace(' ', '').lower()}-{rx.id.lower()}", source=source,
            receiver_id=rx.id, device_serial=rx.serial, status=status, status_reason=reason, executes=executes,
            reservation_start=rs, reservation_end=re_, record_start=w["start"], record_end=w["end"],
            pass_key=f"{'+'.join(str(s['norad_id']) for s in w['satellites'])}:{w['start']:%Y%m%dT%H%M%S}",
            target=target, estimated_bytes=spec.get("estimated_bytes"), spec=spec,
            paths=self._paths(rx, w, job_id) if source == "auto" else None, created_at=now, updated_at=now))
        self.s._event(conn, job_id, None, status, reason, "planner", now)
        counts["inserted"] += 1

    def _blocked(self, conn: Any, rx: Receiver, rs: dt.datetime, re_: dt.datetime, exclude: str | None) -> Any:
        rows = [r for r in self.s.conflicts(conn, rx.serial, rs, re_, exclude=exclude) if r["source"] != "auto"
                or r["status"] in ("preparing", "recording")]
        if rows:
            return rows[0]
        for sess in self.s.session_conflicts(rx.serial, rs, re_):      # a session that will not yield blocks it
            if not (sess.get("policy") or {}).get("end_before_passes"):
                return {"source": "session", "job_name": f"SDR++ session by {sess['created_by']}",
                        "job_id": sess["session_id"]}
        return None

    def _retire_vanished(self, conn: Any, rx: Receiver, windows: list[Window], now: dt.datetime,
                         counts: dict[str, int]) -> None:
        """A planned pass that the predictor no longer produces (e.g. after a TLE update) is skipped, not deleted."""
        if not windows:
            return
        horizon = max(w["end"] for w in windows)
        rows = conn.execute(select(sched_jobs).where(and_(
            sched_jobs.c.receiver_id == rx.id, sched_jobs.c.source.in_(("auto", "legacy")),
            sched_jobs.c.status.in_(("scheduled", "shadow")), sched_jobs.c.record_start > now + dt.timedelta(minutes=10),
            sched_jobs.c.record_end < horizon))).mappings().all()
        for r in rows:
            if not any(r["record_start"] < w["end"] and w["start"] < r["record_end"] for w in windows):
                conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == r["job_id"])
                             .values(status="skipped", status_reason="pass no longer predicted", updated_at=now))
                self.s._event(conn, r["job_id"], r["status"], "skipped", "pass no longer predicted", "planner", now)
                counts["retired"] += 1

    # ------------------------------------------------------------ job content
    def _spec(self, rx: Receiver, w: Window, source: str) -> dict[str, Any]:
        a = rx.auto
        tgt = a.get("target_frequency_hz")
        radio = auto_radio(rx)
        errors: list[str] = []
        validate_radio(rx, radio, rx.engines()[0], a["format"], errors, [])
        if errors:
            raise RuntimeError(f"{rx.id} auto settings are invalid: {'; '.join(errors)}")
        seconds = (w["end"] - w["start"]).total_seconds()
        from .hardware import BYTES_PER_SAMPLE
        pipeline = a.get("satdump_pipeline") or ""
        return {
            "job_name": f"auto {' + '.join(s['name'] for s in w['satellites'])} on {rx.id}",
            "target": {"type": "satellite", "name": " + ".join(s["name"] for s in w["satellites"]),
                       "norad_ids": [s["norad_id"] for s in w["satellites"]], "passes": w["satellites"]},
            "receiver": {"receiver_id": rx.id, "serial": rx.serial, "driver": rx.driver, "antenna": rx.antenna,
                         "signal_path": rx.signal_path, "exclusive_lock": True},
            "radio": radio,
            "recording": {"enabled": True, "engine": rx.engines()[0], "format": a["format"]},
            "satdump": {"enabled": bool(pipeline), "operation": "decode_after_recording" if pipeline else "none",
                        "pipeline": pipeline or None, "input_type": "baseband", "baseband_format": a["format"],
                        "sample_rate_hz": int(a["sample_rate_hz"]), "frequency_hz": int(tgt) if tgt else None},
            "postprocess": {"generate_waterfall": True, "generate_spectrum": True, "checksum": True,
                            "record_metadata": True, "retain_raw_iq": True, "decode": bool(pipeline)},
            "estimated_bytes": int(a["sample_rate_hz"] * BYTES_PER_SAMPLE[a["format"]] * seconds),
            "resolved": {"receiver_id": rx.id, "device_serial": rx.serial, "driver": rx.driver, "switch_port": None,
                         "record_start": w["start"].isoformat(), "record_end": w["end"].isoformat(),
                         "satdump_freq_shift_hz": None,
                         "target_name": " + ".join(s["name"] for s in w["satellites"]), "source": source},
        }

    def _paths(self, rx: Receiver, w: Window, job_id: str) -> dict[str, str]:
        root = pathlib.PurePosixPath(self.s.settings.get("auto_output_root", "/data/observatory/auto")) / rx.id
        target = "+".join(s["name"].replace(" ", "") for s in w["satellites"])
        raw = str(root / f"{target}_{rx.id}_{w['start']:%Y%m%dT%H%M%SZ}_{rx.auto['center_frequency_hz']}_"
                         f"{rx.auto['sample_rate_hz']}.{rx.auto['format']}")
        out = {"directory": str(root), "raw": raw, "sidecar": raw + ".json", "log": raw + ".log"}
        if rx.auto.get("satdump_pipeline"):
            out["decode"] = str(root / "decoded" / job_id)
        return out

    # ------------------------------------------------------------ legacy outcomes
    def reconcile_legacy(self) -> int:
        """Attach the old recorders' own metadata to finished legacy windows (recorded / missed)."""
        now = self.s.clock()
        grace = dt.timedelta(minutes=3)
        with self.s.engine.connect() as conn:
            rows = conn.execute(select(sched_jobs).where(and_(
                sched_jobs.c.source == "legacy", sched_jobs.c.status == "scheduled",
                sched_jobs.c.record_end < now - grace))).mappings().all()
        done = 0
        for r in rows:
            rx = self.s.receivers.get(r["receiver_id"])
            if rx is None:
                continue
            files = find_legacy_outputs(rx, r["reservation_start"], r["reservation_end"])
            if files:
                ok = [f for f in files if f["status"] in ("recorded", "completed")]
                status = "recorded" if ok else "failed"
                reason = f"{len(ok)}/{len(files)} segment(s) recorded by {rx.legacy_unit}"
            else:
                status, reason = "missed", f"no recording from {rx.legacy_unit} found in {rx.legacy_output}"
            self.s.transition(r["job_id"], status, reason, actor="reconciler", expect=("scheduled",),
                              result={"legacy_files": files})
            done += 1
        return done


def find_legacy_outputs(rx: Receiver, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
    out = []
    base = pathlib.Path(rx.legacy_output)
    if not base.is_dir():
        return out
    pattern = "OBS-R0*_*.json" if rx.legacy_kind == "record_py" else "auto_*.json"
    lo, hi = start - dt.timedelta(minutes=5), end
    for meta in base.glob(pattern):
        try:
            d = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        began = d.get("started_at") or d.get("start_utc")
        if not began:
            continue
        t = dt.datetime.fromisoformat(began.replace("Z", "+00:00"))
        if not lo <= t <= hi:
            continue
        raw = base / (d.get("raw_file") or meta.with_suffix(".cs16").name)
        out.append({"metadata": str(meta), "raw": str(raw), "raw_exists": raw.exists(),
                    "status": d.get("status"), "bytes": d.get("raw_bytes", d.get("bytes")),
                    "started_at": began, "finished_at": d.get("finished_at") or d.get("end_utc")})
    return sorted(out, key=lambda f: f["started_at"])
