from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import threading
import types
from typing import Any

import numpy as np
import pytest

from aobs.config import Config
from aobs.db import init_db, make_engine
from aobs.scheduling import calendar as schedcal
from aobs.scheduling import capture, postprocess
from aobs.scheduling.planner import Planner
from aobs.scheduling.runner import Runner
from aobs.scheduling.service import recordings, shadow_report
from aobs.scheduling.store import Scheduler

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 13, 16, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: dt.datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def sched_config(cfg: Config, tmp: pathlib.Path) -> Config:
    cfg.raw["antenna_switch"].update(receiver="OBS-R05", ports=[
        {"port": 1, "code": 0, "antenna": "V-DIPOLE"}, {"port": 8, "code": 7, "antenna": "MA-30-LOOP"}])
    cfg.raw["scheduler"] = {
        "enabled": True, "min_lead_seconds": 120, "max_duration_seconds": 14400, "max_horizon_days": 30,
        "preempt_cutoff_seconds": 180, "legacy_guard_seconds": 60, "legacy_import_days": 7,
        "allowed_output_roots": [str(tmp)], "default_output_root": str(tmp / "jobs"),
        "auto_output_root": str(tmp / "auto"), "reserve_free_gib": 1, "auto_horizon_hours": 48,
        "receivers": {
            "OBS-R01": {"driver": "rtlsdr", "serial": "OBS-R01", "antenna": "QFH-137", "signal_path": "QFH > R01",
                        "mode": "legacy", "legacy_unit": "observatory-record@OBS-R01.service", "legacy_kind": "record_py",
                        "legacy_output": str(tmp / "legacy-r01"),
                        "auto": {"enabled": True, "station_receiver": "OBS-R01", "norad_ids": [57166, 59051],
                                 "center_frequency_hz": 137500000, "sample_rate_hz": 2400000, "gain_db": 29.7,
                                 "format": "cu8"}},
            "OBS-R02": {"driver": "rtlsdr", "serial": "OBS-R02", "antenna": "DISCONE", "signal_path": "Discone > R02",
                        "mode": "broker",
                        "auto": {"enabled": True, "station_receiver": "OBS-R01", "norad_ids": [57166],
                                 "center_frequency_hz": 137500000, "sample_rate_hz": 2400000, "gain_db": 29.7,
                                 "format": "cu8"}},
            "OBS-R05": {"driver": "rtlsdr", "serial": "OBS-R05", "antenna": "ANTSW-01", "signal_path": "ANTSW-01 > R05",
                        "mode": "broker", "bias_tee_supported": True},
            "RSP1B": {"driver": "sdrplay", "serial": "240526D360", "antenna": "UNSPECIFIED", "signal_path": "? > RSP1B",
                      "mode": "broker", "bias_tee_supported": True},
        },
    }
    return cfg


@pytest.fixture
def env(tmp_config: Config, tmp_path: pathlib.Path) -> types.SimpleNamespace:
    cfg = sched_config(tmp_config, tmp_path)
    engine = make_engine(cfg.database_url)
    init_db(engine)
    clock = Clock()
    free = {"bytes": 500 * 10**9}
    present = {"OBS-R01", "OBS-R02", "OBS-R05", "240526D360"}
    s = Scheduler(cfg, engine, clock, disk_free=lambda _: free["bytes"], present=lambda rx: rx.serial in present,
                  satdump_pipelines=frozenset({"meteor_m2-x_lrpt", "noaa_apt"}))
    return types.SimpleNamespace(s=s, clock=clock, free=free, present=present, cfg=cfg, tmp=tmp_path)


def at(minutes: float) -> str:
    return (NOW + dt.timedelta(minutes=minutes)).isoformat()


def rtl_job(name: str = "meteor-test", rx: str = "OBS-R02", start: float = 60, end: float = 76, **over: Any) -> dict[str, Any]:
    job = {
        "job_name": name,
        "target": {"type": "satellite", "name": "METEOR-M2-4", "satellite": "METEOR-M2-4"},
        "schedule": {"start": at(start), "end": at(end), "timezone": "America/New_York",
                     "pre_roll_seconds": 60, "post_roll_seconds": 60},
        "receiver": {"receiver_id": rx, "driver": "rtlsdr", "serial": rx, "exclusive_lock": True},
        "radio": {"center_frequency_hz": 137500000, "target_frequency_hz": 137900000, "sample_rate_hz": 2400000,
                  "bandwidth_hz": None, "gain_mode": "manual", "gain_db": 33.8, "agc": False, "ppm_correction": 0,
                  "bias_tee": False, "direct_sampling": False, "offset_tuning": False, "iq_correction": False,
                  "dc_offset_correction": False, "doppler_tracking": False},
        "recording": {"enabled": True, "engine": "rtl_sdr", "format": "cu8", "output_directory": "",
                      "minimum_free_space_gb": 1},
        "satdump": {"enabled": True, "operation": "decode_after_recording", "pipeline": "meteor_m2-x_lrpt",
                    "input_type": "baseband", "baseband_format": "cu8", "sample_rate_hz": 2400000,
                    "frequency_hz": 137900000, "output_directory": "", "additional_parameters": {}},
        "postprocess": {"generate_waterfall": True, "generate_spectrum": True, "decode": True, "retain_raw_iq": True,
                        "checksum": True, "record_metadata": True},
        "conflict_policy": {"on_receiver_conflict": "reject", "on_storage_warning": "reject", "on_missing_device": "reject"},
    }
    for path, value in over.items():
        node = job
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
    return job


def fix_dirs(job: dict[str, Any], tmp: pathlib.Path) -> dict[str, Any]:
    job["recording"]["output_directory"] = str(tmp / "jobs" / "{job_name}")
    if job.get("satdump"):
        job["satdump"]["output_directory"] = str(tmp / "decoded" / "{job_name}")
    return job


def submit(env: Any, *jobs: dict[str, Any], mode: str = "all_or_nothing", dry: bool = False) -> dict[str, Any]:
    return env.s.submit([fix_dirs(copy.deepcopy(j), env.tmp) for j in jobs], mode, "test", dry)


def errors(result: dict[str, Any], i: int = 0) -> str:
    return " | ".join(result["results"][i]["errors"])


