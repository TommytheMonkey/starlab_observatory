"""The processing queue runs one attempt at a time, so an attempt that never ends must not stop every later one:
per-kind timeouts, a worker heartbeat, requeue of a dead worker's attempt, and cancelling the running attempt."""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import signal
from typing import Any

from aobs.scheduling.processing import HEARTBEAT, Processor
from test_archive import recorded
from test_scheduler import env  # noqa: F401  (fixture)


class Worker:
    """A worker process the test controls: it runs until a signal (or the test) ends it."""
    pid = 31337

    def __init__(self, obeys_sigterm: bool = True) -> None:
        self.returncode: int | None = None
        self.obeys = obeys_sigterm

    def poll(self) -> int | None:
        return self.returncode


def controlled(env: Any, **settings: Any) -> Processor:
    env.s.settings.update(settings)
    workers: list[Worker] = []

    def spawn(argv: list[str], log: str) -> Worker:
        workers.append(Worker(env.s.settings.get("_obeys", True)))
        return workers[-1]

    def sig(pid: int, s: int) -> None:
        proc.signals.append(s)
        w = workers[-1]
        if s == signal.SIGKILL or (s == signal.SIGTERM and w.obeys):
            w.returncode = -s
    proc = Processor(env.s, spawn=spawn, alive=lambda pid, aid: False, signal_group=sig)
    proc.signals, proc.workers = [], workers
    return proc


def analyze(proc: Processor, jid: str) -> str:
    return proc.submit(jid, "analyze", {"analysis": ["checksum"]}, "api")["attempt_id"]


def later(env: Any, seconds: float) -> None:
    env.clock.now = env.clock.now + dt.timedelta(seconds=seconds)


def test_an_attempt_past_its_timeout_is_stopped_failed_and_the_queue_moves_on(env):
    jid, _ = recorded(env, satdump={"enabled": False}, postprocess={"decode": False})
    proc = controlled(env, processing_timeout_seconds={"analyze": 600})
    a1, a2 = analyze(proc, jid), analyze(proc, jid)
    proc.step()
    assert proc.attempt(a1)["status"] == "running"
    later(env, 590)
    proc.step()
    assert proc.signals == []                                 # still inside its limit
    later(env, 20)
    proc.step()                                               # over the limit: SIGTERM to the process group
    assert signal.SIGTERM in proc.signals
    proc.step()                                               # it exited: failed, with the reason
    a = proc.attempt(a1)
    assert a["status"] == "failed" and "timed out" in a["status_reason"] and "limit 10 min" in a["status_reason"]
    proc.step()
    assert proc.attempt(a2)["status"] == "running"            # the queue continues
    assert not any("requeued" in (x["status_reason"] or "") for x in proc.attempts(jid))


def test_a_worker_that_ignores_sigterm_is_killed_after_the_grace_period(env):
    jid, _ = recorded(env, satdump={"enabled": False}, postprocess={"decode": False})
    proc = controlled(env, processing_timeout_seconds=60, processing_kill_grace_seconds=15, _obeys=False)
    a1 = analyze(proc, jid)
    proc.step()
    later(env, 61)
    proc.step()
    assert proc.signals == [signal.SIGTERM]
    later(env, 16)
    proc.step()
    assert proc.signals == [signal.SIGTERM, signal.SIGKILL]
    proc.step()
    assert proc.attempt(a1)["status"] == "failed"


def test_time_paused_for_a_recording_does_not_count_against_the_timeout(env):
    jid, _ = recorded(env, satdump={"enabled": False}, postprocess={"decode": False})
    proc = controlled(env, processing_timeout_seconds={"analyze": 600})
    a1 = analyze(proc, jid)
    proc.step()
    proc.busy_recording = lambda: ["OBS-R02:m1"]              # a receiver records: the gate pauses the worker
    proc.step()
    assert proc.signals == [signal.SIGSTOP] and proc.attempt(a1)["status"] == "paused"
    later(env, 3600)
    proc.step()
    assert signal.SIGTERM not in proc.signals                 # paused: never timed out
    proc.busy_recording = lambda: []
    proc.step()                                               # the recording ended: resumed
    later(env, 300)
    proc.step()
    assert proc.attempt(a1)["status"] == "running" and signal.SIGTERM not in proc.signals


