"""Executes broker jobs on this computer or a remote host: check, reserve, record, monitor, validate, release.

scheduled -> preparing -> recording -> recorded | failed -> processing -> completed. In `preparing` the runner
checks, in order (brief 7):

  1. connectivity (device on USB here, or the remote host answers and the device is on its USB)
  2-4. frequency, sample rate and gain against what the receiver can do now
  5. storage (room for the file where it is recorded, and here for the copy of a remote recording)
  6. availability (no SDR++ session, no other program holding the device)
  7. an exclusive reservation (scheduling.reservations), then the station's other locks (coordinator lease,
     ANTSW-01 switch port)

and then starts the recorder through a backend (scheduling.backends: local process, SSH, later an agent),
monitors it (8-9: lease renewal, SSH drops, host reboots, overruns), validates the output (10) and releases
everything (11). A rule job whose receiver fails a check moves to the rule's first compatible backup.
Every check and error is kept on the job (sched_jobs.execution) with timestamps and a plain explanation.

Recordings run as separate processes in their own session. If the broker restarts, recover() adopts a capture
that is still running instead of starting another one or killing it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Protocol

from ..logjson import log_event
from .backends import BackendError, Backends, LocalBackend, RecordingRequest
from .diagnose import Diagnostics, device_errors, explain, log_tail, message_for
from .hardware import Receiver, validate_radio
from .reservations import RENEW_S, Reservations, ReservationBusy
from .store import Scheduler

LOG = logging.getLogger("aobs.runner")
SPAWN_LEAD_S = 15.0   # the capture process opens the SDR early and waits for record_start
STOP_GRACE_S = 90     # a recorder still running this long after the scheduled end is stopped
KILL_GRACE_S = 30     # ... and killed if SIGTERM did not end it
UNREACHABLE_GRACE_S = 600   # a remote host out of contact this long after the end: the job fails
POLL_S = {"local": 1.0, "ssh": 5.0}
COLLECT_TRIES, COLLECT_RETRY_S = 5, 10.0


class RunnerHardware(Protocol):
    def present(self, rx: Receiver) -> bool: ...
    def busy(self, rx: Receiver) -> list[str]: ...
    def coordinator_reserve(self, rx: Receiver, owner: str, ttl_s: int, purpose: str) -> str | None: ...
    def coordinator_release(self, rx: Receiver, token: str) -> None: ...
    def spawn(self, argv: list[str], log_path: str) -> Any: ...
    def alive(self, pid: int, job_id: str) -> bool: ...


class StartRefused(RuntimeError):
    """A start check failed: the recording does not start (and may move to a backup receiver)."""

    def __init__(self, message: str, code: str = "", check: str = ""):
        super().__init__(message)
        self.code, self.check = code, check


# Start checks whose failure means "this receiver cannot record now", so a rule's backup may take the pass.
UNAVAILABLE = {"device_missing", "busy", "usb_claimed", "api_busy", "reserved", "ssh_unreachable", "ssh_auth",
               "ssh_host_key", "disk_full", "permission", "missing_tool", "missing_python_module", "timeout",
               "closed", "refused", "dns", "unreachable", "host_key_changed", "host_key_unknown", "auth_failed",
               "unsupported", "session"}


class Runner:
    def __init__(self, scheduler: Scheduler, hw: RunnerHardware,
                 take_switched: Callable[[Receiver, str, int | None, dt.datetime], str] | None = None,
                 renew_switched: Callable[[str, dt.datetime], None] | None = None,
                 release_switched: Callable[[str], None] | None = None,
                 find_switched: Callable[[str], str | None] | None = None,
                 sleep: Callable[[float], None] = time.sleep, processor: Any = None, hosts: Any = None,
                 backends: Backends | None = None):
        self.s = scheduler
        self._hw = hw
        self.take_switched = take_switched
        self.renew_switched = renew_switched
        self.release_switched = release_switched
        self.find_switched = find_switched
        self.sleep = sleep
        self.processor = processor            # processing.Processor: post-recording work runs as low-priority attempts
        self.backends = backends or Backends(scheduler, LocalBackend(scheduler, hw, sleep), hosts)
        self.reservations = Reservations(scheduler.engine, scheduler.clock)
        scheduler.runner = self               # catalog (terminate) reaches running recordings through it
        scheduler.reservations = self.reservations
        self._threads: dict[str, threading.Thread] = {}
        self._stop = threading.Event()

    @property
    def hw(self) -> RunnerHardware:
        return self._hw

    @hw.setter
    def hw(self, value: RunnerHardware) -> None:      # the test lab swaps the hardware double after construction
        self._hw = value
        self.backends.local.hw = value

    # ------------------------------------------------------------ loop
    def start(self, tick_s: float = 1.0) -> None:
        self.recover()

        def loop() -> None:
            while not self._stop.wait(tick_s):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - the loop must survive any single failure
                    LOG.exception("runner tick failed")
        threading.Thread(target=loop, name="runner", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self, threaded: bool = True) -> None:
        now = self.s.clock()
        for job in self.s.jobs(status=["scheduled"], end=now + dt.timedelta(seconds=1), executes=True, limit=1000):
            if not job["executes"] or job["job_id"] in self._threads:
                continue
            if dt.datetime.fromisoformat(job["record_end"]) <= now:
                self.s.transition(job["job_id"], "missed", "the broker was not running during this reservation",
                                  expect=("scheduled",), notify=self._say(job, "was missed (broker not running)"))
                continue
            self._launch(job, self.execute, threaded)
        for jid, t in list(self._threads.items()):
            if not t.is_alive():
                del self._threads[jid]

    def _launch(self, job: dict[str, Any], fn: Callable[[dict[str, Any]], None], threaded: bool) -> None:
        if not threaded:
            fn(job)
            return
        t = threading.Thread(target=fn, args=(job,), name=f"job-{job['job_id']}", daemon=True)
        self._threads[job["job_id"]] = t
        t.start()

    # ------------------------------------------------------------ execution
    def execute(self, job: dict[str, Any]) -> None:
        """Checks, reservation, recording, monitoring, validation, release (brief 7, steps 1-11)."""
        jid = job["job_id"]
        rx = self.s.receivers[job["receiver_id"]]
        if not self.s.transition(jid, "preparing", "checking the receiver", expect=("scheduled",)):
            return
        diag = Diagnostics(self.s.clock, rx.id)
        held: dict[str, Any] = {}
        try:
            job, rx, backend, req = self._prepare(job, rx, diag, held)
            if not self._wait_for_start(job, rx, held):
                diag.note("start", "cancelled while preparing; the receiver is released")
                log_event(LOG, "job cancelled during preparation", job_id=jid)
                return                       # cancelled while waiting; `finally` releases the receiver
            handle = backend.start(req)
            held["handle"] = handle
            diag.note("start", f"recorder started on {handle['host']} (pid {handle['pid']})")
            self._save(jid, diag, backend=backend.kind, handle=handle)
            self.s.transition(jid, "recording", f"capture process {handle['pid']}"
                              + (f" on {handle['host']}" if handle["host"] != "local" else ""),
                              expect=("preparing",), pid=handle["pid"],
                              notify=self._say(job, f"is recording now (until {job['record_end'][11:16]}Z)")
                              if job["source"] == "manual" else None)
            log_event(LOG, "recording", job_id=jid, receiver=rx.id, pid=handle["pid"], host=handle["host"])
            self._stamp_rf_path(jid)
            self._watch(job, rx, backend, handle, req, held, diag)
        except StartRefused as exc:
            hint = message_for(exc.code, rx.id) or {}
            diag.note("check", str(exc), "error", code=exc.code or "unknown", action=hint.get("action", ""))
            self.s.transition(jid, "failed", f"{exc}", expect=("preparing",), notify=self._say(job, f"did not start: {exc}"))
        except Exception as exc:  # noqa: BLE001
            LOG.exception("job %s failed", jid)
            e = diag.error("execution", f"{type(exc).__name__}: {exc}")
            msg = e["message"] if e["code"] != "unknown" else f"{type(exc).__name__}: {exc}"
            self.s.transition(jid, "failed", msg if e["code"] == "unknown" else f"{msg} ({exc})",
                              expect=("preparing", "recording"), notify=self._say(job, f"failed: {msg}"))
        finally:
            self._release(rx, held)
            self._save(jid, diag)

    def _prepare(self, job: dict[str, Any], rx: Receiver, diag: Diagnostics,
                 held: dict[str, Any]) -> tuple[dict[str, Any], Receiver, Any, RecordingRequest]:
        """Checks 1-7 on the job's receiver; a rule job whose receiver cannot record tries the rule's backups."""
        try:
            return self._checks_and_reserve(job, rx, diag, held)
        except StartRefused as exc:
            if exc.code not in UNAVAILABLE:
                raise
            moved = self._try_backup(job, rx, diag, str(exc))
            if moved is None:
                raise
            job2, rx2 = moved
            return self._checks_and_reserve(job2, rx2, diag, held)

    def _checks_and_reserve(self, job: dict[str, Any], rx: Receiver, diag: Diagnostics,
                            held: dict[str, Any]) -> tuple[dict[str, Any], Receiver, Any, RecordingRequest]:
        jid = job["job_id"]
        try:
            req = RecordingRequest.from_job(job, rx, self.s.settings)
        except BackendError as exc:
            raise StartRefused(str(exc), exc.code, "request") from None
        # 2-4: frequency, sample rate, gain, format against what the receiver can do now (it may have changed)
        errors: list[str] = []
        warnings: list[str] = []
        if not job["spec"].get("steps"):      # stepped captures were checked step by step at booking
            validate_radio(rx, dict(job["spec"]["radio"]), job["spec"]["recording"].get("engine") or rx.engines()[0],
                           req.format, errors, warnings)
        freq = [e for e in errors if "frequency" in e.lower()]
        rate = [e for e in errors if "sample" in e.lower() or "rate" in e.lower() or "bandwidth" in e.lower()]
        gain = [e for e in errors if e not in freq and e not in rate]
        diag.check("frequency", not freq, "; ".join(freq) or f"{req.radio['center_frequency_hz'] / 1e6:.4f} MHz is supported")
        diag.check("sample rate", not rate, "; ".join(rate) or f"{req.radio['sample_rate_hz']} S/s is supported")
        diag.check("gain", not gain, "; ".join(gain) or "gain settings are valid")
        if errors:
            raise StartRefused(f"{rx.id} cannot record these settings: {'; '.join(errors)}", "rate_rejected", "settings")
        self._sessions_yield(job, rx, diag)
        try:
            backend = self.backends.for_receiver(rx)
        except Exception as exc:  # noqa: BLE001 - unknown or disabled host
            diag.check("connected", False, str(exc), "bad_config")
            raise StartRefused(f"{rx.id}: {exc}", "bad_config", "connected") from None
        # 1, 5, 6: connectivity, storage, not in use by another program
        if not backend.preflight(req, rx, diag):
            failed = next(c for c in reversed(diag.checks) if c["ok"] is False)
            e = message_for(failed.get("code") or "", rx.id) or explain(failed["detail"], rx.id)
            code = failed.get("code") or e["code"]
            msg = e["message"] if e["code"] != "unknown" else f"{rx.id}: {failed['check']} check failed"
            raise StartRefused(f"{msg} ({failed['detail']})", code, failed["check"])
        # 7: exclusive reservation (Starlab's own jobs), then the station's other lock holders
        try:
            held["reservation"] = (rx, self.reservations.acquire(rx, jid, f"{job['source']} job {job['job_name']}"))
        except ReservationBusy as exc:
            diag.check("reserved", False, str(exc), "reserved")
            raise StartRefused(str(exc), "reserved", "reserved") from None
        diag.check("reserved", True, f"{rx.id} reserved for {jid}")
        self._acquire_station_locks(job, rx, held)
        return job, rx, backend, req

    def _sessions_yield(self, job: dict[str, Any], rx: Receiver, diag: Diagnostics) -> None:
        """A listening session must never silently eat a recording."""
        if getattr(self.s, "sessions", None) is None:
            return
        now = self.s.clock()
        for sess in self.s.session_conflicts(rx.serial, now, now + dt.timedelta(seconds=1)):
            if (sess.get("policy") or {}).get("end_before_passes"):
                self.s.sessions.stop(sess["session_id"], "starlab", f"recording {job['job_name']} is starting")
                diag.note("check", f"ended SDR++ session {sess['session_id']} (set to end for recordings)")
            else:
                diag.check("not in use", False, f"lent to SDR++ session {sess['session_id']}", "session")
                raise StartRefused(f"{rx.id} is lent to SDR++ session {sess['session_id']} ({sess['created_by']}), "
                                   "which is set not to end for recordings", "session", "not in use")

    def _acquire_station_locks(self, job: dict[str, Any], rx: Receiver, held: dict[str, Any]) -> None:
        until = dt.datetime.fromisoformat(job["reservation_end"])
        if rx.switched:
            if not self.take_switched:
                raise RuntimeError(f"{rx.id} needs the broker's lease path, which is not wired")
            held["switched"] = self.take_switched(rx, job["job_id"], job["spec"]["resolved"].get("switch_port"), until)
        elif not rx.remote:
            ttl = int((until - self.s.clock()).total_seconds()) + 300
            token = self.hw.coordinator_reserve(rx, f"aobs-scheduler:{job['job_id']}", ttl,
                                                f"scheduled job {job['job_name']}")
            if token:
                held["coordinator"] = (rx, token)

    def _try_backup(self, job: dict[str, Any], rx: Receiver, diag: Diagnostics,
                    why: str) -> tuple[dict[str, Any], Receiver] | None:
        """Move a rule job to the first compatible, free, working backup receiver of its rule.

        Only rule jobs nobody changed by hand: a person's choice of receiver (manual jobs, overrides) is kept and
        fails visibly instead. A backup on a different antenna is used only when the signal-path layout confirms
        that antenna's path covers the frequency; the move is an event and a notification, never silent."""
        from . import spec as specmod
        from .rules import build_job, owned_elsewhere, rule_receivers
        if not job.get("rule_id"):
            return None
        if job.get("override"):
            diag.note("backup", "not trying backup receivers: this recording was changed by hand")
            return None
        try:
            rule = self.s.rules.get(job["rule_id"])
        except Exception:  # noqa: BLE001 - rule deleted since booking
            return None
        meta = job["spec"].get("metadata") or {}
        p = {"norad": meta.get("norad"), "name": (job["spec"].get("target") or {}).get("name"), "aos": meta.get("aos"),
             "los": meta.get("los"), "max_elevation_deg": meta.get("max_elevation_deg"), "max_at": meta.get("max_at"),
             "aos_azimuth_deg": meta.get("aos_azimuth_deg"), "los_azimuth_deg": meta.get("los_azimuth_deg"),
             "duration_s": meta.get("duration_s"), "elements_epoch": meta.get("elements_epoch"),
             "elements_source": meta.get("elements_source")}
        if not p["norad"] or not p["aos"]:
            return None
        want_ant = str((job["spec"].get("receiver") or {}).get("antenna") or "").lower()
        ctx = self.s.context()
        for rid in rule_receivers(rule):
            if rid == rx.id:
                continue
            cand = self.s.receivers.get(rid)
            if cand is None or owned_elsewhere(cand) or not cand.schedulable:
                diag.note("backup", f"backup {rid} skipped: not usable by rules")
                continue
            problems: list[str] = []
            raw = build_job(self.s, rule, p, cand, problems)
            checked = specmod.check_job(raw, ctx)
            errs = problems + [e for e in checked.errors if "less than" not in e]
            if errs:
                diag.note("backup", f"backup {rid} skipped: {'; '.join(errs)}")
                continue
            new_ant = str((checked.job.get("receiver") or {}).get("antenna") or "").lower()
            path_warn = [w for w in checked.warnings if w.startswith("signal path")]
            if want_ant and new_ant != want_ant and (not ctx.rf_layout or path_warn):
                diag.note("backup", f"backup {rid} skipped: it is on antenna {new_ant or 'unknown'}, not {want_ant}, and "
                          + ("; ".join(path_warn) if path_warn else
                             "no signal-path layout confirms that antenna covers this frequency"))
                continue
            sub = Diagnostics(self.s.clock, rid)
            try:
                req = RecordingRequest.from_job({**job, "spec": checked.job, "paths": specmod.output_paths(
                    checked.job, job["job_id"]), "estimated_bytes": checked.job["resolved"].get("estimated_bytes")},
                    cand, self.s.settings)
                ok = self.backends.for_receiver(cand).preflight(req, cand, sub)
            except Exception as exc:  # noqa: BLE001
                ok = False
                sub.check("connected", False, str(exc))
            if not ok:
                bad = next((c for c in reversed(sub.checks) if c["ok"] is False), {"detail": "?"})
                diag.note("backup", f"backup {rid} cannot record either: {bad['detail']}")
                continue
            checked.job["metadata"]["warnings"] = checked.warnings
            antenna = "" if not new_ant or new_ant == want_ant else f" on antenna {new_ant} instead of {want_ant}"
            reason = f"moved from {rx.id} to backup {rid}{antenna}: {why}"
            moved = self.s.move_for_start(job["job_id"], cand, checked.job, reason)
            if moved is None:
                diag.note("backup", f"backup {rid} was taken meanwhile")
                continue
            diag.note("backup", reason, "warning")
            diag.rx_id = rid
            return moved, cand
        diag.note("backup", "no backup receiver could take the recording")
        return None

    def _wait_for_start(self, job: dict[str, Any], rx: Receiver, held: dict[str, Any]) -> bool:
        """Hold the receiver until just before the recording; keep the reservation alive. False if cancelled."""
        start = dt.datetime.fromisoformat(job["record_start"])
        remaining = (start - self.s.clock()).total_seconds() - SPAWN_LEAD_S
        while remaining > 0:
            step = min(remaining, RENEW_S)
            self.sleep(step)
            remaining -= step
            self._renew(held)
            if (self.s.get(job["job_id"]) or {}).get("status") != "preparing":
                return False
        return (self.s.get(job["job_id"]) or {}).get("status") == "preparing"

    def _renew(self, held: dict[str, Any]) -> bool:
        if "reservation" not in held:
            return True
        rx, token = held["reservation"]
        return self.reservations.renew(rx, token)

    def _stamp_rf_path(self, jid: str) -> None:
        """Record the antenna-to-receiver path as it is at capture start; never fails the recording."""
        if getattr(self.s, "rf", None) is None:
            return
        try:
            job = self.s.get(jid)
            if job is not None:
                self.s.rf.stamp(job)
        except Exception:  # noqa: BLE001
            LOG.exception("could not record the signal path of %s", jid)

    def _release(self, rx: Receiver, held: dict[str, Any]) -> None:
        if "switched" in held and self.release_switched:
            try:
                self.release_switched(held.pop("switched"))
            except Exception:  # noqa: BLE001
                LOG.exception("releasing %s lease failed", rx.id)
        if "coordinator" in held:
            crx, token = held.pop("coordinator")
            try:
                self.hw.coordinator_release(crx, token)
            except Exception:  # noqa: BLE001
                LOG.exception("releasing %s coordinator lease failed", crx.id)
        if "reservation" in held:
            rrx, token = held.pop("reservation")
            try:
                self.reservations.release(rrx, token)
            except Exception:  # noqa: BLE001
                LOG.exception("releasing %s reservation failed", rrx.id)

    def _save(self, jid: str, diag: Diagnostics, **values: Any) -> None:
        """The job's execution record: start checks, diagnostics, backend and handle (sched_jobs.execution)."""
        try:
            cur = (self.s.get(jid) or {}).get("execution") or {}
            self.s.update(jid, execution={**cur, **values, **diag.as_dict()})
        except Exception:  # noqa: BLE001 - bookkeeping never fails a recording
            LOG.exception("could not save the execution record of %s", jid)

    # ------------------------------------------------------------ monitoring
    def _watch(self, job: dict[str, Any], rx: Receiver, backend: Any, handle: dict[str, Any], req: RecordingRequest,
               held: dict[str, Any], diag: Diagnostics) -> None:
        """9: poll the recorder until it ends; stop it if it overruns; ride out SSH drops; detect host reboots."""
        jid = job["job_id"]
        end = dt.datetime.fromisoformat(job["record_end"])
        poll_s = POLL_S.get(backend.kind, 1.0)
        last_renew = last_switch = time.monotonic()
        stopping: dt.datetime | None = None
        unreachable_since: dt.datetime | None = None
        while True:
            state = backend.poll(handle)
            now = self.s.clock()
            if state == "running":
                if unreachable_since is not None:
                    diag.note("monitor", f"{handle['host']} answers again; the recorder kept running")
                    unreachable_since = None
                if stopping is None and now > end + dt.timedelta(seconds=STOP_GRACE_S):
                    diag.error("monitor", f"the recorder did not stop {STOP_GRACE_S} s after the scheduled end")
                    backend.stop(handle)
                    stopping, held["stopped_by"] = now, "timeout"
                elif stopping is not None and now > stopping + dt.timedelta(seconds=KILL_GRACE_S):
                    diag.note("monitor", "the recorder ignored SIGTERM; killed", "error")
                    backend.stop(handle, hard=True)
                    stopping = now + dt.timedelta(days=1)
            elif state == "unreachable":
                if unreachable_since is None:
                    unreachable_since = now
                    diag.note("monitor", f"lost contact with {handle['host']}; the recording continues there and "
                              "Starlab keeps trying", "warning", code="ssh_unreachable")
                if now > end + dt.timedelta(seconds=UNREACHABLE_GRACE_S):
                    raise RuntimeError(f"{handle['host']} could not be reached since {unreachable_since:%H:%M:%S}Z "
                                       f"(Connection timed out); the recording stays on that host if it finished")
            elif state == "rebooted":
                raise RuntimeError(f"{handle['host']} rebooted during the recording (boot id changed)")
            else:
                break                                   # finished (or lost)
            if time.monotonic() - last_renew > RENEW_S:
                if not self._renew(held):
                    diag.note("monitor", "the receiver reservation was lost (expired); recording continues",
                              "warning")
                last_renew = time.monotonic()
            if "switched" in held and self.renew_switched and time.monotonic() - last_switch > 60:
                try:
                    self.renew_switched(held["switched"], dt.datetime.fromisoformat(job["reservation_end"]))
                except Exception:  # noqa: BLE001
                    LOG.exception("lease renew failed for %s", jid)
                last_switch = time.monotonic()
            self.sleep(poll_s)
        capture = self._collect(backend, handle, req, diag)
        self._finish_capture(job, capture, diag, stopped_by=held.get("stopped_by"))

    def _collect(self, backend: Any, handle: dict[str, Any], req: RecordingRequest, diag: Diagnostics) -> dict[str, Any]:
        for attempt in range(COLLECT_TRIES):
            try:
                capture = backend.collect(handle, req)
                if capture.get("transfer"):
                    t = capture["transfer"]
                    diag.note("collect", f"copied from {handle['host']}: {t.get('bytes', 0) / 1e6:.1f} MB, verified by "
                              f"{t.get('verified')}" if t.get("ok") else f"copy from {handle['host']} failed: {t.get('error')}",
                              "info" if t.get("ok") else "error")
                return capture
            except BackendError as exc:
                diag.note("collect", f"attempt {attempt + 1}: {exc}", "warning")
                if attempt + 1 < COLLECT_TRIES:
                    self.sleep(COLLECT_RETRY_S)
        raise RuntimeError(f"the result could not be read from {handle['host']} (Connection timed out); "
                           "the recording is kept on that host")

    def _finish_capture(self, job: dict[str, Any], capture: dict[str, Any] | None = None, diag: Diagnostics | None = None,
                        stopped_by: str | None = None) -> None:
        """10: validate the output; recorded only when the file proves it."""
        jid = job["job_id"]
        if capture is None:                   # restart recovery: the local result file
            try:
                capture = json.loads(pathlib.Path(job["paths"]["raw"] + ".result.json").read_text())
            except (OSError, ValueError):
                capture = {"status": "failed", "error": "capture process ended without writing a result"}
        diag = diag or Diagnostics(self.s.clock, job["receiver_id"])
        rx_id = job["receiver_id"]
        if (job["spec"].get("metadata") or {}).get("rf_test"):
            try:                          # hardware evidence for the receiver's readiness (inventory)
                from ..inventory.readiness import on_test_capture
                on_test_capture(self.s, job, capture)
            except Exception:  # noqa: BLE001 - readiness bookkeeping must not fail the test itself
                LOG.exception("readiness update failed for %s", jid)
        if capture.get("status") in ("recorded", "interrupted") and capture.get("bytes") and self.processor is not None:
            try:                          # IQ health + tuner log; never blocks finishing the job
                capture.update(self.processor.assess_capture(job, capture) or {})
            except Exception:  # noqa: BLE001
                LOG.exception("IQ health assessment failed for %s", jid)
        if stopped_by == "timeout":
            e = explain("did not stop", rx_id)
            diag.note("validate", e["message"], "error", code=e["code"])
            self.s.transition(jid, "failed", f"{e['message']} ({(capture.get('bytes') or 0) / 1e9:.2f} GB kept)",
                              expect=("recording",), result={"capture": capture},
                              notify=self._say(job, f"failed: {e['message']}"))
            self._index_kept(jid, capture, "raw IQ (recorder overran)")
            return
        if capture.get("status") == "interrupted" and capture.get("bytes"):
            # stopped on purpose (confirmed /radio-cancel): keep what was captured, as a partial recording
            self.s.transition(jid, "partial", f"terminated on request; {capture.get('bytes', 0) / 1e9:.2f} GB kept",
                              expect=("recording",), result={"capture": capture},
                              notify=self._say(job, f"was terminated; {capture.get('bytes', 0) / 1e9:.2f} GB kept as partial"))
            self._index_kept(jid, capture, "raw IQ (partial)")
            return
        if capture.get("status") != "recorded":
            raw_err = capture.get("error") or capture.get("status", "failed")
            log = diagnose_log(job)
            e = explain(f"{raw_err}\n{log}" if raw_err else log, rx_id)
            diag.note("validate", e["message"], "error", code=e["code"], action=e["action"], detail=raw_err)
            reason = raw_err if e["code"] == "unknown" else f"{e['message']} ({raw_err})"
            self.s.transition(jid, "failed", reason, expect=("recording",), result={"capture": capture},
                              notify=self._say(job, f"recording failed: {reason}"))
            self._index_kept(jid, capture, "raw IQ (failed recording)")
            return
        problems, warnings = check_output(job, capture, diagnose_log(job))
        capture["validation"] = {"ok": not problems, "problems": problems, "warnings": warnings,
                                 "at": self.s.clock().isoformat()}
        for w in warnings:
            diag.note("validate", w, "warning")
        if problems:      # a job is never completed on the recorder's word alone (brief 6)
            for p_ in problems:
                diag.note("validate", p_, "error")
            self.s.transition(jid, "failed", "the recording output failed validation: " + "; ".join(problems),
                              expect=("recording",), result={"capture": capture},
                              notify=self._say(job, f"recorded a file that failed validation: {'; '.join(problems)}"))
            self._index_kept(jid, capture, "raw IQ (failed validation)")
            return
        diag.note("validate", "output validated")
        self.s.transition(jid, "recorded", f"{capture.get('bytes', 0) / 1e9:.2f} GB recorded and validated",
                          expect=("recording",), result={"capture": capture})
        self.post_recording(self.s.get(jid))

    def _index_kept(self, jid: str, capture: dict[str, Any], label: str) -> None:
        f = capture.get("file")
        if self.processor is not None and f and pathlib.Path(f).exists():
            self.processor.index(jid, None, [{"kind": "raw_iq", "path": f, "label": label}])

    def post_recording(self, job: dict[str, Any]) -> None:
        """Queue the processing the request asked for; the processor runs it below recording priority."""
        jid = job["job_id"]
        if self.processor is None:
            self.s.transition(jid, "completed", "recorded (no processor configured)", expect=("recorded",))
            return
        self.processor.index_capture(job)
        # Move to `processing` before queueing, so a fast attempt cannot finish while the job is still `recorded`.
        self.s.transition(jid, "processing", "queueing post-recording processing", expect=("recorded",))
        try:
            attempts = self.processor.submit_post_recording(job)
        except Exception as exc:  # noqa: BLE001 - the recording itself succeeded
            self.s.transition(jid, "completed", f"recorded; processing could not be queued: {exc}", expect=("processing",),
                              notify=self._say(job, f"recorded, but processing could not be queued: {exc}"))
            return
        self.s.update(jid, status_reason=f"processing attempts {', '.join(attempts)}")

    # ------------------------------------------------------------ terminating on request
    def terminate(self, job: dict[str, Any]) -> bool:
        """SIGTERM to a running recorder (keeps what was recorded; the job ends as partial)."""
        handle = self._handle(job)
        if handle is None:
            return False
        return self.backends.for_handle(handle).stop(handle)

    @staticmethod
    def _handle(job: dict[str, Any]) -> dict[str, Any] | None:
        h = (job.get("execution") or {}).get("handle")
        if h:
            return dict(h)
        if job.get("pid"):
            return {"backend": "local", "host": "local", "pid": job["pid"], "job_id": job["job_id"]}
        return None

    # ------------------------------------------------------------ restart
    def recover(self) -> None:
        """Adopt captures that survived a broker restart; resume interrupted post-processing; clear stale reservations."""
        try:
            for c in self.reservations.sweep():
                LOG.info("cleared reservation of %s on %s: %s", c["job_id"], c["receiver_id"], c["why"])
        except Exception:  # noqa: BLE001
            LOG.exception("reservation sweep failed")
        for job in self.s.jobs(status=["preparing", "recording", "recorded", "processing"]):
            if not job["executes"]:
                continue
            jid = job["job_id"]
            if job["status"] == "recorded":
                self.post_recording(job)      # crashed between recording and queueing its processing
                continue
            if job["status"] == "processing":
                continue                      # its attempts are recovered by the processor
            handle = self._handle(job) if job["status"] == "recording" else None
            rx = self.s.receivers.get(job["receiver_id"])
            if handle is not None and rx is not None:
                try:
                    backend = self.backends.for_handle(handle)
                    state = backend.poll(handle)
                except Exception as exc:  # noqa: BLE001
                    backend, state = None, f"error: {exc}"
                if backend is not None and state in ("running", "unreachable"):
                    log_event(LOG, "adopting running capture", job_id=jid, pid=handle["pid"], host=handle["host"])
                    self._launch(job, lambda j, rx=rx, b=backend, h=handle: self._adopt(j, rx, b, h), True)
                    continue
                if backend is not None and state == "finished":
                    self._launch(job, lambda j, rx=rx, b=backend, h=handle: self._adopt(j, rx, b, h), True)
                    continue
            if job["status"] == "recording" and pathlib.Path(job["paths"]["raw"] + ".result.json").exists():
                self._finish_capture(job)
            else:
                self.s.transition(jid, "failed", "broker restarted while preparing; the capture was not running",
                                  expect=("preparing", "recording"), notify=self._say(job, "failed: broker restart"))

    def _adopt(self, job: dict[str, Any], rx: Receiver, backend: Any, handle: dict[str, Any]) -> None:
        held: dict[str, Any] = {}
        diag = Diagnostics(self.s.clock, rx.id)
        diag.entries = list(((job.get("execution") or {}).get("diagnostics")) or [])
        diag.checks = list(((job.get("execution") or {}).get("checks")) or [])
        diag.note("monitor", "the broker restarted; adopted the running recorder")
        if rx.switched and self.find_switched:
            token = self.find_switched(f"scheduler:{job['job_id']}")
            if token:
                held["switched"] = token
        try:
            held["reservation"] = (rx, self.reservations.acquire(rx, job["job_id"], "adopted after restart"))
        except ReservationBusy as exc:
            diag.note("monitor", f"could not re-take the reservation: {exc}", "warning")
        try:
            req = RecordingRequest.from_job(job, rx, self.s.settings)
            self._watch(job, rx, backend, handle, req, held, diag)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("adopted job %s failed", job["job_id"])
            e = diag.error("monitor", f"{exc}")
            self.s.transition(job["job_id"], "failed", e["message"] if e["code"] != "unknown" else str(exc),
                              expect=("recording",), notify=self._say(job, f"failed: {exc}"))
        finally:
            self._release(rx, held)
            self._save(job["job_id"], diag)

    @staticmethod
    def _say(job: dict[str, Any], what: str) -> str:
        kind = "Your job" if job["source"] == "manual" else "Automatic recording"
        return f"{kind} {job['job_name']} ({job['job_id']}) on {job['receiver_id']} {what}"