# ---------------------------------------------------------------- validation
def test_valid_job_is_stored_normalized_with_size_and_freq_shift(env):
    r = submit(env, rtl_job())
    entry = r["results"][0]
    assert entry["stored"], entry["errors"]
    res = entry["job"]["resolved"]
    assert res["device_serial"] == "OBS-R02"
    assert res["reservation_start"] == at(59) and res["reservation_end"] == at(77)
    assert res["estimated_bytes"] == 2_400_000 * 2 * 16 * 60
    assert res["satdump_freq_shift_hz"] == -400_000
    assert entry["job"]["radio"]["gain_db"] == 33.8          # never rewritten
    stored = env.s.get(entry["job_id"])
    assert stored["status"] == "scheduled" and stored["events"][0]["new_status"] == "scheduled"
    assert stored["paths"]["raw"].endswith("_OBS-R02_20261013T170000Z_137500000_2400000.cu8")


@pytest.mark.parametrize("over, needle", [
    ({"radio__gain_db": None}, "gain_mode manual needs radio.gain_db"),
    ({"radio__gain_db": 30.0}, "not an R820T gain step"),
    ({"receiver__device_index": 1}, "device_index is not accepted"),
    ({"receiver__receiver_id": "OBS-R09", "receiver__serial": None}, "not a known receiver"),
    ({"receiver__serial": "OBS-R01"}, "name different physical SDRs"),
    ({"radio__bias_tee": True}, "has no bias tee"),
    ({"radio__bias_tee": None}, "bias_tee must be explicitly"),
    ({"radio__target_frequency_hz": 139_000_000}, "usable half-band"),
    ({"radio__sample_rate_hz": 3_200_000}, "not supported by RTL-SDR"),
    ({"radio__iq_correction": True}, "not applied by rtl_sdr"),
    ({"radio__bandwidth_hz": 1_536_000}, "must be null for rtl_sdr"),
    ({"recording__format": "cs16"}, "not produced by rtlsdr"),
    ({"recording__engine": "soapysdr"}, "cannot drive OBS-R02"),
    ({"satdump__baseband_format": "cs16"}, "does not match the recording format"),
    ({"satdump__sample_rate_hz": 1_024_000}, "differs from the recorded sample rate"),
    ({"satdump__pipeline": "made_up"}, "not installed"),
    ({"satdump__operation": "live"}, "live is not supported"),
    ({"schedule__end": None}, "schedule.end or schedule.duration_seconds is required"),
    ({"schedule__start": at(1)}, "less than 120 s from now"),
    ({"job_name": "x"}, "job_name is required"),
    ({"receiver__exclusive_lock": False}, "exclusive_lock=false is not allowed"),
    ({"receiver__antenna": "V-DIPOLE"}, "hard-wired to DISCONE"),
])
def test_invalid_settings_are_rejected_with_a_reason(env, over, needle):
    r = submit(env, rtl_job(**over))
    assert not r["results"][0]["stored"]
    assert needle in errors(r)


def test_duration_instead_of_end_and_naive_local_time(env):
    job = rtl_job(schedule__end=None, schedule__duration_seconds=600,
                  schedule__start=(NOW + dt.timedelta(hours=1)).astimezone(dt.timezone(dt.timedelta(hours=-4)))
                  .replace(tzinfo=None).isoformat())
    r = submit(env, job)
    assert r["results"][0]["stored"], errors(r)
    assert r["results"][0]["job"]["resolved"]["record_end"] == at(70)


def test_sdrplay_uses_gain_reduction_not_gain_db(env):
    job = rtl_job(rx="RSP1B", receiver__serial="240526D360", receiver__driver="sdrplay", recording__engine="soapysdr",
                  recording__format="cs16", satdump__baseband_format="cs16", radio__sample_rate_hz=2_000_000,
                  satdump__sample_rate_hz=2_000_000, radio__bandwidth_hz=1_536_000)
    assert "gain_db does not apply to SDRplay" in errors(submit(env, job))
    job["radio"]["gain_db"] = None
    assert "IFGR" in errors(submit(env, job))
    job["radio"]["gain_elements"] = {"IFGR": 50, "RFGR": 12}
    assert "RFGR 12 must be an integer 0-9" in errors(submit(env, job))
    job["radio"]["gain_elements"]["RFGR"] = 8
    job["radio"]["device_settings"] = {"rfnotch_ctrl": True, "tuner_hack": True}
    assert "device_settings.tuner_hack is not allowed" in errors(submit(env, job))
    job["radio"]["device_settings"] = {"rfnotch_ctrl": True}
    job["radio"]["sample_rate_hz"] = 1_500_000
    assert "not an RSP1B rate" in errors(submit(env, job))
    job["radio"]["sample_rate_hz"] = 2_000_000
    r = submit(env, job)
    assert r["results"][0]["stored"], errors(r)
    assert r["results"][0]["warnings"]                       # antenna undocumented
    assert r["results"][0]["job"]["resolved"]["estimated_bytes"] == 2_000_000 * 4 * 16 * 60


def test_r05_job_must_name_a_switch_antenna(env):
    job = rtl_job(rx="OBS-R05", satdump={"enabled": False, "operation": "none"},
                  postprocess={"decode": False}, target={"type": "frequency", "name": "noaa-wx"},
                  radio__target_frequency_hz=137_600_000)
    assert "must name a port antenna" in errors(submit(env, job))
    job["receiver"]["antenna"] = "v-dipole"
    r = submit(env, job)
    assert r["results"][0]["stored"], errors(r)
    assert r["results"][0]["job"]["resolved"]["switch_port"] == 1


def test_missing_device_and_storage(env):
    env.present.discard("OBS-R02")
    assert "not connected" in errors(submit(env, rtl_job()))
    env.present.add("OBS-R02")
    env.free["bytes"] = 2 * 10**9
    assert "not enough storage" in errors(submit(env, rtl_job()))
    r = submit(env, rtl_job(conflict_policy__on_storage_warning="warn"))
    assert r["results"][0]["stored"] and "not enough storage" in r["results"][0]["warnings"][0]


def test_storage_counts_bytes_already_committed(env):
    env.free["bytes"] = int(1024**3 + 1e9 + 1.5 * 4.608e9)   # reserve + job minimum + 1.5 recordings
    assert submit(env, rtl_job("job-a", rx="OBS-R01", start=60, end=76))["results"][0]["stored"]
    assert "already committed" in errors(submit(env, rtl_job("job-b", rx="OBS-R02", start=60, end=76)))


