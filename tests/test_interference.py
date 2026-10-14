from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any

import numpy as np
import pytest

from aobs.scheduling import stepcapture
from aobs.scheduling.interference import diagnose
from aobs.scheduling.store import SchedulerError
from test_scheduler import FakeHandle, FakeRunnerHw, env  # noqa: F401
from test_testlab import lab as _lab_fixture  # noqa: F401

RATE = 2_400_000


def m_with(**over: Any) -> dict[str, Any]:
    s = {"rbw_hz": 1171.9, "channel": {"measured": True, "target_frequency_hz": 137_900_000, "bandwidth_hz": 25_000,
         "snr_db": 12.0, "power_over_noise_db": 12.3}, "clipped_fraction": 0.0, "peak_sample_fraction_of_full_scale": 0.3,
         "findings": [], "noise_floor_variation_db": 1.0, "spectral_asymmetry_db": 0.2, "broadband_noise_rises": 0}
    s.update(over.pop("summary", {}))
    m = {"summary": s, "signals": [], "transients": [], "iq": {"dc_offset_i": 0.001, "dc_offset_q": 0.0,
         "iq_amplitude_imbalance_db": 0.1, "iq_correlation": 0.01}, "recording": {"center_frequency_hz": 137_600_000,
         "sample_rate_hz": RATE}, "intermod_hypotheses": []}
    m.update(over)
    return m


def sig(f: float, over: float, duty: float = 1.0, bw: int = 1171) -> dict[str, Any]:
    return {"frequency_hz": int(f), "bandwidth_hz": bw, "duty_cycle": duty, "peak_over_floor_db": over,
            "start_s": 0.1, "end_s": 59.0}


def test_quiet_capture_has_no_anomalies_and_says_clipping_proves_nothing():
    d = diagnose(m_with())
    assert d["anomalies"] == [] and d["no_overload_indicators"]
    assert any("does not rule out analog overload" in n for n in d["notes"])
    assert d["suggestions"][0]["test"] == "baseline"


def test_persistent_carrier_in_the_channel_is_high_severity_with_explanations():
    d = diagnose(m_with(signals=[sig(137_905_000, 25)]))
    a = d["anomalies"][0]
    assert a["kind"] == "persistent_spur" and a["severity"] == "high" and a["confidence"] == "high"
    assert "inside the target channel" in a["title"] and len(a["explanations"]) >= 2
    t = next(x for x in d["suggestions"] if x["test"] == "termination")
    assert "narrowband" in t["because"]


def test_comb_clipping_and_overload_are_only_ever_possible():
    sigs = [sig(137.0e6 + k * 100_000, 18) for k in range(6)] + [sig(137.3e6, 45)]
    d = diagnose(m_with(signals=sigs, summary={"clipped_fraction": 0.002, "peak_sample_fraction_of_full_scale": 1.0},
                        intermod_hypotheses=[{"frequency_hz": 137_100_000, "from_hz": [1, 2]}]))
    kinds = {a["kind"]: a for a in d["anomalies"]}
    assert "spur_comb" in kinds and kinds["clipping"]["severity"] == "high"
    ov = kinds["analog_overload"]
    assert ov["title"] == "Possible analog overload" and ov["confidence"] == "medium"
    assert any("not proven" in x for x in ov["explanations"])
    assert {"gain_sweep", "termination", "scan"} <= {x["test"] for x in d["suggestions"]}
    assert all(x["because"] for x in d["suggestions"])


def test_no_target_signal_suggests_repeating_instead_of_guessing():
    d = diagnose(m_with(summary={"channel": {"measured": True, "target_frequency_hz": 137_900_000, "bandwidth_hz": 150_000,
                                             "snr_db": None, "power_over_noise_db": 0.2}}))
    assert any(x["test"] == "repeat" and "0.2 dB" in x["because"] for x in d["suggestions"])


