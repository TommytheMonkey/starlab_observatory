"""The unified schedule: transactional reservations of physical SDRs.

Every reservation, whatever its source, is a row in sched_jobs keyed by the
device serial. Booking runs under one process lock plus an fcntl file lock and a
single database transaction, so two concurrent requests can never both see a
receiver free and both book it.

Precedence (the owner's rules, 2026-10-13):
  * a running job (preparing/recording) is never interrupted: conflicting requests are rejected;
  * manual jobs never displace other manual jobs;
  * windows imported from a legacy recorder cannot be displaced (that recorder cannot be told to skip);
  * a broker-run automatic job may be preempted by a manual job if it has not started and is more
    than preempt_cutoff_seconds away. It is kept, marked `preempted` with the reason, and Slack is told.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import logging
import os
import pathlib
import secrets
import shutil
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from sqlalchemy import Engine, and_, or_, select

from ..config import Config
from ..db.schema import sched_job_events, sched_jobs, sched_notifications
from ..logjson import log_event
from ..timeutil import utc_now
from . import spec as specmod
from .hardware import Receiver, load_receivers

LOG = logging.getLogger("aobs.scheduler")

STATES = ("scheduled", "preparing", "recording", "recorded", "processing", "completed", "failed", "cancelled",
          "preempted", "skipped", "missed", "shadow", "partial")
HOLDING = ("scheduled", "preparing", "recording")   # statuses that hold the device
ACTIVE = ("preparing", "recording")
TERMINAL = ("completed", "failed", "cancelled", "preempted", "skipped", "missed", "partial")


class SchedulerError(RuntimeError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def new_job_id(prefix: str, when: dt.datetime) -> str:
    """Lower-case base32hex-safe (a-v, 0-9) so it can be a Google Calendar event id suffix."""
    return f"{prefix}{when:%Y%m%d%H%M}{secrets.token_hex(3)}"


def _iso(t: dt.datetime | None) -> str | None:
    return t.isoformat() if t else None


def row_dict(r: Any) -> dict[str, Any]:
    d = dict(r)
    for k in ("reservation_start", "reservation_end", "record_start", "record_end", "created_at", "updated_at"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    return d


@dataclass
class Placement:
    ok: bool
    errors: list[str]
    warnings: list[str]
    job: dict[str, Any]
    preempts: list[dict[str, Any]]


class Scheduler:
    def __init__(self, config: Config, engine: Engine, clock: Callable[[], dt.datetime] = utc_now,
                 disk_free: Callable[[str], int] | None = None, present: Callable[[Receiver], bool] | None = None,
                 satdump_pipelines: frozenset[str] | None = None):
        self.config = config
        self.engine = engine
        self.clock = clock
        self.settings = config.section("scheduler")
        self.receivers = load_receivers(config)
        self.switch_ports = {int(p["port"]): p for p in config.section("antenna_switch").get("ports", []) if p.get("antenna")}
        self.rf: Any = None                  # rflayout.RfLayout once the broker loads the signal-path layout
        self._disk_free = disk_free or (lambda path: shutil.disk_usage(_existing_parent(path)).free)
        self._present = present or device_present
        self.pipelines = satdump_pipelines if satdump_pipelines is not None else specmod.satdump_pipeline_ids(
            self.settings.get("satdump_pipelines_dir", "/usr/share/satdump/pipelines"))
        self._lock = threading.RLock()
        self._lock_path = config.data_root / "scheduler" / "reserve.lock"

    # ------------------------------------------------------------ locking
    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        with self._lock:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock_path.open("a") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def context(self) -> specmod.Context:
        return specmod.Context(now=self.clock(), receivers=self.receivers, switch_ports=self.switch_ports,
                               satdump_pipelines=self.pipelines, settings=self.settings)

    # ------------------------------------------------------------ booking
    def submit(self, jobs: list[dict[str, Any]], transaction_mode: str = "all_or_nothing", requested_by: str = "",
               dry_run: bool = False) -> dict[str, Any]:
        if transaction_mode not in ("all_or_nothing", "allow_partial"):
            raise SchedulerError("transaction_mode must be all_or_nothing or allow_partial", 400)
        if not isinstance(jobs, list) or not jobs:
            raise SchedulerError("jobs must be a non-empty list", 400)
        if len(jobs) > 20:
            raise SchedulerError("at most 20 jobs per request", 400)
        with self.locked(), self.engine.connect() as conn, conn.begin() as tx:
            now = self.clock()
            ctx = self.context()
            placements: list[Placement] = []
            accepted: list[dict[str, Any]] = []      # earlier jobs of this request, for intra-request conflicts
            for raw in jobs:
                checked = specmod.check_job(raw if isinstance(raw, dict) else {}, ctx)
                p = Placement(checked.ok, checked.errors, checked.warnings, checked.job, [])
                r = checked.job["resolved"]
                if r.get("receiver_id") and r.get("reservation_start") and r.get("reservation_end"):
                    self._place(conn, p, accepted, now)   # report conflicts alongside validation errors
                if p.ok:
                    accepted.append(p.job)
                placements.append(p)
            all_ok = all(p.ok for p in placements)
            commit = not dry_run and (all_ok or transaction_mode == "allow_partial")
            results = []
            for p in placements:
                entry: dict[str, Any] = {"job_name": p.job.get("job_name"), "ok": p.ok, "errors": p.errors,
                                         "warnings": p.warnings, "preempts": p.preempts, "job": p.job}
                if p.ok and commit:
                    entry["job_id"] = self._insert_manual(conn, p, requested_by, now)
                    entry["stored"] = True
                else:
                    entry["stored"] = False
                    if p.ok and not dry_run:
                        entry["errors"] = ["not stored: another job in this all_or_nothing request was rejected"]
                results.append(entry)
            if not commit:
                tx.rollback()
        for entry in results:
            if entry.get("stored"):
                log_event(LOG, "job scheduled", job_id=entry["job_id"], job_name=entry["job_name"],
                          receiver=entry["job"]["resolved"]["receiver_id"], preempts=[x["job_id"] for x in entry["preempts"]])
        return {"dry_run": dry_run, "transaction_mode": transaction_mode, "all_ok": all_ok,
                "stored": sum(1 for r in results if r.get("stored")), "results": results}

    def _place(self, conn: Any, p: Placement, accepted: list[dict[str, Any]], now: dt.datetime) -> None:
        r = p.job["resolved"]
        rx = self.receivers[r["receiver_id"]]
        rs, re_ = dt.datetime.fromisoformat(r["reservation_start"]), dt.datetime.fromisoformat(r["reservation_end"])
        if not self._present(rx):
            p.errors.append(f"{rx.id} (serial {rx.serial}) is not connected right now (on_missing_device=reject)")
        if rx.mode in ("legacy", "shadow"):
            days = int(self.settings.get("legacy_import_days", 7))
            if rs > now + dt.timedelta(days=days):
                p.errors.append(f"{rx.id} is still recorded by {rx.legacy_unit}; its passes are only known "
                                f"{days} days ahead, so jobs on it must start within {days} days until it is migrated")
        for other in accepted:
            o = other["resolved"]
            if o["device_serial"] == rx.serial and dt.datetime.fromisoformat(o["reservation_start"]) < re_ \
                    and rs < dt.datetime.fromisoformat(o["reservation_end"]):
                p.errors.append(f"conflicts with {other['job_name']} in this same request on {rx.id} "
                                f"({o['reservation_start']} to {o['reservation_end']})")
        cutoff = now + dt.timedelta(seconds=int(self.settings.get("preempt_cutoff_seconds", 180)))
        for row in self.conflicts(conn, rx.serial, rs, re_):
            what = (f"{row['job_name']} [{row['job_id']}, {row['source']}, {row['status']}] on {rx.id} "
                    f"{row['reservation_start'].isoformat()} to {row['reservation_end'].isoformat()}")
            if row["status"] in ACTIVE or row["reservation_start"] <= now:
                p.errors.append(f"receiver conflict with an active recording: {what}; it will not be interrupted")
            elif row["source"] == "manual":
                p.errors.append(f"receiver conflict with manually scheduled {what}")
            elif row["source"] == "legacy" or not row["executes"]:
                p.errors.append(f"receiver conflict with {what}, recorded by {rx.legacy_unit or 'a legacy recorder'} "
                                "which cannot yet be told to skip a pass (migrate it to the broker first)")
            elif row["reservation_start"] <= cutoff:
                p.errors.append(f"receiver conflict with {what}, which starts too soon to be preempted safely")
            elif (p.job.get("conflict_policy") or {}).get("on_automatic_job") == "reject":
                p.errors.append(f"receiver conflict with the automatic recording {what}; this job never displaces "
                                "automatic passes")
            else:
                p.preempts.append({"job_id": row["job_id"], "job_name": row["job_name"], "target": row["target"],
                                   "receiver_id": row["receiver_id"],
                                   "reservation_start": row["reservation_start"].isoformat(),
                                   "reservation_end": row["reservation_end"].isoformat()})
        self._check_storage(conn, p)
        p.ok = not p.errors

    def conflicts(self, conn: Any, serial: str, start: dt.datetime, end: dt.datetime,
                  exclude: str | None = None) -> list[Any]:
        q = select(sched_jobs).where(and_(sched_jobs.c.device_serial == serial, sched_jobs.c.status.in_(HOLDING),
                                          sched_jobs.c.reservation_start < end, sched_jobs.c.reservation_end > start))
        if exclude:
            q = q.where(sched_jobs.c.job_id != exclude)
        return list(conn.execute(q.order_by(sched_jobs.c.reservation_start)).mappings())

    def _check_storage(self, conn: Any, p: Placement) -> None:
        r = p.job["resolved"]
        est = r.get("estimated_bytes") or 0
        directory = str(p.job["recording"]["output_directory"]).split("{")[0]
        try:
            free = self._disk_free(directory)
        except OSError as exc:
            p.errors.append(f"cannot check free space at {directory}: {exc}")
            return
        pending = sum(int(x or 0) for x in conn.execute(select(sched_jobs.c.estimated_bytes).where(and_(
            sched_jobs.c.executes.is_(True), sched_jobs.c.status.in_(("scheduled", "preparing", "recording"))))).scalars())
        reserve = int(float(self.settings.get("reserve_free_gib", 64)) * 1024**3)
        need = est + reserve + int(r.get("minimum_free_bytes") or 0)
        r["storage"] = {"free_bytes": free, "already_committed_bytes": pending, "needed_bytes": need}
        if free - pending < need:
            msg = (f"not enough storage: {free / 1e9:.1f} GB free, {pending / 1e9:.1f} GB already committed to "
                   f"scheduled jobs, this job needs {est / 1e9:.2f} GB plus {need - est:.0f} bytes reserve")
            (p.errors if p.job["conflict_policy"].get("on_storage_warning", "reject") == "reject" else p.warnings).append(msg)

    def _insert_manual(self, conn: Any, p: Placement, requested_by: str, now: dt.datetime) -> str:
        r = p.job["resolved"]
        job_id = new_job_id("m", now)
        paths = specmod.output_paths(p.job, job_id)
        p.job["job_id"] = job_id
        r["paths"] = paths
        conn.execute(sched_jobs.insert().values(
            job_id=job_id, job_name=p.job["job_name"], source="manual", receiver_id=r["receiver_id"],
            device_serial=r["device_serial"], status="scheduled", status_reason="scheduled from Slack",
            executes=True, reservation_start=dt.datetime.fromisoformat(r["reservation_start"]),
            reservation_end=dt.datetime.fromisoformat(r["reservation_end"]),
            record_start=dt.datetime.fromisoformat(r["record_start"]), record_end=dt.datetime.fromisoformat(r["record_end"]),
            target=r["target_name"], estimated_bytes=r.get("estimated_bytes"), spec=p.job, paths=paths,
            requested_by=requested_by or (p.job.get("metadata") or {}).get("requested_by") or "",
            created_at=now, updated_at=now))
        self._event(conn, job_id, None, "scheduled", "scheduled from Slack", requested_by or "slack", now)
        self.notify(conn, "scheduled", job_id, f"Scheduled {p.job['job_name']} ({job_id}) on {r['receiver_id']}, "
                    f"{r['record_start'][:16]}Z to {r['record_end'][11:16]}Z.", {"receiver": r["receiver_id"]}, now)
        for x in p.preempts:
            covered = self._other_coverage(conn, x, r["device_serial"])
            reason = (f"preempted by manual job {p.job['job_name']} ({job_id}) on {x['receiver_id']}"
                      + (f"; the same pass is still scheduled on {covered}" if covered else "; no other receiver has this pass"))
            conn.execute(sched_jobs.update().where(and_(sched_jobs.c.job_id == x["job_id"],
                                                        sched_jobs.c.status == "scheduled"))
                         .values(status="preempted", status_reason=reason, preempted_by=job_id, updated_at=now))
            self._event(conn, x["job_id"], "scheduled", "preempted", reason, f"manual:{job_id}", now)
            self.notify(conn, "preempted", x["job_id"],
                        f"Automatic recording {x['job_name']} on {x['receiver_id']} ({x['reservation_start']}) was "
                        f"preempted by your job {p.job['job_name']}. {reason.split(';', 1)[-1].strip().capitalize()}.",
                        {"preempted_by": job_id, **x}, now)
        return job_id

    def _other_coverage(self, conn: Any, x: dict[str, Any], serial: str) -> str:
        rows = conn.execute(select(sched_jobs.c.receiver_id).where(and_(
            sched_jobs.c.target == x["target"], sched_jobs.c.device_serial != serial,
            sched_jobs.c.status.in_(HOLDING), sched_jobs.c.source.in_(("auto", "legacy")),
            sched_jobs.c.reservation_start < dt.datetime.fromisoformat(x["reservation_end"]),
            sched_jobs.c.reservation_end > dt.datetime.fromisoformat(x["reservation_start"])))).scalars().all()
        return ", ".join(sorted(set(rows)))

    # ------------------------------------------------------------ state
    def _event(self, conn: Any, job_id: str, old: str | None, new: str, reason: str, actor: str, now: dt.datetime) -> None:
        conn.execute(sched_job_events.insert().values(job_id=job_id, at=now, old_status=old, new_status=new,
                                                      reason=reason, actor=actor))

    def notify(self, conn: Any, kind: str, job_id: str | None, text: str, data: dict[str, Any] | None = None,
               now: dt.datetime | None = None) -> None:
        if kind in set(self.settings.get("notifications_disabled", [])):
            return
        if job_id and kind != "health":       # quiet jobs (signal-path tests) report in the web UI, not Slack
            spec = conn.execute(select(sched_jobs.c.spec).where(sched_jobs.c.job_id == job_id)).scalar()
            if ((spec or {}).get("metadata") or {}).get("quiet"):
                return
        conn.execute(sched_notifications.insert().values(at=now or self.clock(), kind=kind, job_id=job_id, text=text,
                                                         data=data or {}))

    def transition(self, job_id: str, new: str, reason: str = "", actor: str = "runner", expect: tuple[str, ...] = (),
                   notify: str | None = None, **values: Any) -> bool:
        if new not in STATES:
            raise ValueError(new)
        with self.locked(), self.engine.begin() as conn:
            row = conn.execute(select(sched_jobs.c.status).where(sched_jobs.c.job_id == job_id)).first()
            if row is None or (expect and row.status not in expect):
                return False
            now = self.clock()
            conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == job_id)
                         .values(status=new, status_reason=reason or None, updated_at=now, **values))
            if row.status != new:
                self._event(conn, job_id, row.status, new, reason, actor, now)
            if notify:
                self.notify(conn, new, job_id, notify, {"status": new}, now)
        return True

    def update(self, job_id: str, **values: Any) -> None:
        with self.engine.begin() as conn:
            conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == job_id).values(updated_at=self.clock(), **values))

    def cancel(self, job_id: str, reason: str, actor: str) -> dict[str, Any]:
        with self.locked(), self.engine.begin() as conn:
            row = conn.execute(select(sched_jobs).where(sched_jobs.c.job_id == job_id)).mappings().first()
            if row is None:
                raise SchedulerError(f"no job {job_id}", 404)
            if row["status"] in ACTIVE:
                raise SchedulerError(f"{job_id} is {row['status']}; an active recording is never interrupted")
            if row["status"] != "scheduled" or not row["executes"]:
                raise SchedulerError(f"{job_id} is {row['status']} ({row['source']}); only scheduled broker jobs can be cancelled")
            now = self.clock()
            conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == job_id)
                         .values(status="cancelled", status_reason=reason, updated_at=now))
            self._event(conn, job_id, "scheduled", "cancelled", reason, actor, now)
        return {"job_id": job_id, "status": "cancelled"}

    # ------------------------------------------------------------ queries
    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(sched_jobs).where(sched_jobs.c.job_id == job_id)).mappings().first()
            if row is None:
                return None
            events = conn.execute(select(sched_job_events).where(sched_job_events.c.job_id == job_id)
                                  .order_by(sched_job_events.c.id)).mappings().all()
        d = row_dict(row)
        d["events"] = [{**dict(e), "at": e["at"].isoformat()} for e in events]
        return d

    def jobs(self, receiver: str | None = None, status: list[str] | None = None, source: str | None = None,
             start: dt.datetime | None = None, end: dt.datetime | None = None, target: str | None = None,
             limit: int = 200, executes: bool | None = None) -> list[dict[str, Any]]:
        q = select(sched_jobs)
        if executes is not None:
            q = q.where(sched_jobs.c.executes.is_(executes))
        if receiver:
            rx = self.receivers.get(receiver)
            q = q.where(or_(sched_jobs.c.receiver_id == receiver, sched_jobs.c.device_serial == (rx.serial if rx else receiver)))
        if status:
            q = q.where(sched_jobs.c.status.in_(status))
        if source:
            q = q.where(sched_jobs.c.source == source)
        if start:
            q = q.where(sched_jobs.c.reservation_end > start)
        if end:
            q = q.where(sched_jobs.c.reservation_start < end)
        if target:
            q = q.where(sched_jobs.c.target.ilike(f"%{target}%") | sched_jobs.c.job_name.ilike(f"%{target}%"))
        with self.engine.connect() as conn:
            rows = conn.execute(q.order_by(sched_jobs.c.reservation_start).limit(limit)).mappings().all()
        return [row_dict(r) for r in rows]

    def next_reservation(self, receiver_id: str, after: dt.datetime) -> dt.datetime | None:
        """Start of the next holding reservation on this physical device (for capping ad-hoc leases)."""
        rx = self.receivers.get(receiver_id)
        if rx is None:
            return None
        with self.engine.connect() as conn:
            return conn.execute(select(sched_jobs.c.reservation_start).where(and_(
                sched_jobs.c.device_serial == rx.serial, sched_jobs.c.status.in_(HOLDING),
                sched_jobs.c.reservation_end > after)).order_by(sched_jobs.c.reservation_start).limit(1)).scalar()

    def notifications(self, after: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(sched_notifications).where(sched_notifications.c.id > after)
                                .order_by(sched_notifications.c.id).limit(limit)).mappings().all()
        return [{**dict(r), "at": r["at"].isoformat()} for r in rows]

    def capabilities(self) -> dict[str, Any]:
        now = self.clock()
        out = []
        for rx in self.receivers.values():
            d = rx.capabilities(self.switch_ports)
            d["connected"] = self._present(rx)
            upcoming = self.jobs(receiver=rx.id, status=list(HOLDING), start=now, end=now + dt.timedelta(hours=24))
            busy_now = [j for j in upcoming if j["reservation_start"] <= now.isoformat()]
            d["available_now"] = d["connected"] and not busy_now
            d["reservations_next_24h"] = [{k: j[k] for k in ("job_id", "job_name", "source", "status", "target",
                                                               "reservation_start", "reservation_end")} for j in upcoming]
            out.append(d)
        return {"at": now.isoformat(), "receivers": out, "satdump_pipelines": sorted(self.pipelines),
                "rules": {"min_lead_seconds": self.settings.get("min_lead_seconds", 120),
                          "max_duration_seconds": self.settings.get("max_duration_seconds", 14400),
                          "preempt_cutoff_seconds": self.settings.get("preempt_cutoff_seconds", 180),
                          "allowed_output_roots": self.settings.get("allowed_output_roots", ["/data"]),
                          "recording_window": "the SDR records schedule.start to schedule.end; pre/post-roll only extend the reservation",
                          "transaction_modes": ["all_or_nothing", "allow_partial"],
                          "receive_only": True}}


def device_present(rx: Receiver, sysfs: pathlib.Path = pathlib.Path("/sys/bus/usb/devices")) -> bool:
    for entry in sysfs.glob("*"):
        try:
            if (entry / "serial").read_text().strip() == rx.serial:
                return True
        except OSError:
            continue
    return False


def _existing_parent(path: str) -> str:
    p = pathlib.Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return str(p)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)
