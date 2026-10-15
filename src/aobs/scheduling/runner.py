"""Executes broker jobs: lock the physical SDR, record, release, post-process.

scheduled -> preparing (reservation start: device present, not held by anyone,
coordinator lease / R05 lease + switch port) -> recording (capture process
started at record start) -> recorded | failed -> processing -> completed.

Recordings run as separate processes in their own session. If the broker
restarts, recover() adopts a capture that is still running instead of starting
another one or killing it.
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
from .hardware import Receiver
from .store import Scheduler

LOG = logging.getLogger("aobs.runner")
SPAWN_LEAD_S = 15.0   # the capture process opens the SDR early and waits for record_start


class RunnerHardware(Protocol):
    def present(self, rx: Receiver) -> bool: ...
    def busy(self, rx: Receiver) -> list[str]: ...
    def coordinator_reserve(self, rx: Receiver, owner: str, ttl_s: int, purpose: str) -> str | None: ...
    def coordinator_release(self, rx: Receiver, token: str) -> None: ...
    def spawn(self, argv: list[str], log_path: str) -> Any: ...
    def alive(self, pid: int, job_id: str) -> bool: ...


class Runner:
    def __init__(self, scheduler: Scheduler, hw: RunnerHardware,
                 take_switched: Callable[[Receiver, str, int | None, dt.datetime], str] | None = None,
                 renew_switched: Callable[[str, dt.datetime], None] | None = None,
                 release_switched: Callable[[str], None] | None = None,
                 find_switched: Callable[[str], str | None] | None = None,
                 sleep: Callable[[float], None] = time.sleep, processor: Any = None):
        self.s = scheduler
        self.hw = hw
        self.take_switched = take_switched
        self.renew_switched = renew_switched
        self.release_switched = release_switched
        self.find_switched = find_switched
        self.sleep = sleep
        self.processor = processor            # processing.Processor: post-recording work runs as low-priority attempts
        self._threads: dict[str, threading.Thread] = {}
        self._stop = threading.Event()

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
        jid = job["job_id"]
        rx = self.s.receivers[job["receiver_id"]]
        if not self.s.transition(jid, "preparing", "reserving the receiver", expect=("scheduled",)):
            return
        held: dict[str, Any] = {}
        try:
            self._acquire(job, rx, held)
            run = self._run_file(job, rx)
            start = dt.datetime.fromisoformat(job["record_start"])
            wait = (start - self.s.clock()).total_seconds() - SPAWN_LEAD_S
            if wait > 0:
                self.sleep(wait)
            if (self.s.get(jid) or {}).get("status") != "preparing":
                log_event(LOG, "job cancelled during preparation", job_id=jid)
                return                       # cancelled while waiting; `finally` releases the receiver
            handle = self.hw.spawn([sys.executable, "-m", "aobs.scheduling.capture", run], job["paths"]["log"])
            self.s.transition(jid, "recording", f"capture process {handle.pid}", expect=("preparing",), pid=handle.pid,
                              notify=self._say(job, f"is recording now (until {job['record_end'][11:16]}Z)")
                              if job["source"] == "manual" else None)
            log_event(LOG, "recording", job_id=jid, receiver=rx.id, pid=handle.pid)
            self._stamp_rf_path(jid)
            self._watch(job, handle.pid, held, handle)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("job %s failed", jid)
            self.s.transition(jid, "failed", f"{type(exc).__name__}: {exc}", expect=("preparing", "recording"),
                              notify=self._say(job, f"failed: {exc}"))
        finally:
            self._release(rx, held)

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

    def _acquire(self, job: dict[str, Any], rx: Receiver, held: dict[str, Any]) -> None:
        sessions = getattr(self.s, "sessions", None)
        if sessions is not None:                          # a listening session must never silently eat a recording
            now = self.s.clock()
            for sess in self.s.session_conflicts(rx.serial, now, now + dt.timedelta(seconds=1)):
                if (sess.get("policy") or {}).get("end_before_passes"):
                    sessions.stop(sess["session_id"], "starlab", f"recording {job['job_name']} is starting")
                else:
                    raise RuntimeError(f"{rx.id} is lent to SDR++ session {sess['session_id']} "
                                       f"({sess['created_by']}), which is set not to end for recordings")
        if not self.hw.present(rx):
            raise RuntimeError(f"receiver unavailable: {rx.id} (serial {rx.serial}) is not connected")
        until = dt.datetime.fromisoformat(job["reservation_end"])
        if rx.switched:
            if not self.take_switched:
                raise RuntimeError(f"{rx.id} needs the broker's lease path, which is not wired")
            held["switched"] = self.take_switched(rx, job["job_id"], job["spec"]["resolved"].get("switch_port"), until)
        else:
            ttl = int((until - self.s.clock()).total_seconds()) + 300
            token = self.hw.coordinator_reserve(rx, f"aobs-scheduler:{job['job_id']}", ttl,
                                                f"scheduled job {job['job_name']}")
            if token:
                held["coordinator"] = token
            deadline = time.monotonic() + 20
            while True:
                busy = self.hw.busy(rx)
                if not busy:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{rx.id} is held by {', '.join(busy)}; not opening it a second time")
                self.sleep(1)

    def _release(self, rx: Receiver, held: dict[str, Any]) -> None:
        if "switched" in held and self.release_switched:
            try:
                self.release_switched(held.pop("switched"))
            except Exception:  # noqa: BLE001
                LOG.exception("releasing %s lease failed", rx.id)
        if "coordinator" in held:
            try:
                self.hw.coordinator_release(rx, held.pop("coordinator"))
            except Exception:  # noqa: BLE001
                LOG.exception("releasing %s coordinator lease failed", rx.id)

    def _run_file(self, job: dict[str, Any], rx: Receiver) -> str:
        spec, paths = job["spec"], job["paths"]
        pathlib.Path(paths["directory"]).mkdir(parents=True, exist_ok=True)
        run = {"job_id": job["job_id"], "driver": rx.driver, "serial": rx.serial, "radio": spec["radio"],
               "format": spec["recording"]["format"], "record_start": job["record_start"], "record_end": job["record_end"],
               "raw": paths["raw"], "result": paths["raw"] + ".result.json",
               "rtl_sdr_path": self.s.settings.get("rtl_sdr_path", "/usr/bin/rtl_sdr")}
        if rx.gain_model == "elements":
            run["soapy_driver"] = rx.soapy
        if rx.simulated:
            run["simulated"] = True
        if spec.get("steps"):
            run["steps"] = spec["steps"]
        path = str(pathlib.Path(paths["directory"]) / f".{job['job_id']}.run.json")  # job id in argv lets alive() match
        pathlib.Path(path).write_text(json.dumps(run, indent=2))
        return path

    def _watch(self, job: dict[str, Any], pid: int, held: dict[str, Any], handle: Any = None) -> None:
        jid = job["job_id"]
        last_renew = time.monotonic()
        while True:
            running = handle.poll() is None if handle is not None else self.hw.alive(pid, jid)
            if not running:
                break
            if "switched" in held and self.renew_switched and time.monotonic() - last_renew > 60:
                try:
                    self.renew_switched(held["switched"], dt.datetime.fromisoformat(job["reservation_end"]))
                except Exception:  # noqa: BLE001
                    LOG.exception("lease renew failed for %s", jid)
                last_renew = time.monotonic()
            self.sleep(1)
        self._finish_capture(job)

    def _finish_capture(self, job: dict[str, Any]) -> None:
        jid = job["job_id"]
        result_path = pathlib.Path(job["paths"]["raw"] + ".result.json")
        try:
            capture = json.loads(result_path.read_text())
        except (OSError, ValueError):
            capture = {"status": "failed", "error": "capture process ended without writing a result"}
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
        if capture.get("status") == "interrupted" and capture.get("bytes"):
            # stopped on purpose (confirmed /radio-cancel): keep what was captured, as a partial recording
            self.s.transition(jid, "partial", f"terminated on request; {capture.get('bytes', 0) / 1e9:.2f} GB kept",
                              expect=("recording",), result={"capture": capture},
                              notify=self._say(job, f"was terminated; {capture.get('bytes', 0) / 1e9:.2f} GB kept as partial"))
            if self.processor is not None:
                self.processor.index(jid, None, [{"kind": "raw_iq", "path": capture.get("file") or "", "label": "raw IQ (partial)"}])
            return
        if capture.get("status") != "recorded":
            self.s.transition(jid, "failed", capture.get("error") or capture.get("status", "failed"),
                              expect=("recording",), result={"capture": capture},
                              notify=self._say(job, f"recording failed: {capture.get('error')}"))
            return
        self.s.transition(jid, "recorded", f"{capture.get('bytes', 0) / 1e9:.2f} GB recorded", expect=("recording",),
                          result={"capture": capture})
        self.post_recording(self.s.get(jid))

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

    # ------------------------------------------------------------ restart
    def recover(self) -> None:
        """Adopt captures that survived a broker restart; resume interrupted post-processing."""
        for job in self.s.jobs(status=["preparing", "recording", "recorded", "processing"]):
            if not job["executes"]:
                continue
            jid = job["job_id"]
            if job["status"] == "recorded":
                self.post_recording(job)      # crashed between recording and queueing its processing
                continue
            if job["status"] == "processing":
                continue                      # its attempts are recovered by the processor
            pid = job.get("pid")
            if job["status"] == "recording" and pid and self.hw.alive(pid, jid):
                log_event(LOG, "adopting running capture", job_id=jid, pid=pid)
                rx = self.s.receivers[job["receiver_id"]]
                self._launch(job, lambda j, rx=rx, pid=pid: self._adopt(j, rx, pid), True)
            elif job["status"] == "recording" and pathlib.Path(job["paths"]["raw"] + ".result.json").exists():
                self._finish_capture(job)
            else:
                self.s.transition(jid, "failed", "broker restarted while preparing; the capture was not running",
                                  expect=("preparing", "recording"), notify=self._say(job, "failed: broker restart"))

    def _adopt(self, job: dict[str, Any], rx: Receiver, pid: int) -> None:
        held: dict[str, Any] = {}
        if rx.switched and self.find_switched:
            token = self.find_switched(f"scheduler:{job['job_id']}")
            if token:
                held["switched"] = token
        try:
            self._watch(job, pid, held)
        finally:
            self._release(rx, held)

    @staticmethod
    def _say(job: dict[str, Any], what: str) -> str:
        kind = "Your job" if job["source"] == "manual" else "Automatic recording"
        return f"{kind} {job['job_name']} ({job['job_id']}) on {job['receiver_id']} {what}"


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
        pathlib.Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        log = open(log_path, "ab")
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        return subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True, env=env)

    def alive(self, pid: int, job_id: str) -> bool:
        try:
            cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
        except OSError:
            return False
        return "aobs.scheduling.capture" in cmdline and job_id in cmdline