# ---------------------------------------------------------------- end to end
class AntennaHw(FakeRunnerHw):
    """Tone + external interferer arrive through the antenna; an internal spur is always there."""
    def __init__(self) -> None:
        super().__init__()
        self.antenna = True

    def spawn(self, argv, log):
        run = json.loads(pathlib.Path(argv[-1]).read_text())
        r = run["radio"]
        n = int(r["sample_rate_hz"] * 3)
        t = np.arange(n) / r["sample_rate_hz"]
        rng = np.random.default_rng(3)
        x = rng.normal(0, 3, n) + 1j * rng.normal(0, 3, n)
        x += 4 * np.exp(2j * np.pi * 512_000 * t)                                  # internal spur
        if self.antenna:
            x += 8 * np.exp(2j * np.pi * (r["target_frequency_hz"] - r["center_frequency_hz"]) * t)
            x += 25 * np.exp(2j * np.pi * -650_000 * t)                            # external interferer
        iq = np.empty(2 * n)
        iq[0::2], iq[1::2] = x.real + 127.5, x.imag + 127.5
        raw = pathlib.Path(run["raw"])
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(iq.clip(0, 255).round().astype(np.uint8).tobytes())
        pathlib.Path(run["result"]).write_text(json.dumps({"status": "recorded", "bytes": raw.stat().st_size,
                                                           "actual_start": run["record_start"]}))
        return FakeHandle(1)


class GainRF:
    bytes_per_sample = 2

    def __init__(self, rate: int) -> None:
        self.rate, self.g, self.f = rate, 20.0, 0
        self.rng = np.random.default_rng(5)

    def tune(self, hz: int) -> int:
        self.f = hz
        return hz

    def set_gain(self, g: Any) -> Any:
        self.g = float(g)
        return g

    def read(self, n: int) -> tuple[bytes, int]:
        t = np.arange(n) / self.rate
        k = 10 ** ((self.g - 30) / 20)                       # gain scales antenna signal and antenna noise
        x = k * (6 * np.exp(2j * np.pi * 300_000 * t) + 60 * np.exp(2j * np.pi * -800_000 * t)
                 + self.rng.normal(0, 2, n) + 1j * self.rng.normal(0, 2, n))
        x += self.rng.normal(0, 0.8, n) + 1j * self.rng.normal(0, 0.8, n)          # the ADC's own noise
        iq = np.empty(2 * n)
        iq[0::2], iq[1::2] = x.real + 127.5, x.imag + 127.5
        return iq.clip(0, 255).round().astype(np.uint8).tobytes(), 0

    def close(self) -> None:
        pass


class GainHw(FakeRunnerHw):
    def spawn(self, argv, log):
        run = json.loads(pathlib.Path(argv[-1]).read_text())
        raw = pathlib.Path(run["raw"])
        raw.parent.mkdir(parents=True, exist_ok=True)
        part = raw.with_name(raw.name + ".part")
        now = dt.datetime.now(dt.timezone.utc)
        res = stepcapture.capture(run, part, now, now + dt.timedelta(hours=1), lambda: False,
                                  open_device=lambda run: GainRF(int(run["radio"]["sample_rate_hz"])))
        part.rename(raw)
        pathlib.Path(run["result"]).write_text(json.dumps({**res, "file": str(raw), "actual_start": run["record_start"]}))
        return FakeHandle(1)


@pytest.fixture
def ilab(env, _lab_fixture):
    lab = _lab_fixture
    lab.runner.hw = lab.hw = AntennaHw()
    return lab


def run(env, lab, body: dict[str, Any]) -> str:
    started = lab.start(body.get("receiver_id", "OBS-R02"), body, "owner (web)")
    job = env.s.get(started["job_id"])
    env.clock.now = dt.datetime.fromisoformat(job["reservation_start"]) + dt.timedelta(seconds=1)
    lab.runner.tick(threaded=False)
    for _ in range(10):
        lab.proc.step()
    return started["test_id"]


INV = {"receiver_id": "OBS-R02", "test_type": "interference", "frequency": "137.9", "sample_rate_hz": RATE,
       "gain_db": 20.7, "duration_s": 60, "preset": "satellite", "signal_type": "meteor_lrpt"}


