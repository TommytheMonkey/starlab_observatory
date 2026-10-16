"""Brief 7: receiver reservations, recording backends (local, SSH, agent), start checks, backups, diagnostics.

Simulated throughout: hardware doubles for this computer, scripted SSH hosts, and one real (throwaway) sshd on
127.0.0.1 that records from a simulated receiver. No physical SDR is opened.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import getpass
import json
import os
import pathlib
import shutil
import signal
import sys
import threading
import time
import types
from typing import Any

import pytest
from sqlalchemy import select

from aobs.db import init_db, make_engine
from aobs.db.schema import sched_reservations
from aobs.inventory.hosts import HostStore, RunResult
from aobs.inventory.drivers import SIM_CAPS
from aobs.scheduling import runner as runner_mod
from aobs.scheduling.backends import BackendError, Backends, LocalBackend, RecordingRequest, SshBackend
from aobs.scheduling.diagnose import Diagnostics, explain
from aobs.scheduling.reservations import LEASE_S, Reservations, ReservationBusy
from aobs.scheduling.rules import RulePlanner
from aobs.scheduling.runner import Runner
from aobs.scheduling.store import Scheduler
from test_rule_scheduler import M23, Passes, rule, rule_jobs, save
from test_scheduler import NOW, Clock, FakeHandle, FakeRunnerHw, fix_dirs, rtl_job, sched_config

UTC = dt.timezone.utc


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def ex(tmp_config, tmp_path):
    cfg = sched_config(tmp_config, tmp_path)
    rxs = cfg.raw["scheduler"]["receivers"]
    rxs["OBS-R02"].pop("auto")
    rxs["OBS-R03"] = {"driver": "rtlsdr", "serial": "OBS-R03", "antenna": "DISCONE", "signal_path": "Discone > R03",
                      "mode": "broker"}                                     # same antenna as R02 (a splitter)
    rxs["OBS-R04"] = {"driver": "rtlsdr", "serial": "OBS-R04", "antenna": "HF-WIRE", "signal_path": "HF wire > R04",
                      "mode": "broker"}                                     # another antenna
    engine = make_engine(cfg.database_url)
    init_db(engine)
    clock = Clock()
    present = {"OBS-R01", "OBS-R02", "OBS-R03", "OBS-R04", "OBS-R05", "240526D360"}
    free = {"bytes": 500 * 10**9}
    s = Scheduler(cfg, engine, clock, disk_free=lambda _: free["bytes"], present=lambda rx: rx.serial in present,
                  satdump_pipelines=frozenset({"meteor_m2-x_lrpt", "noaa_apt"}))
    save(s, "observatory", latitude=45.0, longitude=10.0, elevation_m=100)
    save(s, "scheduling", mode="hybrid")
    return types.SimpleNamespace(s=s, clock=clock, present=present, free=free, cfg=cfg, tmp=tmp_path, engine=engine)


class Hw(FakeRunnerHw):
    """This computer's hardware, simulated: presence follows ex.present; the recorder's outcome is chosen."""

    def __init__(self, ex: Any, mode: str = "ok"):
        super().__init__(ok=mode != "failed")
        self.ex, self.mode, self.signals = ex, mode, []
        self.handle: Any = None

    def present(self, rx):
        return rx.serial in self.ex.present

    def signal(self, pid, job_id, sig):
        self.signals.append(sig)
        if self.handle is not None and (sig == signal.SIGKILL or self.mode != "overrun"):
            self.handle.code = -sig
        return True

    def spawn(self, argv, log):
        run = json.loads(pathlib.Path(argv[-1]).read_text())
        self.calls.append(("spawn", run["serial"]))
        raw, result = pathlib.Path(run["raw"]), pathlib.Path(run["result"])
        raw.parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(log).parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "crash":                             # killed by the OOM killer: no result, half a file
            (raw.parent / (raw.name + ".part")).write_bytes(b"\0" * 1000)
            pathlib.Path(log).write_text("Found 1 device(s)\nKilled\n")
            return FakeHandle(4242)
        if self.mode == "enospc":
            pathlib.Path(log).write_text("")
            result.write_text(json.dumps({"status": "failed", "bytes": 4096,
                                          "error": "OSError: [Errno 28] No space left on device"}))
            return FakeHandle(4242)
        size = 1_310_720 + (1 if self.mode == "cut" else 0)
        raw.write_bytes(b"\x7f" * size)
        res = {"status": "recorded", "bytes": size, "actual_start": run["record_start"]}
        if self.mode == "usb_errors":
            pathlib.Path(log).write_text("Reading samples in async mode...\ncb transfer status: 5, canceling...\n")
        if self.mode == "samples":
            res["samples"] = 12
        result.write_text(json.dumps(res))
        if self.mode in ("overrun", "exit1"):
            self.handle = Running(4243 if self.mode == "overrun" else 4244)
            if self.mode == "exit1":
                self.handle.code = 1
            return self.handle
        return FakeHandle(4242)


class Running:
    """A recorder that keeps running until it is signalled (or already exited with `code`)."""

    def __init__(self, pid: int):
        self.pid, self.code = pid, None

    def poll(self):
        return self.code


def manual(ex: Any, name: str = "meteor-test", rx: str = "OBS-R02", **kw: Any) -> str:
    """Book a manual recording (minutes after NOW); the clock goes back to NOW to book it."""
    ex.clock.now = NOW
    res = ex.s.submit([fix_dirs(rtl_job(name, rx=rx, satdump={"enabled": False}, postprocess={"decode": False}, **kw),
                                ex.tmp)], "all_or_nothing", "test")["results"][0]
    assert res["ok"], res["errors"]
    return res["job_id"]


def run(ex: Any, hw: Any, jid: str, **kw: Any) -> Runner:
    ex.clock.now = dt.datetime.fromisoformat(ex.s.get(jid)["reservation_start"]) + dt.timedelta(seconds=1)
    r = Runner(ex.s, hw, sleep=kw.pop("sleep", lambda s: None), **kw)
    r.tick(threaded=False)
    return r


def checks(job: dict[str, Any]) -> dict[str, Any]:
    return {c["check"]: c["ok"] for c in job["execution"]["checks"]}


# ------------------------------------------------------------------ reservations
def test_reservations_are_exclusive_per_physical_device_and_atomic(ex, tmp_path):
    res = Reservations(ex.engine, ex.clock)
    r02 = ex.s.receivers["OBS-R02"]
    a, b = manual(ex, "job-a"), manual(ex, "job-b", start=200, end=210)
    tok = res.acquire(r02, a, "test")
    with pytest.raises(ReservationBusy, match=f"reserved by Starlab job {a}"):
        res.acquire(r02, b)
    alias = dataclasses.replace(r02, id="ALIAS")                # a second inventory entry for the same dongle
    with pytest.raises(ReservationBusy):
        res.acquire(alias, b)
    assert res.acquire(ex.s.receivers["OBS-R03"], b)            # another device is independent
    assert res.renew(r02, tok) and not res.renew(r02, "forged")
    assert res.release(r02, tok) and res.acquire(r02, b)

    # many brokers racing for one free device, each for a live job of its own: exactly one wins
    r04 = ex.s.receivers["OBS-R04"]
    racers = [manual(ex, f"race-{i}", rx="OBS-R04", start=300 + 20 * i, end=310 + 20 * i) for i in range(8)]
    winners, errors = [], []
    barrier = threading.Barrier(8)

    def grab(i: int) -> None:
        r = Reservations(make_engine(ex.cfg.database_url), ex.clock, holder=f"broker-{i}")
        barrier.wait()
        try:
            r.acquire(r04, racers[i])
            winners.append(i)
        except ReservationBusy:
            pass
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=grab, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(winners) == 1


def test_expired_and_abandoned_reservations_are_recovered(ex):
    crashed = Reservations(ex.engine, ex.clock, holder="old-broker:123")
    mine = Reservations(ex.engine, ex.clock)
    r02 = ex.s.receivers["OBS-R02"]
    a, b = manual(ex, "job-a"), manual(ex, "job-b", start=200, end=210)
    crashed.acquire(r02, a)
    with pytest.raises(ReservationBusy):
        mine.acquire(r02, b)
    ex.clock.now += dt.timedelta(seconds=LEASE_S + 1)              # the old broker stopped renewing
    tok = mine.acquire(r02, b)
    mine.release(r02, tok)

    crashed.acquire(r02, a)
    ex.s.transition(a, "failed", "test")                          # its job ended: the reservation is abandoned
    assert mine.acquire(r02, b)
    crashed.acquire(ex.s.receivers["OBS-R03"], a)
    cleared = mine.sweep()
    assert [c["receiver_id"] for c in cleared] == ["OBS-R03"] and "abandoned" in cleared[0]["why"]


# ------------------------------------------------------------------ the eleven steps
def test_a_recording_runs_every_check_then_reserves_records_validates_and_releases(ex):
    jid = manual(ex)
    hw = Hw(ex)
    run(ex, hw, jid)
    job = ex.s.get(jid)
    assert job["status"] == "completed", job["status_reason"]
    assert [c["check"] for c in job["execution"]["checks"]] == ["frequency", "sample rate", "gain", "connected",
                                                                 "storage", "not in use", "reserved"]
    assert all(checks(job).values())
    assert job["execution"]["backend"] == "local" and job["execution"]["handle"]["pid"] == 4242
    assert all(c["at"] for c in job["execution"]["checks"])           # timestamps
    assert any(d["message"] == "output validated" for d in job["execution"]["diagnostics"])
    assert job["result"]["capture"]["validation"]["ok"]
    assert hw.calls == [("reserve", "OBS-R02"), ("spawn", "OBS-R02"), ("release", "OBS-R02")]
    assert ex.s.reservations.current() == []                           # released


def test_another_starlab_job_holding_the_receiver_stops_the_start(ex):
    jid = manual(ex)
    other = manual(ex, "other-broker-job", start=200, end=210)
    ex.clock.now = dt.datetime.fromisoformat(ex.s.get(jid)["reservation_start"])
    Reservations(ex.engine, ex.clock, holder="second-broker").acquire(ex.s.receivers["OBS-R02"], other, "test capture")
    hw = Hw(ex)
    run(ex, hw, jid)
    job = ex.s.get(jid)
    assert job["status"] == "failed" and ("spawn", "OBS-R02") not in hw.calls
    assert f"reserved by Starlab job {other}" in job["status_reason"]
    assert checks(job)["reserved"] is False


def test_a_claimed_usb_interface_is_explained(ex):
    jid = manual(ex)
    hw = Hw(ex)
    hw.busy_with = ["in use: a program has claimed its USB interface"]
    run(ex, hw, jid)
    job = ex.s.get(jid)
    assert job["status_reason"].startswith(
        "OBS-R02 could not start because another process has claimed its USB interface.")
    assert ("spawn", "OBS-R02") not in hw.calls
    err = [d for d in job["execution"]["diagnostics"] if d["level"] == "error"][-1]
    assert err["code"] == "usb_claimed" and "Close the other program" in err["action"] and err["at"]
    assert "claimed its USB interface" in ex.s.notifications()[-1]["text"]


def test_a_receiver_that_disappeared_is_reported_before_anything_is_held(ex):
    jid = manual(ex)
    ex.present.discard("OBS-R02")
    hw = Hw(ex)
    run(ex, hw, jid)
    job = ex.s.get(jid)
    assert job["status"] == "failed" and "OBS-R02 was not found on USB" in job["status_reason"]
    assert hw.calls == [] and checks(job)["connected"] is False


def test_recorder_crash_partial_and_cut_files_fail_with_reasons(ex):
    jid = manual(ex, "crash")
    run(ex, Hw(ex, "crash"), jid)
    job = ex.s.get(jid)
    assert job["status"] == "failed" and "recorder stopped unexpectedly" in job["status_reason"]
    assert "without writing a result" in job["status_reason"]

    for mode, why in (("cut", "not a whole number of 2-byte samples"), ("samples", "recorder counted 12 samples"),
                      ("usb_errors", "device errors during the recording: cb transfer status: 5"),
                      ("exit1", "exited with status 1")):
        jid = manual(ex, f"mode-{mode}")
        run(ex, Hw(ex, mode), jid)
        job = ex.s.get(jid)
        assert job["status"] == "failed" and why in job["status_reason"], (mode, job["status_reason"])
        assert job["lifecycle"] == "FAILED"


def test_disk_full_before_and_during_the_recording(ex):
    jid = manual(ex)
    ex.free["bytes"] = 10 * 1024**2
    hw = Hw(ex)
    run(ex, hw, jid)
    job = ex.s.get(jid)
    assert "not enough disk space" in job["status_reason"] and checks(job)["storage"] is False
    assert ("spawn", "OBS-R02") not in hw.calls

    ex.free["bytes"] = 500 * 10**9
    jid = manual(ex, "fills-up")
    run(ex, Hw(ex, "enospc"), jid)
    job = ex.s.get(jid)
    assert job["status"] == "failed" and "not enough disk space" in job["status_reason"]
    assert "No space left on device" in job["status_reason"]


def test_an_overrunning_recorder_is_stopped_then_killed(ex, monkeypatch):
    jid = manual(ex)
    hw = Hw(ex, "overrun")

    def sleep(s: float) -> None:
        ex.clock.now += dt.timedelta(seconds=s)
    run(ex, hw, jid, sleep=sleep)
    job = ex.s.get(jid)
    assert hw.signals == [signal.SIGTERM, signal.SIGKILL]
    assert job["status"] == "failed" and "did not stop at the end of the recording" in job["status_reason"]
    assert ex.s.reservations.current() == []


def test_terminating_goes_through_the_backend_that_started_the_recorder(ex):
    jid = manual(ex)
    hw = Hw(ex)
    r = run(ex, hw, jid)
    job = ex.s.get(jid)
    assert r.terminate(job) and hw.signals == [signal.SIGTERM]
    plain = Runner(ex.s, FakeRunnerHw(), sleep=lambda s: None)       # no verified signal path: nothing is signalled
    assert not plain.terminate(job)


def test_a_restarted_broker_adopts_the_recording_and_takes_over_its_reservation(ex):
    jid = manual(ex)
    stale = manual(ex, "ended-long-ago", start=200, end=210)
    old = Reservations(ex.engine, ex.clock, holder="old-broker:1")
    rx = ex.s.receivers["OBS-R02"]
    ex.clock.now = dt.datetime.fromisoformat(ex.s.get(jid)["record_start"]) + dt.timedelta(seconds=5)
    old.acquire(rx, jid, "recording")
    ex.s.transition(jid, "preparing", "")
    ex.s.transition(jid, "recording", "", pid=4242,
                    execution={"backend": "local", "handle": {"backend": "local", "host": "local", "pid": 4242,
                                                              "job_id": jid}})
    old.acquire(ex.s.receivers["OBS-R03"], stale)
    ex.s.transition(stale, "cancelled", "test")
    paths = ex.s.get(jid)["paths"]
    pathlib.Path(paths["raw"]).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(paths["raw"]).write_bytes(b"\x7f" * 1000)
    pathlib.Path(paths["raw"] + ".result.json").write_text(json.dumps({"status": "recorded", "bytes": 1000}))
    hw = Hw(ex)
    lives = {"n": 3}

    def alive(pid, job_id):
        lives["n"] -= 1
        return pid == 4242 and job_id == jid and lives["n"] > 0
    hw.alive = alive
    r = Runner(ex.s, hw, sleep=lambda s: None)
    r.recover()
    r._threads[jid].join(10)
    job = ex.s.get(jid)
    assert job["status"] == "completed", job["status_reason"]
    assert any("adopted the running recorder" in d["message"] for d in job["execution"]["diagnostics"])
    assert not any("could not re-take" in d["message"] for d in job["execution"]["diagnostics"])
    with ex.engine.connect() as conn:
        assert conn.execute(select(sched_reservations)).all() == []   # re-taken, then released; stale one swept


# ------------------------------------------------------------------ backup receivers
def book_rule(ex: Any, monkeypatch: Any, **over: Any) -> dict[str, Any]:
    Passes(monkeypatch, [(M23, 120, 12, 60)])
    ex.s.rules.create(rule(**over), "owner")
    RulePlanner(ex.s).reconcile(refresh=False)
    jobs = rule_jobs(types.SimpleNamespace(engine=ex.engine), status="scheduled")
    assert len(jobs) == 1 and jobs[0]["receiver_id"] == "OBS-R02"
    return ex.s.get(jobs[0]["job_id"])


def test_an_unavailable_receiver_hands_the_pass_to_a_compatible_backup(ex, monkeypatch):
    job = book_rule(ex, monkeypatch, backup_receivers=["OBS-R04", "OBS-R03"])
    ex.present.discard("OBS-R02")                                    # unplugged after booking
    hw = Hw(ex)
    run(ex, hw, job["job_id"])
    done = ex.s.get(job["job_id"])
    assert done["status"] == "completed" and done["receiver_id"] == "OBS-R03", done["status_reason"]
    assert hw.calls[1] == ("spawn", "OBS-R03")
    moved = [e for e in done["events"] if "moved from OBS-R02 to backup OBS-R03" in (e["reason"] or "")]
    assert moved and "was not found on USB" in moved[0]["reason"]
    notes = " ".join(d["message"] for d in done["execution"]["diagnostics"])
    assert "backup OBS-R04 skipped: it is on antenna hf-wire, not discone" in notes      # never a silent antenna swap
    assert any(n["kind"] == "moved" for n in ex.s.notifications())
    assert "OBS-R03" in done["paths"]["raw"]


def test_no_backup_for_a_recording_changed_by_hand_or_without_a_compatible_receiver(ex, monkeypatch):
    job = book_rule(ex, monkeypatch, backup_receivers=["OBS-R03"])
    ex.s.modify(job["job_id"], {"note": "keep this on R02"}, "owner")
    ex.present.discard("OBS-R02")
    run(ex, Hw(ex), job["job_id"])
    done = ex.s.get(job["job_id"])
    assert done["status"] == "failed" and done["receiver_id"] == "OBS-R02"
    assert any("changed by hand" in d["message"] for d in done["execution"]["diagnostics"])


def test_only_incompatible_backups_means_the_job_fails_on_its_own_receiver(ex, monkeypatch):
    job = book_rule(ex, monkeypatch, backup_receivers=["OBS-R04"])
    ex.present.discard("OBS-R02")
    run(ex, Hw(ex), job["job_id"])
    done = ex.s.get(job["job_id"])
    assert done["status"] == "failed" and done["receiver_id"] == "OBS-R02"
    assert "OBS-R02 was not found on USB" in done["status_reason"]


# ------------------------------------------------------------------ requests and remote hosts
def test_recording_requests_are_validated_before_any_backend_sees_them(ex):
    jid = manual(ex)
    job, rx = ex.s.get(jid), ex.s.receivers["OBS-R02"]
    req = RecordingRequest.from_job(job, rx, ex.s.settings)
    assert req.run_doc("r", "x")["serial"] == "OBS-R02"
    for change, why in ((dict(serial="R02; rm -rf ~"), "serial"), (dict(job_id="../../etc"), "job id"),
                        (dict(raw="/data/../etc/passwd"), "without '..'"), (dict(format="wav"), "format"),
                        (dict(radio={**req.radio, "sample_rate_hz": "2.4e6"}), "sample_rate_hz"),
                        (dict(record_end=req.record_start), "recording times"),
                        (dict(host="pi", steps=[{"center_hz": 1}]), "this computer only")):
        with pytest.raises(BackendError, match=why.replace("(", r"\(")):
            dataclasses.replace(req, **change).validate()


class ScriptedHost:
    """A remote host that answers each fixed script with a canned reply."""
    label = "starlab@pi:22"
    hostname, port = "pi.lan", 22

    def __init__(self) -> None:
        self.replies: dict[str, RunResult] = {}
        self.calls: list[list[str]] = []

    def run(self, argv, input=None, timeout=60):
        self.calls.append(argv)
        script = argv[2] if len(argv) > 2 else ""
        for key, reply in self.replies.items():
            if key in script:
                return reply
        return RunResult(0, "", "")


class ScriptedHosts:
    def __init__(self, host: ScriptedHost, cfg: dict[str, Any] | None = None):
        self._host, self.cfg = host, cfg or {}

    def get(self, hid):
        return {"id": hid, "hostname": "pi.lan", "port": 22, "exec_config": self.cfg}

    def host(self, hid):
        return self._host


GOOD = ("boot\tb1\nwritable\tyes\nfree_kb\t500000000\npython\t3.13\nmodules\tok\n")


def test_remote_hosts_problems_are_found_by_the_start_checks(ex):
    rx = dataclasses.replace(ex.s.receivers["OBS-R02"], id="PI-R1", host="pi", driver="sim", serial="SIM-0009")
    job = ex.s.get(manual(ex))
    req = dataclasses.replace(RecordingRequest.from_job(job, ex.s.receivers["OBS-R02"], ex.s.settings),
                              host="pi", driver="sim", serial="SIM-0009", simulated=True, format="cs16")
    host = ScriptedHost()
    b = SshBackend(ex.s, ScriptedHosts(host), "pi")

    def result(stdout: str = "", rc: int = 0, kind: str | None = None, err: str | None = None) -> dict[str, Any]:
        host.replies = {"free_kb": RunResult(rc, stdout, "", err, kind)}
        d = Diagnostics(ex.clock, "PI-R1")
        ok = b.preflight(req, rx, d)
        bad = [c for c in d.checks if c["ok"] is False]
        return {"ok": ok, "bad": bad[0] if bad else None}

    assert result(GOOD)["ok"]
    r = result(rc=255, kind="timeout", err="no answer within 60 s")
    assert r["bad"]["check"] == "connected" and "could not be reached over SSH" in r["bad"]["detail"]
    r = result(GOOD.replace("500000000", "1000"))
    assert r["bad"]["check"] == "storage" and r["bad"]["code"] == "disk_full"
    r = result(GOOD.replace("writable\tyes", "writable\tno\tmkdir: cannot create directory: Permission denied"))
    assert r["bad"]["code"] == "permission" and "Permission denied" in r["bad"]["detail"]
    r = result(GOOD.replace("modules\tok", "modules\tmissing\tModuleNotFoundError: No module named 'numpy'"))
    assert r["bad"]["code"] == "missing_python_module" and "numpy is not installed there" in r["bad"]["detail"]
    r = result(GOOD.replace("python\t3.13", "python\tmissing"))
    assert r["bad"]["check"] == "recorder installed" and r["bad"]["code"] == "missing_tool"

    # a reboot during the recording, an SSH drop, and a finished recorder look different to the monitor
    h = {"pid": 77, "dir": ".starlab/recordings/x", "boot_id": "b1", "host": "pi", "job_id": "x"}
    host.replies = {"state": RunResult(0, "boot\tb2\nstate\tstopped\n", "")}
    assert b.poll(h) == "rebooted"
    host.replies = {"state": RunResult(255, "", "", "no answer", "timeout")}
    assert b.poll(h) == "unreachable"
    host.replies = {"state": RunResult(0, "boot\tb1\nstate\tstopped\nresult\tyes\n", "")}
    assert b.poll(h) == "finished"

    real = dataclasses.replace(req, driver="rtlsdr", simulated=False, serial="00000001")
    d = Diagnostics(ex.clock, "PI-R1")
    assert not b.preflight(real, dataclasses.replace(rx, driver="rtlsdr"), d)        # tests never open real SDRs
    assert d.checks[-1]["code"] == "blocked" and "pytest" in d.checks[-1]["detail"]
    with pytest.raises(BackendError, match="never opens a physical SDR"):
        b.start(real)


def test_agent_hosts_are_refused_clearly_until_agents_exist(ex):
    rx = dataclasses.replace(ex.s.receivers["OBS-R02"], host="roof", driver="sim", serial="SIM-0009")
    hosts = ScriptedHosts(ScriptedHost(), {"backend": "agent"})
    b = Backends(ex.s, LocalBackend(ex.s, FakeRunnerHw()), hosts).for_receiver(rx)
    d = Diagnostics(ex.clock, rx.id)
    assert b.kind == "agent" and not b.preflight(None, rx, d)
    assert "receiver agents are not available" in d.checks[-1]["detail"]


def test_error_messages_name_the_receiver_and_what_to_do():
    cases = {
        "usb_claim_interface error -6": "OBS-R01 could not start because another process has claimed its USB interface.",
        "LIBUSB_ERROR_ACCESS": "OBS-R01 could not be opened: this user is not allowed to access its USB device.",
        "No matching devices found.": "OBS-R01 was not found on USB.",
        "cb transfer status: 5, canceling...": "OBS-R01 disappeared from USB during the recording.",
        "sh: 1: rtl_sdr: not found": "OBS-R01 cannot record on this host: a required program is not installed (rtl_sdr).",
        "Permission denied (publickey).": "OBS-R01's host refused Starlab's SSH key.",
        "ssh: connect to host pi port 22: Connection refused": "OBS-R01's host could not be reached over SSH.",
        "pi rebooted during the recording (boot id changed)": "OBS-R01's host restarted during the recording, which stopped it.",
        "sdrplay_api_Fail": "OBS-R01 could not start because the SDRplay API reports the device is in use or not responding.",
    }
    for text, want in cases.items():
        e = explain(text, "OBS-R01")
        assert e["message"] == want and e["action"], text
    assert explain("something new", "OBS-R01") == {"code": "unknown", "message": "OBS-R01: something new", "action": "",
                                                   "detail": "something new"}


# ------------------------------------------------------------------ a real SSH host (throwaway sshd, simulated SDR)
SSHD = shutil.which("sshd") or "/usr/sbin/sshd"


class RealClock:
    def __call__(self) -> dt.datetime:
        return dt.datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def pi(ex, tmp_path, monkeypatch):
    if not os.access(SSHD, os.X_OK) or not shutil.which("ssh"):
        pytest.skip("OpenSSH server/client not installed")
    from test_hosts import Sshd
    sshd = Sshd(tmp_path / "sshd")
    sshd.start()
    store = HostStore(ex.engine, tmp_path / "secrets", tmp_path / "run", clock=ex.s.clock)
    sshd.auth.write_text(store.client_key()["public_key"] + "\n")
    store.add({"id": "pi", "name": "Roof Pi", "hostname": "127.0.0.1", "port": sshd.port, "username": getpass.getuser(),
               "accept_fingerprints": [sshd.fingerprint]}, "owner")
    work = tmp_path / "remote-home" / "starlab"
    store.update("pi", {"revision": store.get("pi")["revision"],
                        "recording": {"python": sys.executable, "work_dir": str(work)}}, "owner")
    ex.s.inventory.create(rid="PI-SIM", name="Pi simulator", driver="sim", serial="SIM-0009", identity="serial",
                          usb_path=None, usb_vid=None, usb_pid=None, manufacturer=None, model=None,
                          config={"antenna": "DISCONE", "signal_path": "Discone > Pi"}, capabilities=SIM_CAPS,
                          actor="test", host="pi")
    ex.s.reload_receivers()
    monkeypatch.setitem(runner_mod.POLL_S, "ssh", 0.5)
    yield types.SimpleNamespace(sshd=sshd, store=store, work=work)
    sshd.stop()


def sim_job(ex: Any, start: dt.datetime, seconds: int) -> str:
    job = fix_dirs(rtl_job("pi-sim", rx="PI-SIM"), ex.tmp)
    job["receiver"].update(driver="sim", serial="SIM-0009")
    job["radio"].update(gain_db=None, gain_elements={"GAIN": 25}, sample_rate_hz=250_000,
                        center_frequency_hz=137_900_000 - 31_250)
    job["recording"].update(engine="simulator", format="cs16")
    job["satdump"] = {"enabled": False, "operation": "none"}
    job["postprocess"]["decode"] = False
    job["schedule"].update(start=start.isoformat(), end=(start + dt.timedelta(seconds=seconds)).isoformat(),
                           pre_roll_seconds=2, post_roll_seconds=2)
    res = ex.s.submit([job], "all_or_nothing", "test")["results"][0]
    assert res["ok"], res["errors"]
    return res["job_id"]


def test_a_remote_recording_over_ssh_survives_a_dropped_connection_and_is_copied_back_verified(ex, pi):
    real = RealClock()
    start = real() + dt.timedelta(seconds=16)
    ex.clock.now = real() - dt.timedelta(minutes=10)                   # book well ahead, then run in real time
    jid = sim_job(ex, start, 8)
    ex.s.clock = real
    r = Runner(ex.s, FakeRunnerHw(), hosts=pi.store)
    t = threading.Thread(target=r.execute, args=(ex.s.get(jid),))
    t.start()
    deadline = time.monotonic() + 60
    while ex.s.get(jid)["status"] != "recording" and time.monotonic() < deadline:
        time.sleep(0.2)
    assert ex.s.get(jid)["status"] == "recording", ex.s.get(jid)["status_reason"]
    assert ex.s.reservations.current()[0]["receiver_id"] == "PI-SIM"
    time.sleep(1)
    pi.sshd.stop()                                                     # the network drops mid-recording
    time.sleep(3)
    pi.sshd.start()
    t.join(90)
    job = ex.s.get(jid)
    assert job["status"] == "completed", (job["status_reason"], job["execution"])
    notes = " ".join(d["message"] for d in job["execution"]["diagnostics"])
    assert "lost contact with pi" in notes and "answers again" in notes
    cap = job["result"]["capture"]
    assert cap["simulated"] and cap["remote"]["host"] == "pi" and cap["transfer"]["verified"] == "size and SHA-256"
    raw = pathlib.Path(job["paths"]["raw"])
    assert raw.stat().st_size == cap["bytes"] == cap["samples"] * 4 and cap["validation"]["ok"]
    assert 7.5 <= cap["bytes"] / 4 / 250_000 <= 8.5                   # the samples cover the scheduled 8 s
    assert not (pi.work / "recordings" / jid).exists()                 # verified copy here; removed on the host
    assert "recorder log from pi" in pathlib.Path(job["paths"]["log"]).read_text()
    assert job["execution"]["backend"] == "ssh" and ex.s.reservations.current() == []


def test_a_remote_host_without_the_recording_dependencies_fails_at_the_start_check(ex, pi):
    pi.store.update("pi", {"revision": pi.store.get("pi")["revision"],
                           "recording": {"python": "/nonexistent/python3", "work_dir": str(pi.work)}}, "owner")
    real = RealClock()
    ex.clock.now = real() - dt.timedelta(minutes=10)
    jid = sim_job(ex, real() + dt.timedelta(seconds=30), 5)
    ex.s.clock = real
    Runner(ex.s, FakeRunnerHw(), hosts=pi.store, sleep=lambda s: None).execute(ex.s.get(jid))
    job = ex.s.get(jid)
    assert job["status"] == "failed" and "/nonexistent/python3 is not available on pi" in job["status_reason"]
    assert {c["check"]: c["ok"] for c in job["execution"]["checks"]}["connected"] is True


def test_the_live_recorder_never_opens_a_real_receiver_in_tests_or_development(ex, tmp_path):
    from aobs.scheduling.runner import LiveRunnerHardware
    hw = LiveRunnerHardware.__new__(LiveRunnerHardware)               # no coordinator needed for this check
    run_file = tmp_path / "run.json"
    run_file.write_text(json.dumps({"driver": "rtlsdr", "serial": "OBS-R02"}))
    with pytest.raises(RuntimeError, match="not opening OBS-R02: this is a test run"):
        hw.spawn([sys.executable, "-m", "aobs.scheduling.capture", str(run_file)], str(tmp_path / "log"))
    assert not (tmp_path / "log").exists()


def test_migration_7_adds_reservations_and_keeps_jobs_and_hosts(tmp_path):
    from sqlalchemy import text
    from aobs.db.migrate import current_version, migrate
    e = make_engine(f"sqlite:///{tmp_path / 'db.sqlite3'}")
    init_db(e)
    with e.begin() as conn:                                                 # wind back to schema 6
        conn.execute(text("DROP TABLE sched_reservations"))
        conn.execute(text("ALTER TABLE sched_jobs DROP COLUMN execution"))
        conn.execute(text("ALTER TABLE inv_hosts DROP COLUMN exec_config"))
        conn.execute(text("INSERT INTO sched_jobs (job_id, job_name, source, receiver_id, device_serial, status, executes,"
                          " reservation_start, reservation_end, record_start, record_end, spec, created_at, updated_at)"
                          " VALUES ('m1', 'old', 'manual', 'OBS-R02', 'OBS-R02', 'completed', 1, '2026-10-01 00:00:00',"
                          " '2026-10-01 00:10:00', '2026-10-01 00:01:00', '2026-10-01 00:09:00', '{}',"
                          " '2026-10-01 00:00:00', '2026-10-01 00:00:00')"))
        conn.execute(text("UPDATE schema_info SET value='6' WHERE key='schema_version'"))
    out = migrate(e)
    from aobs.db.schema import SCHEMA_VERSION
    assert out["steps"][0] == "7: receiver reservations and recording execution" and current_version(e) == SCHEMA_VERSION
    assert out["backup"]
    with e.connect() as conn:
        assert tuple(conn.execute(text("SELECT job_id, execution FROM sched_jobs")).one()) == ("m1", None)
        assert conn.execute(text("SELECT COUNT(*) FROM sched_reservations")).scalar() == 0
        assert "exec_config" in {r[1] for r in conn.execute(text("PRAGMA table_info(inv_hosts)"))}
    assert migrate(e)["steps"] == []