def diagnose_log(job: dict[str, Any]) -> str:
    return log_tail(str((job.get("paths") or {}).get("log") or ""), 16_000)


def validate_output(job: dict[str, Any], capture: dict[str, Any]) -> list[str]:
    """Why a capture that says "recorded" is not a usable recording (empty when it is). See check_output."""
    return check_output(job, capture)[0]


FATAL_DEVICE = ("LIBUSB_ERROR_NO_DEVICE", "cb transfer status: 5", "Failed to submit transfer", "usb_claim_interface")


def check_output(job: dict[str, Any], capture: dict[str, Any], log_text: str = "") -> tuple[list[str], list[str]]:
    """(problems, warnings) for a capture that says "recorded" (brief 6, extended in brief 7).

    Checks the file itself, not the recorder's report: it exists and is not empty; it is the size the recorder
    reported; it holds at least 98% of the samples the schedule asked for and a whole number of samples; the
    recorder's sample count matches the file; the samples cover the time the recorder says it ran (duration);
    the recorder process exited cleanly; the log shows no device failure (USB disconnects, failed transfers);
    and, where IQ health applies, the samples are not flat or a capture failure. Dropped-sample counts and
    minor device messages are warnings."""
    out: list[str] = []
    warn: list[str] = []
    f = capture.get("file") or ((job.get("paths") or {}).get("raw"))
    path = pathlib.Path(f) if f else None
    if path is None or not path.is_file():
        return [f"the recording file {f or '(none)'} does not exist"], warn
    size = path.stat().st_size
    if size == 0:
        out.append("the recording file is empty")
    if capture.get("bytes") is not None and int(capture["bytes"]) != size:
        out.append(f"the file holds {size} bytes but the recorder reported {capture['bytes']}")
    spec = job.get("spec") or {}
    from .hardware import BYTES_PER_SAMPLE
    per = BYTES_PER_SAMPLE.get(str((spec.get("recording") or {}).get("format") or ""))
    if not spec.get("steps"):
        expected = capture.get("expected_bytes")
        if expected and size < 0.98 * int(expected):
            out.append(f"{size} of {expected} expected bytes ({size / int(expected):.0%})")
        if per and size % per:
            out.append(f"{size} bytes is not a whole number of {per}-byte samples (the file is cut)")
        if per and capture.get("samples") is not None and not capture.get("steps") \
                and int(capture["samples"]) * per != size:
            out.append(f"the recorder counted {capture['samples']} samples but the file holds {size // per}")
        rate = (spec.get("radio") or {}).get("sample_rate_hz")
        try:
            span = (dt.datetime.fromisoformat(capture["actual_end"])
                    - dt.datetime.fromisoformat(capture["actual_start"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            span = None
        if per and rate and span is not None and span > 5:
            got_s = size / per / float(rate)
            if got_s > span * 1.05 + 2:
                out.append(f"the file holds {got_s:.0f} s of samples but the recorder ran only {span:.0f} s "
                           "(wrong sample rate or format)")
            elif got_s < span * 0.9 - 2:
                warn.append(f"the file holds {got_s:.0f} s of samples over {span:.0f} s of recording (samples were lost)")
    if capture.get("exit_code") not in (None, 0):
        out.append(f"the recorder process exited with status {capture['exit_code']}")
    if capture.get("overflows"):
        warn.append(f"{capture['overflows']} receive overflow(s): some samples were dropped")
    errs = device_errors(log_text)
    fatal = [e for e in errs if any(k in e for k in FATAL_DEVICE)]
    if fatal:
        out.append(f"device errors during the recording: {fatal[0]}" + (f" (+{len(fatal) - 1} more)" if len(fatal) > 1 else ""))
    warn += [f"recorder log: {e}" for e in errs if e not in fatal][:5]
    iq = capture.get("iq_health") or {}
    if iq.get("applicable") and iq.get("status") in ("capture_failure", "suspicious_flat"):
        out.append(f"IQ health: {iq.get('label')} ({iq.get('summary')})")
    return out, warn


def device_users(rx: Receiver) -> list[str]:
    """Who else has a (non-RTL, non-SDRplay) receiver open: processes, or a claimed USB interface."""
    from ..inventory.discovery import find_device, usage
    dev = find_device(rx.serial, usb_path=rx.usb_path if rx.identity == "usb_path" else None)
    if dev is None:
        return []                                  # absence is reported by present(), not as "busy"
    u = usage(dev)
    if u["free"]:
        return []
    return u["holders"] or [u["detail"]]


class LiveRunnerHardware:
    """USB presence/holders, legacy coordinator leases, detached capture processes."""

    def __init__(self, scheduler: Scheduler, coordinator_hw: Any):
        self.s = scheduler
        self.coord = coordinator_hw            # coordination.hardware.LiveHardware
        self._station_receivers: set[str] = set()      # receivers the original station's coordinator knows
        if scheduler.config.legacy_enabled:
            from ..legacy import load_legacy
            self._station_receivers = {r["id"] for r in load_legacy(scheduler.config).station.get("receivers", [])}

    def present(self, rx: Receiver) -> bool:
        return self.s._present(rx)

    def busy(self, rx: Receiver) -> list[str]:
        if rx.driver in ("sdrplay", "sim"):
            return []   # SDRplay: the API service always holds the USB device (open() fails if in use); sim: no device
        if rx.driver == "rtlsdr":
            return self.coord.device_busy(rx.serial)
        return device_users(rx)

    def coordinator_reserve(self, rx: Receiver, owner: str, ttl_s: int, purpose: str) -> str | None:
        if rx.id not in self._station_receivers:
            return None
        return self.coord.coordinator_reserve(rx.id, owner, ttl_s, purpose)

    def coordinator_release(self, rx: Receiver, token: str) -> None:
        self.coord.coordinator_release(rx.id, token)

    def spawn(self, argv: list[str], log_path: str) -> Any:
        from ..inventory.hosts import hardware_blocked
        why = hardware_blocked()
        if why:                       # development and tests never open a physical SDR (simulated receivers only)
            run = json.loads(pathlib.Path(argv[-1]).read_text())
            if run.get("driver") != "sim":
                raise RuntimeError(f"not opening {run.get('serial')}: {why}")
        pathlib.Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        log = open(log_path, "ab")
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        return subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True, env=env)

    def signal(self, pid: int, job_id: str, sig: int) -> bool:
        """Signal a recorder's process group, only if the PID is still this job's capture process."""
        if not self.alive(pid, job_id):
            return False
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return False
        return True

    def alive(self, pid: int, job_id: str) -> bool:
        try:
            cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
        except OSError:
            return False
        return "aobs.scheduling.capture" in cmdline and job_id in cmdline