# ---------------------------------------------------------------- conflicts
def test_spec_example_conflict_is_rejected_and_other_receivers_are_free(env):
    first = submit(env, rtl_job("existing", start=51, end=71))          # reservation 14:50-15:12 analogue
    assert first["results"][0]["stored"]
    r = submit(env, rtl_job("late", start=66, end=79))
    assert "conflict with manually scheduled existing" in errors(r)
    assert first["results"][0]["job_id"] in errors(r)
    assert submit(env, rtl_job("parallel", rx="OBS-R05", receiver__antenna="V-DIPOLE", start=66, end=79))["results"][0]["stored"]


def test_back_to_back_reservations_do_not_conflict(env):
    assert submit(env, rtl_job("one", start=60, end=70))["results"][0]["stored"]
    assert submit(env, rtl_job("two", start=72, end=80))["results"][0]["stored"]   # 70+1 == 72-1


def test_all_or_nothing_and_allow_partial(env):
    good, bad = rtl_job("good"), rtl_job("bad", radio__gain_db=None)
    r = submit(env, good, bad)
    assert r["stored"] == 0 and "another job in this all_or_nothing request" in errors(r, 0)
    assert env.s.jobs() == []
    r = submit(env, good, bad, mode="allow_partial")
    assert r["stored"] == 1 and r["results"][0]["stored"] and not r["results"][1]["stored"]


def test_jobs_in_one_request_conflict_with_each_other(env):
    r = submit(env, rtl_job("job-a"), rtl_job("job-b", start=70, end=80))
    assert "in this same request" in errors(r, 1)


def test_dry_run_stores_nothing(env):
    r = submit(env, rtl_job(), dry=True)
    assert r["results"][0]["ok"] and not r["results"][0]["stored"] and env.s.jobs() == []


def test_concurrent_requests_cannot_double_book(env):
    results: list[dict[str, Any]] = []
    barrier = threading.Barrier(8)

    def go(i: int) -> None:
        barrier.wait()
        results.append(submit(env, rtl_job(f"race-{i}")))
    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r["stored"] for r in results) == 1
    assert len(env.s.jobs(receiver="OBS-R02")) == 1


# ---------------------------------------------------------------- planner + preemption
def window(start_min: float, end_min: float, name: str = "METEOR M2-3", norad: int = 57166) -> dict[str, Any]:
    s, e = NOW + dt.timedelta(minutes=start_min), NOW + dt.timedelta(minutes=end_min)
    return {"start": s, "end": e, "satellites": [{"name": name, "norad_id": norad, "rise": s.isoformat(),
                                                   "set": e.isoformat(), "max_elevation_deg": 40.0}]}


def planner(env: Any, windows: list[dict[str, Any]]) -> Planner:
    return Planner(env.s, predictor=lambda rx, hours: copy.deepcopy(windows))


def test_legacy_windows_are_imported_locked_and_block_manual_jobs(env):
    planner(env, [window(60, 75)]).plan()
    legacy = env.s.jobs(receiver="OBS-R01", source="legacy")
    assert len(legacy) == 1 and legacy[0]["executes"] is False
    assert legacy[0]["reservation_start"] < at(60)             # guard added
    r = submit(env, rtl_job(rx="OBS-R01", start=70, end=80))
    assert "cannot yet be told to skip a pass" in errors(r)


def test_manual_job_preempts_broker_auto_job_auditably(env):
    planner(env, [window(60, 75)]).plan()
    auto = env.s.jobs(receiver="OBS-R02", source="auto")[0]
    assert auto["status"] == "scheduled" and auto["executes"]
    r = submit(env, rtl_job("priority", start=70, end=80))
    entry = r["results"][0]
    assert entry["stored"] and entry["preempts"][0]["job_id"] == auto["job_id"]
    after = env.s.get(auto["job_id"])
    assert after["status"] == "preempted" and after["preempted_by"] == entry["job_id"]
    assert "still scheduled on OBS-R01" in after["status_reason"]         # the legacy R01 window covers it
    assert [e["new_status"] for e in after["events"]] == ["scheduled", "preempted"]
    note = next(n for n in env.s.notifications() if n["kind"] == "preempted")
    assert "priority" in note["text"]
    # cancelling the manual job brings the automatic pass back on the next plan
    env.s.cancel(entry["job_id"], "changed my mind", "test")
    planner(env, [window(60, 75)]).plan()
    assert env.s.get(auto["job_id"])["status"] == "scheduled"


def test_auto_job_inside_cutoff_is_not_preempted(env):
    planner(env, [window(2.5, 20)]).plan()
    env.clock.now = NOW + dt.timedelta(seconds=10)
    r = submit(env, rtl_job("too-late", start=12, end=30))
    assert "starts too soon to be preempted" in errors(r)


def test_active_recording_is_never_interrupted(env):
    jid = submit(env, rtl_job("running"))["results"][0]["job_id"]
    env.s.transition(jid, "recording", "test")
    r = submit(env, rtl_job("intruder", start=70, end=90))
    assert "active recording" in errors(r)
    with pytest.raises(Exception, match="never interrupted"):
        env.s.cancel(jid, "x", "test")


def test_planner_skips_passes_on_receivers_held_by_manual_jobs(env):
    submit(env, rtl_job("hold", start=60, end=80))
    planner(env, [window(70, 85)]).plan()
    auto = env.s.jobs(receiver="OBS-R02", source="auto")[0]
    assert auto["status"] == "skipped" and "hold" in auto["status_reason"]
    assert env.s.notifications()[-1]["kind"] == "skipped"


def test_planner_is_idempotent_and_tracks_small_prediction_drift(env):
    p = planner(env, [window(60, 75)])
    p.plan()
    p.predictor = lambda rx, hours: [window(60.05, 75.05)]
    p.plan()
    rows = env.s.jobs(receiver="OBS-R02", source="auto")
    assert len(rows) == 1 and rows[0]["record_start"] == at(60.05)