def test_investigation_reports_anomalies_and_grounded_suggestions(env, ilab):
    tid = run(env, ilab, INV)
    d = ilab.detail(tid)
    inv = d["metrics"]["investigation"]
    assert inv["preset_label"] == "Satellite reception interference"
    spur = [a for a in inv["anomalies"] if a["kind"] == "persistent_spur"]
    assert spur and all(a["frequency_hz"] and a["explanations"] for a in spur)
    assert any(x["test"] == "termination" for x in inv["suggestions"])
    html = ilab.report(tid)
    assert "Interference investigation" in html and "Suggested follow-up" in html
    assert ilab.list(test_type="interference")[0]["test_id"] == tid


def test_antenna_vs_termination_separates_internal_from_external(env, ilab):
    a = run(env, ilab, {**INV, "input_state": "antenna", "workflow": "termination"})
    ilab.hw.antenna = False
    b = run(env, ilab, {**INV, "input_state": "terminated", "workflow": "termination", "baseline_test_id": a})
    c = ilab.compare_termination(a, b)
    rem = [x["frequency_hz"] for x in c["remained"]]
    gone = [x["frequency_hz"] for x in c["disappeared"]]
    center = 137_900_000 - RATE // 8
    assert any(abs(f - (center + 512_000)) < 5_000 for f in rem)                 # the internal spur stays
    assert any(abs(f - (center - 650_000)) < 5_000 for f in gone)                # the external interferer goes
    assert any("does not conclusively identify" in h for h in c["report"]["hypotheses"])
    o = run(env, ilab, {**INV, "input_state": "open", "baseline_test_id": a})
    assert "OPEN input" in ilab.compare_termination(a, o)["report"]["warnings"][0]
    with pytest.raises(SchedulerError, match="input_state"):
        ilab.start("OBS-R02", {**INV, "input_state": "floating"}, "owner (web)")


def test_gain_sweep_finds_clipping_onset_and_best_contrast(env, ilab):
    ilab.runner.hw = ilab.hw = GainHw()
    tid = run(env, ilab, {"receiver_id": "OBS-R02", "test_type": "gain_sweep", "frequency": "137.9",
                          "sample_rate_hz": RATE, "dwell_s": 0.3, "channel_bandwidth_hz": 5000,
                          "gains": [0.0, 12.5, 20.7, 29.7, 38.6, 49.6]})
    d = ilab.detail(tid)
    m = d["metrics"]
    assert [r["gain"] for r in m["rows"]] == [0.0, 12.5, 20.7, 29.7, 38.6, 49.6]
    rms = [r["rms_dbfs"] for r in m["rows"]]
    assert rms == sorted(rms)                                                    # more gain, more digital power
    assert m["summary"]["clipping_starts_at"] in ("38.6 dB", "49.6 dB")
    best = m["summary"]["best_contrast"]
    assert best and best["gain"] < 38.6                                          # best contrast is not at max gain
    assert "not better in itself" in m["summary"]["caution"]
    assert "tuner gain dB" in m["gain_semantics"]
    job = env.s.get(d["job"]["job_id"])
    assert len(job["spec"]["steps"]["steps"]) == 6 and job["spec"]["metadata"]["quiet"]
    with pytest.raises(SchedulerError, match="tuner's steps"):
        ilab.start("OBS-R02", {"test_type": "gain_sweep", "frequency": "137.9", "sample_rate_hz": RATE,
                               "gains": [0.0, 21.0]}, "owner (web)")


def test_preset_reaches_the_analysis_and_termination_has_no_false_warning(env, ilab):
    tid = run(env, ilab, {**INV, "preset": "lna_overload"})
    assert ilab.detail(tid)["metrics"]["investigation"]["preset_label"] == "LNA overload investigation"
    b = run(env, ilab, {**INV, "input_state": "terminated", "baseline_test_id": tid})
    assert not any(w.startswith("Nothing differs") for w in ilab.compare_termination(tid, b)["report"]["warnings"])
