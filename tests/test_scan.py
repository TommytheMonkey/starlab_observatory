from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any

import numpy as np
import pytest

from aobs.scheduling import scanmetrics as sm
from aobs.scheduling import stepcapture
from aobs.scheduling.store import SchedulerError
from test_scheduler import NOW, FakeHandle, FakeRunnerHw, env  # noqa: F401
from test_testlab import lab as _lab_fixture  # noqa: F401

FM = (88.5e6, 91.1e6, 95.3e6, 101.1e6, 105.7e6)


class FakeRF:
    """A simulated antenna + receiver: FM stations, a 137.1 MHz carrier, noise; an optional FM bandstop."""
    bytes_per_sample = 2

    def __init__(self, rate: int, filt: bool = False, noise: float = 3.0, loss_db: float = 0.0):
        self.rate, self.filt, self.noise, self.loss = rate, filt, noise, loss_db
        self.f = 0
        self.rng = np.random.default_rng(7)
        self.gains: list[Any] = []

    def tune(self, hz: int) -> int:
        self.f = hz
        return hz

    def set_gain(self, gain: Any) -> Any:
        self.gains.append(gain)
        return gain

    def read(self, n: int) -> tuple[bytes, int]:
        t = np.arange(n) / self.rate
        x = self.rng.normal(0, self.noise, n) + 1j * self.rng.normal(0, self.noise, n)
        for fc, amp in [(f, 30.0) for f in FM] + [(137.1e6, 12.0)]:
            off = fc - self.f
            if abs(off) < self.rate * 0.45:
                a = amp * (10 ** (-25 / 20) if self.filt and 88e6 <= fc <= 108e6 else 1.0) * 10 ** (-self.loss / 20)
                x += a * np.exp(2j * np.pi * off * t)
        iq = np.empty(2 * n)
        iq[0::2], iq[1::2] = x.real + 127.5, x.imag + 127.5
        return iq.clip(0, 255).round().astype(np.uint8).tobytes(), 0

    def close(self) -> None:
        pass


class StepHw(FakeRunnerHw):
    def __init__(self) -> None:
        super().__init__()
        self.filt, self.loss, self.devices = False, 0.0, []

    def spawn(self, argv, log):
        run = json.loads(pathlib.Path(argv[-1]).read_text())
        raw = pathlib.Path(run["raw"])
        raw.parent.mkdir(parents=True, exist_ok=True)
        part = raw.with_name(raw.name + ".part")
        now = dt.datetime.now(dt.timezone.utc)

        def dev(run: dict[str, Any]) -> FakeRF:
            d = FakeRF(int(run["radio"]["sample_rate_hz"]), self.filt, loss_db=self.loss)
            self.devices.append(d)
            return d
        if run.get("steps"):
            res = stepcapture.capture(run, part, now, now + dt.timedelta(hours=1), lambda: False, open_device=dev)
        else:
            d = dev(run)
            d.tune(run["radio"]["center_frequency_hz"])
            part.write_bytes(d.read(int(run["radio"]["sample_rate_hz"] * 2))[0])
            res = {"status": "recorded", "bytes": part.stat().st_size}
        part.rename(raw)
        res["file"] = str(raw)
        pathlib.Path(run["result"]).write_text(json.dumps({**res, "actual_start": run["record_start"]}))
        self.calls.append(("spawn", run["serial"]))
        return FakeHandle(4242)


@pytest.fixture
def scanlab(env, _lab_fixture):
    lab = _lab_fixture
    hw = StepHw()
    lab.runner.hw = hw
    lab.hw = hw
    return lab


def run(env, lab, body: dict[str, Any]) -> str:
    started = lab.start(body.get("receiver_id", "OBS-R02"), body, "owner (web)")
    job = env.s.get(started["job_id"])
    env.clock.now = dt.datetime.fromisoformat(job["reservation_start"]) + dt.timedelta(seconds=1)
    lab.runner.tick(threaded=False)
    for _ in range(10):
        lab.proc.step()
    return started["test_id"]