def test_mode_change_retires_legacy_rows_and_vanished_passes(env):
    planner(env, [window(60, 75), window(160, 175)]).plan()
    assert len(env.s.jobs(receiver="OBS-R01", source="legacy", status=["scheduled"])) == 2
    env.s.receivers["OBS-R01"] = env.s.receivers["OBS-R01"].__class__(**{**env.s.receivers["OBS-R01"].__dict__, "mode": "broker"})
    planner(env, [window(60, 75)]).plan()
    assert {j["status"] for j in env.s.jobs(receiver="OBS-R01", source="legacy")} == {"cancelled"}
    autos = env.s.jobs(receiver="OBS-R01", source="auto")
    assert [a["status"] for a in autos] == ["scheduled"]


def test_shadow_mode_plans_without_executing_and_reports_agreement(env):
    rx = env.s.receivers["OBS-R01"]
    env.s.receivers["OBS-R01"] = rx.__class__(**{**rx.__dict__, "mode": "shadow"})
    planner(env, [window(60, 75)]).plan()
    shadow = env.s.jobs(receiver="OBS-R01", source="auto")
    assert shadow[0]["status"] == "shadow" and not shadow[0]["executes"]
    assert shadow_report(env.s)["OBS-R01"]["pending"] == 1
    out = env.tmp / "legacy-r01"
    out.mkdir()
    (out / "OBS-R01_a.json").write_text(json.dumps({"started_at": at(60.05), "finished_at": at(70), "status": "recorded"}))
    (out / "OBS-R01_b.json").write_text(json.dumps({"started_at": at(70), "finished_at": at(75), "status": "recorded"}))
    env.clock.now = NOW + dt.timedelta(minutes=90)
    planner(env, []).reconcile_legacy()
    rep = shadow_report(env.s, min_compared=1)["OBS-R01"]
    assert rep["agrees"] and rep["pairs"][0]["start_diff_s"] == 3.0 and rep["pairs"][0]["end_diff_s"] == 0.0


def test_legacy_reconciliation_marks_recorded_or_missed(env):
    planner(env, [window(60, 75), window(160, 175)]).plan()
    out = env.tmp / "legacy-r01"
    out.mkdir()
    (out / "OBS-R01_20261013T170000.000000Z.json").write_text(json.dumps(
        {"started_at": at(59), "status": "recorded", "raw_file": "OBS-R01_x.cu8", "raw_bytes": 5}))
    env.clock.now = NOW + dt.timedelta(minutes=200)
    planner(env, []).reconcile_legacy()
    rows = {j["record_start"]: j for j in env.s.jobs(receiver="OBS-R01", source="legacy")}
    assert rows[at(60)]["status"] == "recorded" and rows[at(160)]["status"] == "missed"


# ---------------------------------------------------------------- runner
class FakeHandle:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> int:
        return 0


class FakeRunnerHw:
    def __init__(self, ok: bool = True) -> None:
        self.ok, self.calls, self.busy_with = ok, [], []

    def present(self, rx): return True
    def busy(self, rx): return self.busy_with
    def coordinator_reserve(self, rx, owner, ttl, purpose):
        self.calls.append(("reserve", rx.id))
        return "tok"

    def coordinator_release(self, rx, token): self.calls.append(("release", rx.id))
    def alive(self, pid, job_id): return False

    def spawn(self, argv, log):
        run = json.loads(pathlib.Path(argv[-1]).read_text())
        self.calls.append(("spawn", run["serial"]))
        raw = pathlib.Path(run["raw"])
        raw.parent.mkdir(parents=True, exist_ok=True)
        if self.ok:
            iq = (np.random.default_rng(0).normal(0, 20, 2 * 2048 * 8 * 40) + 127).clip(0, 255).astype(np.uint8)
            raw.write_bytes(iq.tobytes())
        pathlib.Path(run["result"]).write_text(json.dumps(
            {"status": "recorded" if self.ok else "failed", "bytes": raw.stat().st_size if self.ok else 0,
             "error": None if self.ok else "rtl_sdr exit 1", "actual_start": run["record_start"]}))
        return FakeHandle(4242)


class InlineHandle:
    pid = 999999

    def poll(self) -> int:
        return 0


def inline_processor(env: Any) -> Any:
    """A Processor whose workers run in-process (same code as the real subprocess)."""
    from aobs.scheduling import procworker
    from aobs.scheduling.processing import Processor

    signals: list[Any] = []

    def spawn(argv: list[str], log: str) -> InlineHandle:
        assert argv[:6] == ["nice", "-n", "19", "ionice", "-c", "3"]
        procworker.main(["procworker", argv[-1]])
        pathlib.Path(log).write_text("inline")
        return InlineHandle()
    proc = Processor(env.s, spawn=spawn, alive=lambda pid, aid: False,
                     signal_group=lambda pid, sig: signals.append(sig))
    proc.signals = signals
    return proc


def drain(proc: Any, steps: int = 20) -> None:
    for _ in range(steps):
        proc.step()


def run_job(env: Any, hw: FakeRunnerHw, job: dict[str, Any], **kw: Any) -> tuple[str, Runner]:
    jid = submit(env, job)["results"][0]["job_id"]
    env.clock.now = NOW + dt.timedelta(minutes=59, seconds=30)
    proc = kw.pop("processor", None) or inline_processor(env)
    runner = Runner(env.s, hw, sleep=lambda s: None, processor=proc, **kw)
    runner.proc = proc
    runner.tick(threaded=False)
    return jid, runner


