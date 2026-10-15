from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any

import numpy as np
import pytest

from aobs.scheduling import rflayout
from aobs.scheduling.store import SchedulerError
from aobs.scheduling.testlab import TestLab, compare_metrics, parse_hz
from test_rflayout import owner_layout
from test_scheduler import NOW, FakeHandle, FakeRunnerHw, env, inline_processor, planner, window  # noqa: F401

RATE = 2_400_000


class ToneHw(FakeRunnerHw):
    """Writes cu8 IQ: noise plus a tone at the job's target frequency (amplitude `tone`, 0 = noise only)."""
    def __init__(self, tone: float = 6.0, noise: float = 4.0, interferers: tuple[float, ...] = ()) -> None:
        super().__init__()
        self.tone, self.noise, self.interferers = tone, noise, interferers

    def spawn(self, argv, log):
        run = json.loads(pathlib.Path(argv[-1]).read_text())
        radio = run["radio"]
        n = int(radio["sample_rate_hz"] * 3)                   # 3 s is plenty for the analysis
        t = np.arange(n) / radio["sample_rate_hz"]
        rng = np.random.default_rng(1)
        x = (rng.normal(0, self.noise, n) + 1j * rng.normal(0, self.noise, n))
        off = radio["target_frequency_hz"] - radio["center_frequency_hz"]
        x += self.tone * np.exp(2j * np.pi * off * t)
        for f in self.interferers:
            x += 40 * np.exp(2j * np.pi * f * t)
        iq = np.empty(2 * n, np.float64)
        iq[0::2], iq[1::2] = x.real + 127.5, x.imag + 127.5
        raw = pathlib.Path(run["raw"])
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(iq.clip(0, 255).round().astype(np.uint8).tobytes())
        self.calls.append(("spawn", run["serial"]))
        pathlib.Path(run["result"]).write_text(json.dumps({"status": "recorded", "bytes": raw.stat().st_size,
                                                           "actual_start": run["record_start"]}))
        return FakeHandle(4242)


@pytest.fixture
def lab(env):
    from aobs.scheduling.runner import Runner
    from aobs.scheduling.service import SchedulerService
    env.s.rf = rflayout.RfLayout(env.s)
    env.s.rf.load()
    env.s.rf.save(owner_layout(env.s.rf), 1, "owner (web)")
    hw = ToneHw()
    proc = inline_processor(env)
    runner = Runner(env.s, hw, sleep=lambda s: None, processor=proc)
    svc = SchedulerService(env.s, planner(env, []), runner, processor=proc)
    leases: dict[str, Any] = {}
    t = TestLab(svc, lambda: {"leases": leases})
    t.hw, t.runner, t.proc, t.leases = hw, runner, proc, leases
    return t


def run_test(env, lab, body: dict[str, Any]) -> dict[str, Any]:
    started = lab.start(body.get("receiver_id", "OBS-R02"), body, "owner (web)")
    job = env.s.get(started["job_id"])
    env.clock.now = dt.datetime.fromisoformat(job["reservation_start"]) + dt.timedelta(seconds=1)
    lab.runner.tick(threaded=False)
    for _ in range(10):
        lab.proc.step()
    return started


BASE = {"receiver_id": "OBS-R02", "frequency": "137.9 MHz", "sample_rate_hz": RATE, "gain_db": 20.7, "duration_s": 60,
        "name": "baseline", "tags": ["discone", "LNA"]}


def test_parse_hz():
    assert parse_hz("137.9 MHz", "f") == 137_900_000 and parse_hz("137.9", "f") == 137_900_000
    assert parse_hz(137900000, "f") == 137_900_000 and parse_hz("145800 kHz", "f") == 145_800_000
    assert parse_hz("1.0897 GHz", "f") == 1_089_700_000
    with pytest.raises(SchedulerError):
        parse_hz("abc", "f")


def test_options_are_filled_from_what_the_system_knows(env, lab):
    o = lab.options("OBS-R02")
    assert [c["name"] for c in o["chain"]] == ["Discone", "Nooelec LaNA"]
    assert o["signal_path"] == "Discone > Nooelec LaNA > OBS-R02"
    assert o["capabilities"]["gain_db_values"] and o["defaults"]["duration_s"] == 60
    assert o["availability"]["available"] and o["bias_tee"]["fixed"] is None
    r05 = lab.options("OBS-R05")
    assert r05["switched"] and r05["switch_ports"] == {1: "V-DIPOLE", 8: "MA-30-LOOP"}
    with pytest.raises(SchedulerError, match="not a receiver in Starlab.s inventory"):
        lab.options("OBS-R03")