SCAN = {"receiver_id": "OBS-R02", "test_type": "scan", "start": "80", "stop": "160", "resolution": "coarse",
        "dwell_s": 0.02, "gain_db": 20.7, "sample_rate_hz": 2_400_000, "name": "no filter"}


def test_plan_is_grid_aligned_and_overlapping():
    p = sm.plan(80_000_000, 160_000_000, 2_400_000, "coarse", 0.25)
    rbw = p["rbw_hz"]
    assert p["nfft"] == 64 or p["nfft"] >= 256
    assert all(abs(c / rbw - round(c / rbw)) < 1e-6 for c in p["centers_hz"])
    assert p["stride_hz"] <= 0.75 * 2_400_000 / 2 + 1                      # half the usable span: 2x coverage
    usable = 0.75 * 2_400_000 / 2
    assert p["centers_hz"][0] - usable <= 80e6 and p["centers_hz"][-1] + usable >= 160e6
    assert 80 < len(p["centers_hz"]) < 100
    assert sm.estimate_seconds(len(p["centers_hz"]), 0.25, 0.06, 1, "rtlsdr") < 40


def test_step_runner_keeps_order_gain_and_stops_at_the_deadline(tmp_path):
    dev = FakeRF(2_400_000)
    t = [dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)]

    def clock() -> dt.datetime:
        t[0] += dt.timedelta(seconds=1)
        return t[0]
    steps = [{"center_hz": int(100e6 + i * 1e6), "dwell_s": 0.01, "gain": 20.7} for i in range(10)]
    idx = stepcapture.run_steps(dev, 2_400_000, steps, tmp_path / "x.part", t[0] + dt.timedelta(seconds=9), lambda: False, clock)
    assert not idx["complete"] and 0 < idx["completed_steps"] < 10
    assert [s["center_hz"] for s in idx["steps"]] == [st["center_hz"] for st in steps[:idx["completed_steps"]]]
    assert dev.gains == [20.7]                               # gain set once, not on every step
    assert (tmp_path / "x.part").stat().st_size == sum(s["samples"] for s in idx["steps"]) * 2


def test_scan_finds_the_fm_stations_and_reports_honest_units(env, scanlab):
    tid = run(env, scanlab, SCAN)
    d = scanlab.detail(tid)
    assert d["progress"]["phase"] == "complete", d["progress"]
    s = d["metrics"]["summary"]
    assert s["units"].startswith("dBFS/Hz") and s["uncovered_bins"] == 0 and s["steps_completed"] == s["steps_planned"]
    found = [c["frequency_hz"] for c in d["metrics"]["carriers"]]
    for f in FM + (137.1e6,):
        assert any(abs(x - f) <= 2 * s["rbw_hz"] for x in found), f
    fm = s["regions"][0]
    assert fm["name"] == "FM broadcast" and fm["median_over_floor_db"] is not None
    job = env.s.get(d["job"]["job_id"])
    assert job["spec"]["steps"]["steps"][0]["gain"] == 20.7 and job["spec"]["metadata"]["quiet"]
    assert job["spec"]["resolved"]["rf_path"]["text"] == "Discone > Nooelec LaNA > OBS-R02"
    assert "scan_npz" in d["files"] and "metrics" in d["files"]
    assert scanlab.list(test_type="scan")[0]["test_id"] == tid


