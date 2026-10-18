"""Wires the scheduler into the broker process: planner + runner + calendar loops, recordings lookup."""

from __future__ import annotations

import datetime as dt
import logging
import pathlib
import threading
from typing import Any

from ..timeutil import fmt_clock
from .planner import Planner
from .runner import Runner
from .store import Scheduler, SchedulerError

LOG = logging.getLogger("aobs.scheduler.service")
RESULT_STATES = ["recording", "recorded", "processing", "completed", "failed", "missed"]
MAX_DOWNLOAD = 100 * 1024**2


def artifacts(job: dict[str, Any]) -> dict[str, Any]:
    """Every file a job produced, with existence and size; raw IQ is listed but never sent over Slack."""
    def info(p: str | None) -> dict[str, Any] | None:
        if not p:
            return None
        path = pathlib.Path(p)
        return {"path": p, "exists": path.exists(), "bytes": path.stat().st_size if path.exists() else None}

    paths = job.get("paths") or {}
    res = job.get("result") or {}
    pp = res.get("postprocess") or {}
    out: dict[str, Any] = {"raw_iq": info(paths.get("raw")), "metadata": info(paths.get("sidecar")),
                           "capture_log": info(paths.get("log"))}
    plots = pp.get("plots") or {}
    out["waterfall"] = info(plots.get("waterfall"))
    out["spectrum"] = info(plots.get("spectrum"))
    sd = pp.get("satdump") or {}
    if sd:
        base = pathlib.Path(sd["output_directory"])
        out["satdump"] = {"output_directory": sd["output_directory"], "decoded": sd.get("decoded"),
                          "images": [str(base / p) for p in sd.get("products", [])
                                     if pathlib.Path(p).suffix.lower() in (".png", ".jpg", ".jpeg")][:40],
                          "log": info(sd.get("log")), "return_code": sd.get("return_code")}
    if res.get("legacy_files"):
        out["legacy_files"] = res["legacy_files"]
    return {k: v for k, v in out.items() if v}


def recordings(s: Scheduler, receiver: str | None = None, satellite: str | None = None, date: str | None = None,
               job_id: str | None = None, limit: int = 50, processor: Any = None) -> list[dict[str, Any]]:
    if job_id:
        job = s.get(job_id)
        jobs = [job] if job else []
    else:
        start = end = None
        if date:
            day = dt.date.fromisoformat(date)
            tz = dt.timezone.utc
            start = dt.datetime.combine(day, dt.time(), tz)
            end = start + dt.timedelta(days=1)
        jobs = s.jobs(receiver=receiver, status=RESULT_STATES, start=start, end=end, target=satellite, limit=500)
        jobs = sorted(jobs, key=lambda j: j["record_start"], reverse=True)[:limit]
    out = []
    for j in jobs:
        cap = (j.get("result") or {}).get("capture") or {}
        entry = {"recording_id": j["job_id"], "job_id": j["job_id"], "job_name": j["job_name"], "source": j["source"],
                 "status": j["status"], "status_reason": j.get("status_reason"), "receiver": j["receiver_id"],
                 "target": j.get("target"), "record_start": j["record_start"], "record_end": j["record_end"],
                 "bytes": cap.get("bytes"), "files": artifacts(j),
                 "simulated": bool(((j.get("spec") or {}).get("metadata") or {}).get("simulated"))}
        if processor is not None:
            arts = processor.artifacts(j["job_id"])
            entry["artifact_counts"] = {k: sum(1 for a in arts if a["kind"] == k) for k in sorted({a["kind"] for a in arts})}
            entry["attempts"] = [{k: a[k] for k in ("attempt_id", "kind", "status", "origin", "queued_at")}
                                 for a in processor.attempts(j["job_id"])]
        out.append(entry)
    return out


def allowed_file(s: Scheduler, job_id: str, path: str) -> pathlib.Path:
    """A file may be fetched only if it belongs to that job's artifacts (no arbitrary paths)."""
    job = s.get(job_id)
    if job is None:
        raise SchedulerError(f"no job {job_id}", 404)
    a = artifacts(job)
    allowed = {v["path"] for k, v in a.items() if isinstance(v, dict) and "path" in v and k != "raw_iq"}
    sd = a.get("satdump") or {}
    allowed |= set(sd.get("images", []))
    if sd.get("log"):
        allowed.add(sd["log"]["path"])
    p = pathlib.Path(path)
    if str(p) not in allowed or not p.is_file():
        raise SchedulerError("that file is not a downloadable artifact of this job", 404)
    if p.stat().st_size > MAX_DOWNLOAD:
        raise SchedulerError(f"{p.name} is larger than {MAX_DOWNLOAD // 1024**2} MB; fetch it on the observatory", 413)
    return p