def test_runner_records_and_post_processes(env):
    hw = FakeRunnerHw()
    jid, runner = run_job(env, hw, rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    assert env.s.get(jid)["status"] == "processing"
    assert hw.calls == [("reserve", "OBS-R02"), ("spawn", "OBS-R02"), ("release", "OBS-R02")]
    env.clock.now = NOW + dt.timedelta(minutes=80)          # the recording window is over
    drain(runner.proc)
    done = env.s.get(jid)
    assert done["status"] == "completed", done["status_reason"]
    assert [e["new_status"] for e in done["events"]] == ["scheduled", "preparing", "recording", "recorded",
                                                          "processing", "completed"]
    assert env.s.notifications()[-1]["kind"] == "completed"


def test_runner_refuses_a_device_someone_else_holds(env):
    hw = FakeRunnerHw()
    hw.busy_with = ["sdrpp_server[999]"]
    jid, _ = run_job(env, hw, rtl_job())
    job = env.s.get(jid)
    assert job["status"] == "failed" and "sdrpp_server" in job["status_reason"]
    assert "another program is using it" in job["status_reason"]
    assert hw.calls == []            # checked before any lock is taken (brief 7: checks 1-6, then reserve)
    assert env.s.reservations.current() == []


def test_runner_reports_failed_capture(env):
    jid, _ = run_job(env, FakeRunnerHw(ok=False), rtl_job())
    assert env.s.get(jid)["status"] == "failed"
    assert "failed" in env.s.notifications()[-1]["text"]


def test_runner_uses_broker_lease_for_switched_receiver(env):
    taken: list[Any] = []
    job = rtl_job(rx="OBS-R05", receiver__antenna="MA-30-LOOP", satdump={"enabled": False},
                  postprocess={"decode": False}, target={"type": "frequency", "name": "x"})
    jid, _ = run_job(env, FakeRunnerHw(), job,
                     take_switched=lambda rx, j, port, until: taken.append(("take", port)) or "L",
                     release_switched=lambda tok: taken.append(("release", tok)))
    assert taken == [("take", 8), ("release", "L")]
    assert env.s.get(jid)["status"] == "processing"


def test_runner_marks_reservations_missed_while_down(env):
    jid = submit(env, rtl_job())["results"][0]["job_id"]
    env.clock.now = NOW + dt.timedelta(minutes=90)
    Runner(env.s, FakeRunnerHw(), sleep=lambda s: None).tick(threaded=False)
    assert env.s.get(jid)["status"] == "missed"


def test_runner_recover_adopts_live_capture(env):
    jid = submit(env, rtl_job())["results"][0]["job_id"]
    env.s.transition(jid, "preparing", "")
    env.s.transition(jid, "recording", "", pid=777)
    hw = FakeRunnerHw()
    state = {"alive": 3}

    def alive(pid, job_id):
        state["alive"] -= 1
        return pid == 777 and state["alive"] > 0
    hw.alive = alive
    runner = Runner(env.s, hw, sleep=lambda s: None)
    runner.recover()
    runner._threads[jid].join(5)
    assert env.s.get(jid)["status"] == "failed"   # adopted; ended without a result file
    assert "without writing a result" in env.s.get(jid)["status_reason"]


# ---------------------------------------------------------------- broker interplay
def test_agent_leases_yield_to_scheduled_jobs(env):
    from aobs.coordination.broker import Broker, BrokerError
    from test_broker import FakeHw

    env.cfg.raw["broker"] = {"receivers": ["OBS-R05"]}
    hw = FakeHw()
    broker = Broker(env.cfg, hw, env.clock)
    broker.reservation_guard = env.s.next_reservation
    submit(env, rtl_job("r05", rx="OBS-R05", receiver__antenna="V-DIPOLE", start=20, end=30,
                        satdump={"enabled": False}, postprocess={"decode": False}, target={"type": "frequency", "name": "x"}))
    lease = broker.lease("agent", ttl_s=3600)
    assert dt.datetime.fromisoformat(lease["expires_at"]) <= NOW + dt.timedelta(minutes=18, seconds=30)
    env.clock.now = NOW + dt.timedelta(minutes=18, seconds=45)
    broker.release(lease["token"])
    with pytest.raises(BrokerError, match="reserved for a scheduled recording"):
        broker.lease("agent")
    tok = broker.take_for_job("OBS-R05", "m1", 1, NOW + dt.timedelta(minutes=31))
    assert tok and broker.state()["leases"]["OBS-R05"]["owner"] == "scheduler:m1"


def test_scheduler_takes_r05_from_an_agent_lease(env):
    from aobs.coordination.broker import Broker
    from test_broker import FakeHw

    env.cfg.raw["broker"] = {"receivers": ["OBS-R05"]}
    broker = Broker(env.cfg, FakeHw(), env.clock)
    broker.lease("agent", ttl_s=600)
    broker.take_for_job("OBS-R05", "m2", 8, NOW + dt.timedelta(minutes=10))
    assert broker.state()["leases"]["OBS-R05"]["owner"] == "scheduler:m2"


# ---------------------------------------------------------------- capture, post-processing, calendar, API
def test_capture_process_runs_rtl_sdr_by_serial(tmp_path, monkeypatch):
    fake = tmp_path / "rtl_sdr"
    # stands in for rtl_sdr: streams zeros at roughly the requested rate until terminated
    fake.write_text('#!/usr/bin/env python3\nimport sys, time, pathlib, signal\n'
                    'pathlib.Path(sys.argv[0]).with_name("args").write_text(" ".join(sys.argv[1:]))\n'
                    'signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n'
                    'rate = int(sys.argv[sys.argv.index("-s") + 1])\n'
                    'with open(sys.argv[-1], "wb") as f:\n'
                    '    t0 = time.monotonic(); sent = 0\n'
                    '    while True:\n'
                    '        want = int((time.monotonic() - t0 + 0.05) * rate * 2)\n'
                    '        f.write(bytes(want - sent)); f.flush(); sent = want; time.sleep(0.01)\n')
    fake.chmod(0o755)
    now = dt.datetime.now(UTC)
    run = {"job_id": "m1", "driver": "rtlsdr", "serial": "OBS-R02", "format": "cu8", "rtl_sdr_path": str(fake),
           "radio": {"center_frequency_hz": 137500000, "sample_rate_hz": 240000 * 4, "gain_mode": "manual",
                     "gain_db": 33.8, "ppm_correction": 0},
           "record_start": now.isoformat(), "record_end": (now + dt.timedelta(seconds=1)).isoformat(),
           "raw": str(tmp_path / "out" / "x.cu8"), "result": str(tmp_path / "out" / "x.cu8.result.json")}
    (tmp_path / "run.json").write_text(json.dumps(run))
    assert capture.main(["capture", str(tmp_path / "run.json")]) == 0
    res = json.loads((tmp_path / "out" / "x.cu8.result.json").read_text())
    assert res["status"] == "recorded" and (tmp_path / "out" / "x.cu8").exists()
    args = (tmp_path / "args").read_text().split()
    assert args[:2] == ["-d", "OBS-R02"] and "-g" in args and args[args.index("-g") + 1] == "33.8"


def test_postprocess_plots_checksum_and_sidecar(env):
    hw = FakeRunnerHw()
    job = rtl_job(satdump={"enabled": False}, postprocess={"decode": False, "generate_waterfall": True,
                                                            "generate_spectrum": True})
    jid, runner = run_job(env, hw, job)
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    done = env.s.get(jid)
    assert done["status"] == "completed", done["status_reason"]
    side = json.loads(pathlib.Path(done["paths"]["sidecar"]).read_text())
    for key in ("job_id", "receiver_serial", "center_frequency_hz", "gain_db", "bias_tee", "recording_filename",
                "actual_recording_start", "satdump_pipeline", "signal_path"):
        assert key in side
    pp = done["result"]["postprocess"]
    assert len(pp["sha256"]) == 64 and "overload" in pp and pp["integrity"]["consistent"] is not None
    kinds = {a["kind"] for a in runner.proc.artifacts(jid)}
    assert {"raw_iq", "metadata", "waterfall", "spectrum", "diagnostics", "log"} <= kinds
    found = recordings(env.s, receiver="OBS-R02", processor=runner.proc)
    assert found[0]["artifact_counts"]["waterfall"] == 1 and found[0]["attempts"][0]["origin"] == "post-recording"


def test_reprocessing_keeps_every_attempt(env):
    hw = FakeRunnerHw()
    jid, runner = run_job(env, hw, rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    p = runner.proc
    a1 = p.submit(jid, "analyze", {"tasks": ["waterfall"], "nfft": 4096, "center_hz": 137_900_000, "span_hz": 200_000},
                  "slack", "owner")
    a2 = p.submit(jid, "analyze", {"tasks": ["spectrum", "stats"], "nfft": 1024, "start_s": 0.01, "end_s": 0.2},
                  "slack", "owner")
    drain(p)
    atts = p.attempts(jid)
    assert len(atts) == 3 and all(a["status"] == "completed" for a in atts)
    assert a1["output_dir"] != a2["output_dir"]
    w1 = p.artifacts(jid, kind="waterfall", attempt_id=a1["attempt_id"])
    assert len(w1) == 1 and "137.8000-138.0000 MHz" in w1[0]["label"]
    assert len(p.artifacts(jid, kind="spectrum")) == 1 and len(p.artifacts(jid, kind="waterfall")) == 1
    assert p.attempt(a2["attempt_id"])["resolved"]["fft_size"] == 1024
    path, mime = p.artifact_file(w1[0]["artifact_id"])
    assert mime == "image/png" and path.is_file()
    raw = p.artifacts(jid, kind="raw_iq")[0]
    with pytest.raises(Exception, match="too large to fetch"):
        p.artifact_file(raw["artifact_id"])
    notes = [n for n in env.s.notifications() if n["kind"].startswith("attempt_")]
    assert len(notes) == 2 and "/radio-results" in notes[0]["text"]


@pytest.mark.parametrize("kind, params, needle", [
    ("decode", {"pipeline": "noaa_apt", "shift_method": "bogus"}, "shift_method must be"),
    ("decode", {"pipeline": "nope"}, "not installed"),
    ("decode", {"pipeline": "noaa_apt", "target_frequency_hz": 138_600_000}, "not fully inside"),
    ("decode", {"pipeline": "noaa_apt", "target_frequency_hz": 137_900_000, "frequency_offset_hz": 1}, "disagree"),
    ("decode", {"pipeline": "noaa_apt", "frequency_offset_hz": 400_000, "shift_method": "none"}, "wrong frequency"),
    ("decode", {"pipeline": "noaa_apt", "baseband_format": "cs16"}, "does not match the recording"),
    ("decode", {"pipeline": "noaa_apt", "output_sample_rate_hz": 700_000}, "must divide"),
    ("decode", {"pipeline": "noaa_apt", "additional_parameters": {"samplerate": 1}}, "not allowed"),
    ("decode", {"pipeline": "noaa_apt", "rm": "-rf"}, "unknown decode parameters"),
    ("analyze", {"nfft": 1000}, "power of two"),
    ("analyze", {"center_hz": 137_900_000}, "zoom needs both"),
    ("analyze", {"center_hz": 139_000_000, "span_hz": 100_000}, "outside the recorded band"),
    ("analyze", {"tasks": ["mine_bitcoin"]}, "analysis must be among"),
    ("analyze", {"start_s": 5, "end_s": 1}, "end_s must be after"),
])
def test_processing_parameters_are_validated(env, kind, params, needle):
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    with pytest.raises(Exception, match=needle):
        runner.proc.submit(jid, kind, params)


def test_processing_waits_for_and_pauses_around_recordings(env):
    import signal as sig
    # another recording starts 60 s from "now": processing must not start
    submit(env, rtl_job("soon", rx="OBS-R05", receiver__antenna="V-DIPOLE", start=61, end=70,
                        satdump={"enabled": False}, postprocess={"decode": False}, target={"type": "frequency", "name": "x"}))
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    p = runner.proc
    drain(p, 3)
    assert {a["status"] for a in p.attempts(jid)} == {"queued"}
    env.clock.now = NOW + dt.timedelta(minutes=80)

    class Slow:
        pid = 4321
        done = False

        def poll(self):
            return 0 if self.done else None
    slow = Slow()
    p._spawn = lambda argv, log: slow
    p.step()
    assert p.attempts(jid)[0]["status"] == "running"
    other = submit(env, rtl_job("other", rx="OBS-R05", receiver__antenna="V-DIPOLE", start=84, end=90,
                                satdump={"enabled": False}, postprocess={"decode": False}, target={"type": "frequency", "name": "x"}))
    assert other["results"][0]["stored"], errors(other)
    env.clock.now = NOW + dt.timedelta(minutes=83)     # inside the processing gate of that recording
    p.step()
    assert p.signals == [sig.SIGSTOP] and p.attempts(jid)[0]["status"] == "paused"
    env.clock.now = NOW + dt.timedelta(minutes=95)
    p.step()
    assert p.signals == [sig.SIGSTOP, sig.SIGCONT] and p.attempts(jid)[0]["status"] == "running"


def test_decode_attempt_runs_satdump_with_requested_parameters(env, tmp_path, monkeypatch):
    import numpy as np
    from PIL import Image
    img = tmp_path / "real.png"
    Image.fromarray((np.random.default_rng(1).random((80, 80)) * 255).astype("uint8")).save(img)
    fake = tmp_path / "satdump"
    # exits 1 like the real SatDump 1.2.2 build, even though it wrote frames and an image
    fake.write_text(f'#!/bin/sh\nmkdir -p "$4/MSU-MR"\necho "$@" > "$4/args.txt"\ncp {img} "$4/MSU-MR/rgb.png"\n'
                    'printf x > "$4/MSU-MR/blank.png"\nhead -c 2048 /dev/zero > "$4/meteor_m2-x_lrpt.cadu"\n'
                    'echo "SNR : 7.5dB SYNCED"\nexit 1\n')
    fake.chmod(0o755)
    env.s.settings["satdump_path"] = str(fake)
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    a = runner.proc.submit(jid, "decode", {"pipeline": "meteor_m2-x_lrpt", "search_offsets_hz": [0, 2000],
                                            "additional_parameters": {"dc_block": True}, "start_s": 0.0, "end_s": 0.1})
    drain(runner.proc)
    done = runner.proc.attempt(a["attempt_id"])
    assert done["status"] == "completed", done["status_reason"]
    args = next(pathlib.Path(done["output_dir"]).rglob("args.txt")).read_text().split()
    # target 137.9 MHz from the recording's metadata -> internal shift of +400 kHz, 2.4 MS/s / 4 -> 600 kS/s cf32
    assert args[:2] == ["meteor_m2-x_lrpt", "baseband"] and "--freq_shift" not in args and "--dc_block" in args
    assert args[args.index("--samplerate") + 1] == "600000" and args[args.index("--baseband_format") + 1] == "cf32"
    assert ".channel" in args[2] and not pathlib.Path(args[2]).exists()     # intermediates removed afterwards
    assert done["resolved"]["frequency_offset_hz"] == 400000 and done["resolved"]["shift_method"] == "internal"
    res = done["result"]["inputs"][0]
    assert res["decoded"] and res["return_code"] == 1 and res["cadu_frames"] == 2 and res["snr_db_max"] == 7.5
    assert [s["residual_offset_hz"] for s in res["search"]] == [0, 2000]
    assert res["images"] == 1 and res["images_blank"] == 1          # an unreadable/blank PNG is not a decode
    assert [x["kind"] for x in done["artifacts"]].count("image") == 4    # both search runs keep their outputs


def test_meteor_decodes_default_to_a_fine_frequency_search(env):
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    a = runner.proc.submit(jid, "decode", {})
    assert a["resolved"]["search_offsets_hz"] == [0, -1000, 1000, -2000, 2000, -3000, 3000, -4000, 4000, -5000, 5000,
                                                  -6000, 6000]
    b = runner.proc.submit(jid, "decode", {"search_offsets_hz": [0]})
    assert b["resolved"]["search_offsets_hz"] == [0]
    with pytest.raises(Exception, match="search_offsets_hz must be"):
        runner.proc.submit(jid, "decode", {"search_offsets_hz": [0, 50_000]})


def test_satdump_command_is_built_from_the_request(env):
    sd = rtl_job()["satdump"]
    sd["additional_parameters"] = {"dc_block": True, "fill_missing": False}
    cmd = postprocess.satdump_command({"satdump_path": "/usr/bin/satdump"}, sd, "/d/x.cu8", "/d/out", -400000)
    assert cmd[3:12] == ["/usr/bin/satdump", "meteor_m2-x_lrpt", "baseband", "/d/x.cu8", "/d/out", "--samplerate",
                         "2400000", "--baseband_format", "cu8"]
    assert "--dc_block" in cmd and "--fill_missing" not in cmd and cmd[cmd.index("--freq_shift") + 1] == "-400000"


def test_calendar_sync_publishes_and_is_idempotent(env):
    planner(env, [window(60, 75)]).plan()
    submit(env, rtl_job("cal", start=70, end=80))

    class Client:
        def __init__(self): self.inserted, self.updated = [], []
        def insert_event(self, cal, body): self.inserted.append(body)
        def update_event(self, cal, eid, body): self.updated.append(eid)
    c = Client()
    first = schedcal.sync(env.s, c, "cal-id")
    # R02 auto (now preempted) + manual job; the R01 legacy row is not published (publish_legacy_to_calendar default true here)
    assert first["create"] == len(c.inserted) >= 2
    titles = " ".join(b["summary"] for b in c.inserted)
    assert "PREEMPTED" in titles and "SCHEDULED" in titles
    auto = next(b for b in c.inserted if "PREEMPTED" in b["summary"])
    assert auto["summary"].endswith("· max 40°") and "max elevation 40.0°" in auto["description"]
    assert not auto["summary"].startswith("⚠️")
    assert schedcal.sync(env.s, c, "cal-id")["noop"] == first["create"]


def test_calendar_title_marks_high_passes(env):
    planner(env, [window(60, 75)]).plan()
    job = next(j for j in env.s.jobs(limit=100) if (j["spec"].get("target") or {}).get("passes"))
    for el, marked in ((55.0, False), (55.4, True), (78.0, True)):
        for x in job["spec"]["target"]["passes"]:
            x["max_elevation_deg"] = el
        title = schedcal.render(job, "America/New_York")["summary"]
        assert title.startswith("⚠️ ") is marked, (el, title)


def test_api_schedule_capabilities_and_notifications(env):
    import socket
    import time
    import urllib.error
    import urllib.request

    import uvicorn

    from aobs.coordination.api import create_app
    from aobs.coordination.broker import Broker
    from aobs.scheduling.service import SchedulerService
    from test_broker import FakeHw

    env.cfg.raw["broker"] = {"receivers": ["OBS-R05"]}
    svc = SchedulerService(env.s, planner(env, []), Runner(env.s, FakeRunnerHw()))
    svc.start = lambda: None
    app = create_app(Broker(env.cfg, FakeHw(), env.clock), "t", scheduler=svc)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)

    def call(method: str, path: str, body: Any = None, token: str = "t") -> tuple[int, Any]:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")
    try:
        code, caps = call("GET", "/v1/capabilities")
        assert code == 200
        assert {r["receiver_id"] for r in caps["receivers"]} == {"OBS-R01", "OBS-R02", "OBS-R05", "RSP1B"}
        rsp = next(r for r in caps["receivers"] if r["receiver_id"] == "RSP1B")
        assert rsp["bandwidth_hz_values"] and rsp["formats"] == ["cs16", "cf32"]
        code, out = call("POST", "/v1/schedule", {"jobs": [fix_dirs(rtl_job(), env.tmp)]})
        assert code == 200 and out["stored"] == 1
        jid = out["results"][0]["job_id"]
        assert call("GET", f"/v1/jobs/{jid}")[1]["status"] == "scheduled"
        assert call("POST", f"/v1/jobs/{jid}/cancel", {})[1]["status"] == "cancelled"
        assert call("GET", "/v1/jobs/nope")[0] == 404
        assert call("GET", "/v1/notifications?after=0")[0] == 200
        assert call("GET", "/v1/recordings?receiver=OBS-R02")[0] == 200
        assert call("GET", "/v1/capabilities", token="wrong")[0] == 401
    finally:
        server.should_exit = True


def test_broker_restart_keeps_a_running_recordings_lease(env):
    from aobs.coordination.broker import Broker
    from test_broker import FakeHw

    env.cfg.raw["broker"] = {"receivers": ["OBS-R05"]}
    hw = FakeHw()
    first = Broker(env.cfg, hw, env.clock)
    first.lease("scheduler:m9", ttl_s=600, port=1)
    first.lease_token_for("scheduler:m9")
    hw.calls.clear()
    second = Broker(env.cfg, hw, env.clock)
    second.recover(keep=lambda lease: lease.owner == "scheduler:m9")
    assert "adsb start 0" not in hw.calls and second.lease_token_for("scheduler:m9")
    third = Broker(env.cfg, hw, env.clock)
    third.recover(keep=lambda lease: False)
    assert "adsb start 0" in hw.calls and third.lease_token_for("scheduler:m9") is None


def test_auto_raw_iq_is_pruned_after_retention_but_manual_is_not(env):
    env.s.settings["auto_retention_hours"] = 1
    planner(env, [window(60, 75)]).plan()
    auto = env.s.jobs(receiver="OBS-R02", source="auto")[0]
    manual = submit(env, rtl_job("keep-me", rx="OBS-R05", receiver__antenna="V-DIPOLE", satdump={"enabled": False},
                                 postprocess={"decode": False}, target={"type": "frequency", "name": "x"}))["results"][0]
    for jid in (auto["job_id"], manual["job_id"]):
        raw = pathlib.Path(env.s.get(jid)["paths"]["raw"])
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"iq")
        env.s.transition(jid, "completed", "")
    env.clock.now = NOW + dt.timedelta(hours=3)
    assert planner(env, []).prune_auto() == 1
    assert not pathlib.Path(env.s.get(auto["job_id"])["paths"]["raw"]).exists()
    assert pathlib.Path(env.s.get(manual["job_id"])["paths"]["raw"]).exists()