def test_before_after_shows_selective_filtering_and_saves_a_filter_test(env, scanlab):
    a = run(env, scanlab, SCAN)
    lay = json.loads(json.dumps(env.s.rf.current["layout"]))
    lay["receivers"]["OBS-R05"]["chain"] = ["ANTSW-01"]
    lay["receivers"]["OBS-R02"]["chain"] = ["DISCONE", "FM-BANDSTOP-2", "LANA"]
    env.s.rf.save(lay, env.s.rf.current["version"], "owner (web)", "FM bandstop on R02")
    scanlab.hw.filt = True
    b = run(env, scanlab, {**SCAN, "name": "FM filter", "baseline_test_id": a})
    c = scanlab.compare_scans(a, b)
    fm = c["regions"][0]
    assert fm["median_change_db"] < -15, fm
    assert abs(c["outside_regions"]["median_change_db"]) < 2
    assert any("selective filtering" in x for x in c["report"]["interpretations"])
    assert any("over-the-air" in x for x in c["report"]["hypotheses"])
    assert c["path"]["changes"] == ["added filter FM bandstop"]
    fmc = [x for x in c["carriers"] if x["in_region"] == "FM broadcast"]
    assert len(fmc) == 5 and all(x["change_db"] < -15 for x in fmc)
    r = scanlab.filter_test({"component_id": "FM-BANDSTOP-2", "baseline_test_id": a, "modified_test_id": b}, "owner (web)")
    assert 18 < r["stopband_median_attenuation_db"] < 32 and r["path_note"] is None
    assert 88e6 <= r["max_attenuation_frequency_hz"] <= 108e6
    assert "Not a calibrated" in r["method"] and r["confidence"] in ("medium", "low") and r["confidence_reasons"]
    saved = scanlab.list(test_type="filter_test")[0]
    assert saved["component_id"] == "FM-BANDSTOP-2" and saved["summary"]["stopband_median_attenuation_db"]
    assert "curve" not in saved["summary"]


def test_overall_loss_is_not_called_filtering(env, scanlab):
    a = run(env, scanlab, SCAN)
    scanlab.hw.loss = 10.0
    b = run(env, scanlab, {**SCAN, "baseline_test_id": a})
    c = scanlab.compare_scans(a, b)
    assert c["regions"][0]["median_change_db"] < -6 and c["outside_regions"]["median_change_db"] < -6
    assert any("general loss" in x for x in c["report"]["interpretations"])
    assert not any("consistent with selective filtering" in x for x in c["report"]["interpretations"])


def test_repeated_sweeps_make_a_heatmap_and_persistence(env, scanlab):
    tid = run(env, scanlab, {**SCAN, "start": "95", "stop": "105", "scan_mode": "repeated", "repeats": 3})
    m = scanlab.detail(tid)["metrics"]
    assert m["summary"]["sweeps"] == 3 and m["heatmap"]["rows"] == 3
    assert all(c["persistence"] == 1.0 for c in m["carriers"][:2])


def test_scan_plan_and_refusals(env, scanlab):
    p = scanlab.scan_plan("OBS-R02", SCAN)
    assert p["steps"] > 80 and p["estimated_seconds"] < p["reserved_seconds"] and p["availability"]["available"]
    with pytest.raises(SchedulerError, match="tunes 24-1766 MHz"):
        scanlab.scan_plan("OBS-R02", {**SCAN, "start": "10", "stop": "30"})
    with pytest.raises(SchedulerError, match="dwell"):
        scanlab.scan_plan("OBS-R02", {**SCAN, "dwell_s": 9})
    with pytest.raises(SchedulerError, match="gain_db"):
        scanlab.scan_plan("OBS-R02", {**SCAN, "gain_db": 21})


def test_history_reports_the_scan_range_and_flags_mismatched_ranges(env, scanlab):
    a = run(env, scanlab, SCAN)
    row = scanlab.list(test_type="scan")[0]
    assert (row["frequency_hz"], row["stop_hz"]) == (80_000_000, 160_000_000)
    b = run(env, scanlab, {**SCAN, "start": "130", "stop": "160", "baseline_test_id": a})
    c = scanlab.compare_scans(a, b)
    assert any(d["setting"] == "Range (MHz)" for d in c["settings_differ"])
    assert any("outside the range both scans cover" in w for w in c["report"]["warnings"])