def shadow_report(s: Scheduler, min_compared: int = 3, tolerance_s: float = 15.0) -> dict[str, Any]:
    """Compare broker-planned (shadow) windows with what the legacy recorder actually recorded.

    Both plans come from the same predictor, so comparing predictions would prove nothing. The
    check that matters is whether the broker would have started and stopped when the old service
    really did: shadow record_start/end vs the first/last legacy segment's own metadata timestamps.
    """
    now = s.clock()
    report: dict[str, Any] = {}
    for rx in s.receivers.values():
        if rx.mode != "shadow":
            continue
        since = now - dt.timedelta(days=3)
        shadow = s.jobs(receiver=rx.id, source="auto", status=["shadow"], start=since, limit=1000)
        legacy = s.jobs(receiver=rx.id, source="legacy", start=since, limit=1000)
        pairs, pending, unmatched, issues = [], 0, [], []
        for sj in shadow:
            a, b = dt.datetime.fromisoformat(sj["record_start"]), dt.datetime.fromisoformat(sj["record_end"])
            lj = next((x for x in legacy if x["target"] == sj["target"]
                       and dt.datetime.fromisoformat(x["record_start"]) < b and a < dt.datetime.fromisoformat(x["record_end"])), None)
            if lj is None:
                unmatched.append(sj["job_id"])
                continue
            files = (lj.get("result") or {}).get("legacy_files") or []
            if lj["status"] == "scheduled":
                pending += 1
                continue
            if not files:
                issues.append(f"{sj['target']} {fmt_clock(sj['record_start'], day=True)}: legacy recorder produced nothing ({lj['status']})")
                continue
            first = dt.datetime.fromisoformat(files[0]["started_at"].replace("Z", "+00:00"))
            last = files[-1].get("finished_at")
            last_t = dt.datetime.fromisoformat(last.replace("Z", "+00:00")) if last else None
            pairs.append({"target": sj["target"], "start": sj["record_start"], "legacy_status": lj["status"],
                          "start_diff_s": round((first - a).total_seconds(), 1),
                          "end_diff_s": round((last_t - b).total_seconds(), 1) if last_t else None})
        worst = max((abs(p["start_diff_s"]) for p in pairs), default=None)
        report[rx.id] = {"compared": len(pairs), "pending": pending, "unmatched_shadow": unmatched, "issues": issues,
                         "worst_start_diff_s": worst,
                         "agrees": len(pairs) >= min_compared and not unmatched and worst is not None and worst <= tolerance_s,
                         "pairs": pairs[-20:]}
    return report