def test_antenna_alias_is_accepted_and_errors_are_reported_together(env):
    planner(env, [window(60, 75)]).plan()
    job = rtl_job(rx="OBS-R01", receiver__antenna="QFH", radio__iq_correction=True, start=70, end=80)
    env.s.receivers["OBS-R01"] = env.s.receivers["OBS-R01"].__class__(
        **{**env.s.receivers["OBS-R01"].__dict__, "signal_path": "Diamond DP-KE137 QFH > filter > OBS-R01"})
    text = errors(submit(env, job))
    assert "hard-wired" not in text
    assert "iq_correction" in text and "cannot yet be told to skip" in text


def test_decode_pipeline_defaults_from_the_recording(env):
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    a = runner.proc.submit(jid, "decode", {})
    assert a["resolved"]["pipeline"] == "meteor_m2-x_lrpt"          # the recording's target is METEOR-M2-4
    assert a["resolved"]["target_frequency_hz"] == 137_900_000 and a["resolved"]["shift_method"] == "internal"


def test_satdump_exit_zero_without_images_is_a_failed_decode(env, tmp_path):
    fake = tmp_path / "satdump"
    fake.write_text('#!/bin/sh\nmkdir -p "$4"\necho "Deframer : NOSYNC"\nexit 0\n')
    fake.chmod(0o755)
    env.s.settings["satdump_path"] = str(fake)
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    a = runner.proc.submit(jid, "decode", {"pipeline": "meteor_m2-x_lrpt", "search_offsets_hz": [0]}, "slack")
    drain(runner.proc)
    done = runner.proc.attempt(a["attempt_id"])
    assert done["status"] == "failed" and "no CADU frames" in done["status_reason"]
    assert done["result"]["ok"] is False                                # the worker itself no longer claims success
    assert done["result"]["inputs"][0]["return_code"] == 0 and done["result"]["inputs"][0]["nosync_reports"] == 1
    note = [n for n in env.s.notifications() if n["kind"].startswith("attempt")][-1]
    assert note["kind"] == "attempt_failed" and "NOT decoded" in note["text"]