def test_a_worker_whose_heartbeat_stops_is_stopped_and_its_attempt_requeued_once(env):
    jid, _ = recorded(env, satdump={"enabled": False}, postprocess={"decode": False})
    proc = controlled(env, processing_heartbeat_stale_seconds=300)
    a1 = analyze(proc, jid)
    proc.step()
    hb = pathlib.Path(proc.attempt(a1)["output_dir"]) / HEARTBEAT
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text("beat")
    t = env.clock.now.timestamp()
    os.utime(hb, (t, t))
    later(env, 200)
    proc.step()
    assert proc.signals == []
    later(env, 150)                                           # 350 s without a beat
    proc.step()
    assert proc.signals == [signal.SIGTERM]
    proc.step()
    a = proc.attempt(a1)
    assert a["status"] == "failed" and "no heartbeat" in a["status_reason"] and "requeued as p" in a["status_reason"]
    again = [x for x in proc.attempts(jid) if x["status"] == "queued"]
    assert len(again) == 1 and again[0]["params"] == a["params"]


def test_a_dead_worker_is_requeued_once_then_failed_for_good(env):
    jid, _ = recorded(env, satdump={"enabled": False}, postprocess={"decode": False})
    proc = controlled(env, processing_max_requeues=1)
    a1 = analyze(proc, jid)
    proc.step()
    proc.workers[-1].returncode = -9                          # killed by the OOM killer, say: no result.json
    proc.step()
    a = proc.attempt(a1)
    assert a["status"] == "failed" and "killed by signal 9" in a["status_reason"] and "requeued as p" in a["status_reason"]
    proc.step()
    second = proc.current["attempt_id"]
    proc.workers[-1].returncode = -9
    proc.step()
    b = proc.attempt(second)
    assert b["status"] == "failed" and "requeued" not in b["status_reason"]
    assert not [x for x in proc.attempts(jid) if x["status"] == "queued"]


def test_cancelling_the_running_attempt_stops_it_with_the_reason_and_does_not_requeue(env):
    jid, _ = recorded(env, satdump={"enabled": False}, postprocess={"decode": False})
    proc = controlled(env)
    a1, a2 = analyze(proc, jid), analyze(proc, jid)
    proc.step()
    assert proc.cancel(a1, "stuck: cleared by the operator")["status"] == "stopping"
    proc.step()
    a = proc.attempt(a1)
    assert a["status"] == "failed" and a["status_reason"].startswith("stuck: cleared by the operator")
    proc.step()
    assert proc.attempt(a2)["status"] == "running"
    assert not any("requeued" in (x["status_reason"] or "") for x in proc.attempts(jid))


def test_a_worker_adopted_after_a_broker_restart_is_held_to_the_same_timeout(env):
    """The attempt that jammed the queue on 2026-11-24 had been running for hours: after the upgrade the restarted
    broker adopts it and stops it at once."""
    jid, _ = recorded(env, satdump={"enabled": False}, postprocess={"decode": False})
    first = controlled(env)
    a1 = analyze(first, jid)
    first.step()
    later(env, 5 * 3600)
    proc = controlled(env)
    w = first.workers[-1]
    proc.workers.append(w)
    proc._alive = lambda pid, aid: w.returncode is None       # still running when the new broker starts
    proc.recover()
    assert proc.current and proc.current["attempt_id"] == a1 and proc.current["handle"] is None
    proc.step()
    assert signal.SIGTERM in proc.signals
    proc.step()
    assert "timed out: ran 300 min" in proc.attempt(a1)["status_reason"]