class SchedulerService:
    def __init__(self, scheduler: Scheduler, planner: Planner, runner: Runner, calendar_client: Any = None,
                 calendar_id: str | None = None, processor: Any = None, rule_planner: Any = None):
        self.s, self.planner, self.runner, self.processor = scheduler, planner, runner, processor
        from .rules import RulePlanner
        self.rules = rule_planner or RulePlanner(scheduler)
        self._plan_lock = threading.Lock()
        from .catalog import Catalog
        from .status import StatusService
        self.catalog = Catalog(scheduler, processor)
        self.status = StatusService(scheduler, None, processor, self)
        from ..slack.service import SlackService
        self.slack = SlackService(scheduler, self.status, self.catalog)     # does nothing until enabled
        self.calendar_client, self.calendar_id = calendar_client, calendar_id
        self.calendar_error: str | None = None
        self._cal_lock = threading.Lock()
        if getattr(scheduler, "config_store", None) is not None:
            scheduler.config_store.on_change.append(self._integrations_changed)
        self._stop = threading.Event()
        self.last_plan: dict[str, Any] = {}
        self.last_calendar: dict[str, Any] = {}

    def start(self) -> None:
        if getattr(self.s, "sessions", None) is not None:
            self.s.sessions.recover()                 # no receiver stays locked by a session that is gone
            self.s.sessions.start_watcher()
        self.runner.start()
        if self.processor is not None:
            self.processor.start()
        self.watch_health(float(self.s.settings.get("health_watch_seconds", 900)))

        try:
            self.s.rules.import_from_toml()          # once: the TOML's automatic settings, shown as disabled rules
        except Exception:  # noqa: BLE001
            LOG.exception("importing automatic settings as rules failed")
        try:
            self.s.profiles.seed()                   # once: the built-in decoders as editable profiles (brief 8)
        except Exception:  # noqa: BLE001
            LOG.exception("creating the built-in decoder profiles failed")
        try:
            self.slack.start()                       # optional: only when Settings → Slack enables it (brief 9)
        except Exception:  # noqa: BLE001 - Slack must never stop the observatory
            LOG.exception("starting the Slack integration failed")

        def plan_loop() -> None:
            interval = float(self.s.settings.get("plan_interval_seconds", 300))
            while True:
                try:
                    self.last_plan = {"at": self.s.clock().isoformat(), **self.planner.plan()}
                except Exception:  # noqa: BLE001
                    LOG.exception("planning failed")
                self.reconcile_rules()
                if self.calendar_client and self.calendar_id:
                    self.sync_calendar()
                if self._stop.wait(interval):
                    return
        threading.Thread(target=plan_loop, name="planner", daemon=True).start()

        def cal_loop() -> None:                       # runs always: the calendar can be turned on from the web page
            while not self._stop.wait(60):
                if self.calendar_client and self.calendar_id:
                    self.sync_calendar()
        threading.Thread(target=cal_loop, name="calendar", daemon=True).start()

    def reconcile_rules(self, refresh: bool = True) -> dict[str, Any]:
        """One rule-planning cycle; also run right after a rule, a job override or the elements change."""
        with self._plan_lock:
            try:
                return self.rules.reconcile(refresh=refresh)
            except Exception as exc:  # noqa: BLE001 - planning failures must not stop the broker
                LOG.exception("rule planning failed")
                self.rules.last = {"at": self.s.clock().isoformat(), "error": str(exc)}
                return self.rules.last

    def keeps_lease(self, lease: Any) -> bool:
        """At broker start: keep a scheduler lease whose job is still preparing/recording."""
        if not lease.owner.startswith("scheduler:"):
            return False
        job = self.s.get(lease.owner.split(":", 1)[1])
        return job is not None and job["status"] in ("preparing", "recording")

    def _integrations_changed(self, doc: str, data: dict[str, Any]) -> None:
        if doc == "integrations":
            self.reload_calendar()

    def reload_calendar(self) -> dict[str, Any]:
        """Rebuild the calendar client from the current settings (Settings → Integrations; no restart)."""
        from ..calendar.setup import client_from_config
        with self._cal_lock:
            try:
                client, cal_id = client_from_config(self.s.config)
                self.calendar_error = None
            except Exception as exc:  # noqa: BLE001 - scheduling must work without the calendar
                LOG.exception("calendar credentials unavailable")
                client, cal_id, self.calendar_error = None, None, str(exc)
            was = bool(self.calendar_client and self.calendar_id)
            self.calendar_client, self.calendar_id = client, cal_id
        if client and cal_id and not was:
            threading.Thread(target=self.sync_calendar, name="calendar-first-sync", daemon=True).start()
        return {"running": bool(client and cal_id), "error": self.calendar_error}

    def sync_calendar(self) -> None:
        from .calendar import sync
        was_bad = bool(self.last_calendar.get("error"))
        try:
            self.last_calendar = {"at": self.s.clock().isoformat(), **sync(self.s, self.calendar_client, self.calendar_id)}
        except Exception as exc:  # noqa: BLE001 - the database stays authoritative; the calendar is a mirror
            LOG.exception("calendar sync failed")
            self.last_calendar = {"at": self.s.clock().isoformat(), "error": str(exc)}
        bad = bool(self.last_calendar.get("error"))
        if bad != was_bad:
            with self.s.engine.begin() as conn:
                self.s.notify(conn, "calendar", None, "Google Calendar sync is failing: "
                              f"{self.last_calendar['error']}" if bad else "Google Calendar sync recovered.")

    def watch_health(self, interval_s: float = 900) -> None:
        """Post to Slack only when a check enters or leaves FAIL (no repeated noise)."""
        from .health import health

        def loop() -> None:
            failing: set[str] = set()
            while not self._stop.wait(interval_s):
                try:
                    h = health(self.s, self, self.processor)
                    now_fail = {f"{c['area']}: {c['check']}" for c in h["checks"] if c["status"] == "FAIL"}
                    new, gone = now_fail - failing, failing - now_fail
                    if new or gone:
                        detail = {f"{c['area']}: {c['check']}": c for c in h["checks"]}
                        lines = [f"FAIL {k}: {detail[k]['detail']}" + (f" → {detail[k]['action']}" if detail[k]["action"] else "")
                                 for k in sorted(new)] + [f"recovered: {k}" for k in sorted(gone)]
                        with self.s.engine.begin() as conn:
                            self.s.notify(conn, "health", None, "Observatory health changed:\n" + "\n".join(lines))
                    failing = now_fail
                except Exception:  # noqa: BLE001
                    LOG.exception("health watch failed")
        threading.Thread(target=loop, name="health-watch", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.slack.stop()
        self.runner.stop()
        if self.processor is not None:
            self.processor.stop()