def test_a_test_records_analyzes_and_reports_without_slack(env, lab):
    before = len(env.s.notifications(0))
    started = run_test(env, lab, BASE)
    job = env.s.get(started["job_id"])
    assert job["spec"]["conflict_policy"]["on_automatic_job"] == "reject" and job["spec"]["metadata"]["quiet"]
    assert job["spec"]["radio"]["center_frequency_hz"] == 137_900_000 - RATE // 8      # DC spike kept out of the channel
    assert job["spec"]["resolved"]["rf_path"]["text"] == "Discone > Nooelec LaNA > OBS-R02"
    d = lab.detail(started["test_id"])
    assert d["progress"]["phase"] == "complete", d["progress"]
    s = d["metrics"]["summary"]
    assert s["units"].startswith("dBFS") and s["channel"]["measured"]
    assert s["snr_db"] is not None and s["snr_db"] > 10 and "ENBW" in s["channel"]["snr_method"]
    assert s["clipped_fraction"] == 0 and s["adc_codes_used"] > 24
    assert {"waterfall", "spectrum", "metrics", "raw_iq", "power"} <= set(d["files"])
    sp = d["metrics"]["spectrum"]
    assert len(sp["freqs_hz"]) == 1024 and sp["freqs_hz"][0] < 137_900_000 < sp["freqs_hz"][-1]
    assert d["metrics"]["waterfall"]["u8_b64"] and d["metrics"]["time"]["channel_dbfs"]
    assert [n for n in env.s.notifications(0)[before:] if n["kind"] != "health"] == []   # quiet: nothing to Slack
    listed = lab.list(receiver="OBS-R02")
    assert listed[0]["test_id"] == started["test_id"] and listed[0]["snr_db"] == s["snr_db"]
    assert lab.list(part="LANA") and not lab.list(part="AM-BANDSTOP") and lab.list(tag="discone")
    html = lab.report(started["test_id"])
    assert "Signal path test" in html and "data:image/png;base64," in html and "Methodology" in html


def test_noise_only_says_snr_unavailable(env, lab):
    lab.hw.tone = 0.0
    d = lab.detail(run_test(env, lab, BASE)["test_id"])
    s = d["metrics"]["summary"]
    assert s["snr_db"] is None and s["channel"]["snr_unavailable"].startswith("SNR unavailable")


def test_ab_comparison_shows_the_path_change_and_stays_honest(env, lab):
    a = run_test(env, lab, BASE)["test_id"]
    lay = json.loads(json.dumps(env.s.rf.current["layout"]))
    lay["receivers"]["OBS-R02"]["chain"] = ["DISCONE", "FM-BANDSTOP-2", "LANA"]
    lay["receivers"]["OBS-R05"]["chain"] = ["ANTSW-01"]
    env.s.rf.save(lay, env.s.rf.current["version"], "owner (web)", "FM bandstop on R02")
    lab.hw.tone, lab.hw.interferers = 18.0, (-600_000.0,)
    b = run_test(env, lab, {**BASE, "name": "FM filter installed", "baseline_test_id": a})["test_id"]
    c = lab.compare(a, b)
    assert c["path"]["changes"] == ["added filter FM bandstop"] and c["settings_differ"] == []
    assert c["path"]["a"] == "Discone > Nooelec LaNA > OBS-R02"          # history is not rewritten by the new layout
    assert lab.detail(a)["job"]["rf_path"]["text"] == "Discone > Nooelec LaNA > OBS-R02"
    rows = {r["key"]: r for r in c["metrics"]}
    assert rows["snr_db"]["change"] > 3
    assert c["overlay"] and len(c["overlay"]["diff_db"]) > 100
    rep = c["report"]
    assert "suggests" in rep["verdict"] and any("RF sensitivity" in h for h in rep["hypotheses"])
    assert lab._row(b)["group_id"] == a and lab._row(a)["group_id"] == a
    # different gain: absolute levels flagged, and no verdict beyond "inconclusive"
    d = run_test(env, lab, {**BASE, "gain_db": 40.2, "baseline_test_id": a})["test_id"]
    rep = lab.compare(a, d)["report"]
    assert any("Gain settings differ" in w for w in rep["warnings"]) and rep["verdict"].startswith("Inconclusive")