def test_fixed_bias_tee_must_be_stated_truthfully(env):
    rx = env.s.receivers["OBS-R05"]
    env.s.receivers["OBS-R05"] = rx.__class__(**{**rx.__dict__, "bias_tee_fixed": True})
    base = dict(rx="OBS-R05", receiver__antenna="MA-30-LOOP", satdump={"enabled": False}, postprocess={"decode": False},
                target={"type": "frequency", "name": "x"}, radio__center_frequency_hz=137_500_000)
    assert "permanently ON" in errors(submit(env, rtl_job(**base)))                       # bias_tee false is wrong
    assert submit(env, rtl_job(**base, radio__bias_tee=True))["results"][0]["stored"]
    caps = next(r for r in env.s.capabilities()["receivers"] if r["receiver_id"] == "OBS-R05")
    assert caps["bias_tee"]["schedulable_values"] == [True]


def test_old_misreported_decodes_are_reclassified_without_losing_results(env):
    from aobs.db.schema import proc_attempts
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    a = runner.proc.submit(jid, "decode", {"search_offsets_hz": [0]})
    with env.s.engine.begin() as conn:
        conn.execute(proc_attempts.update().where(proc_attempts.c.attempt_id == a["attempt_id"]).values(
            status="completed", result={"inputs": [{"decoded": False, "images": 0}], "ok": True}))
    assert runner.proc.reclassify_decodes() == 1
    after = runner.proc.attempt(a["attempt_id"])
    assert after["status"] == "failed" and after["status_reason"].startswith("reclassified")
    assert after["result"]["inputs"][0]["images"] == 0                  # result preserved