def test_tests_never_displace_satellite_passes(env, lab):
    planner(env, [window(1.5, 10)]).plan()                     # an automatic R02 pass starting in 90 s
    av = lab.availability("OBS-R02", 60)
    assert not av["available"] and "Try after" in av["reasons"][0]
    with pytest.raises(SchedulerError, match="cannot run safely right now"):
        lab.start("OBS-R02", BASE, "owner (web)")
    assert lab.list() == []
    auto = env.s.jobs(receiver="OBS-R02", source="auto")[0]
    assert auto["status"] == "scheduled"


def test_policy_rejects_preemption_even_past_the_availability_check(env, lab):
    from test_scheduler import rtl_job, submit
    planner(env, [window(30, 45)]).plan()
    job = rtl_job("would-preempt", start=31, end=40, conflict_policy={"on_automatic_job": "reject"})
    r = submit(env, job)["results"][0]
    assert not r["ok"] and any("never displaces automatic passes" in e for e in r["errors"])
    assert submit(env, rtl_job("would-preempt", start=31, end=40))["results"][0]["preempts"]   # default unchanged


def test_busy_or_leased_receivers_are_refused(env, lab):
    lab.hw.busy_with = ["pid 77 (rtl_tcp)"]
    with pytest.raises(SchedulerError, match="open in another program"):
        lab.start("OBS-R02", BASE, "owner (web)")
    lab.hw.busy_with = []
    lab.leases["OBS-R02"] = {"owner": "slack-agent", "purpose": "scan"}
    assert "leased to slack-agent" in lab.availability("OBS-R02")["reasons"][0]


@pytest.mark.parametrize("over, message", [
    ({"gain_db": 21}, "tuner's steps"),
    ({"duration_s": 3}, "duration"),
    ({"frequency": "139.5 MHz", "center_frequency": "137.0 MHz"}, "outside the usable band"),
    ({"tags": ["ok", "bad/tag"]}, "tags"),
    ({"signal_type": "x"}, "signal_type"),
    ({"receiver_id": "OBS-R05"}, "choose the antenna"),
])
def test_bad_parameters_are_refused_with_reasons(env, lab, over, message):
    body = {**BASE, **over}
    with pytest.raises(SchedulerError, match=message):
        lab.start(body["receiver_id"], body, "owner (web)")


def test_switched_receiver_records_its_port_and_keeps_the_fixed_bias_tee(env, lab):
    env.s.receivers["OBS-R05"] = __import__("dataclasses").replace(env.s.receivers["OBS-R05"], bias_tee_fixed=True)
    lab.svc.runner.take_switched = lambda rx, jid, port, until: "lease"
    lab.svc.runner.release_switched = lambda tok: None
    started = run_test(env, lab, {**BASE, "receiver_id": "OBS-R05", "antenna": "MA-30-LOOP", "frequency": "137.9"})
    job = env.s.get(started["job_id"])
    assert job["spec"]["radio"]["bias_tee"] is True and job["spec"]["resolved"]["switch_port"] == 8
    assert lab._row(started["test_id"])["request"]["switch_port"] == 8
    assert "MA-30-LOOP" in job["spec"]["resolved"]["rf_path"]["text"]


def test_presets_and_notes(env, lab):
    p = lab.save_preset({"name": "METEOR 137.9 MHz", "params": {"frequency": "137.9", "sample_rate_hz": RATE,
                                                                 "signal_type": "meteor_lrpt", "evil": 1}})
    assert p["params"] == {"frequency": "137.9", "sample_rate_hz": RATE, "signal_type": "meteor_lrpt"}
    assert lab.presets("OBS-R01")[0]["name"] == "METEOR 137.9 MHz"
    t = run_test(env, lab, BASE)["test_id"]
    assert lab.update(t, {"notes": "antenna pointed north", "tags": ["a", "b"]})["notes"] == "antenna pointed north"
    lab.delete_preset(p["preset_id"])
    assert lab.presets() == []
