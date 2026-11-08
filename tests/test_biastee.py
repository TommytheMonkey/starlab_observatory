"""Bias tee control: the RF layout decides it, recordings and Signal Path Tests switch it on, and it goes off again."""

from __future__ import annotations

import copy
import ctypes
import datetime as dt
import json
import pathlib
import types
from typing import Any

import pytest

from aobs.inventory.store import rtl_blog_bias_tee
from aobs.scheduling import capture, rflayout, stepcapture
from aobs.scheduling.hardware import validate_radio
from aobs.scheduling.rflayout import bias_tee_for_job, bias_tee_plan
from test_scheduler import env, errors, rtl_job, submit  # noqa: F401
from test_testlab import BASE, ToneHw, lab, run_test  # noqa: F401


# ------------------------------------------------------------------ the rule (pure)
def rx(control: bool = True, fixed: bool | None = None) -> Any:
    return types.SimpleNamespace(id="RX", bias_tee_control=control, bias_tee_fixed=fixed)


def path(*parts: tuple[str, str, str, str], feed: str | None = None) -> dict[str, Any]:
    """parts = (id, kind, power, dc_pass), antenna side first."""
    comps = {}
    for cid, kind, power, dc in parts:
        comps[cid] = {"id": cid, "name": cid, "kind": kind, "power": power, "status": "working"}
        if kind not in ("antenna", "rotator"):
            comps[cid]["dc_pass"] = dc
    return {"components": comps, "receivers": {"RX": {"id": "RX", "chain": [p[0] for p in parts], "feed": feed}}}


ANT = ("ANT", "antenna", "none", "")
LNA = ("LNA", "amplifier", "bias_tee", "unknown")


def test_on_when_a_part_is_powered_by_the_bias_tee():
    p = bias_tee_plan(path(ANT, LNA, ("COAX", "cable", "none", "unknown")), rx())
    assert p["enable"] and p["powered"] == ["LNA"] and p["reason"] == "on: LNA is powered by the bias tee"
    assert p["warnings"] == []


def test_off_when_nothing_is_marked_bias_tee_powered():
    p = bias_tee_plan(path(ANT, ("LNA", "amplifier", "external", "unknown")), rx())
    assert not p["enable"] and "nothing in the signal path is marked" in p["reason"]
    assert not bias_tee_plan(path(("QFH", "antenna", "none", ""), ("FM-STOP", "filter", "none", "unknown")), rx())["enable"]
    assert not bias_tee_plan(None, rx())["enable"]
    assert not bias_tee_plan({"components": {}, "receivers": {}}, rx())["enable"]


def test_rx02_qfh_bandstop_sawbird_turns_it_on():
    """The owner's RX-02 chain: QFH > FM bandstop > SAWbird NOAA (bias tee) > RX-02."""
    p = bias_tee_plan(path(("QFH", "antenna", "none", ""), ("FM-STOP", "filter", "none", "unknown"),
                           ("SAWBIRD", "amplifier", "bias_tee", "unknown")), rx())
    assert p["enable"] and p["powered"] == ["SAWBIRD"] and p["warnings"] == []      # the filter is beyond the LNA


@pytest.mark.parametrize("switch", ["ANTSW-01", "MANUAL-SW"])
def test_an_antenna_switch_in_the_path_is_warned_about(switch):
    p = bias_tee_plan(path((switch, "switch", "external", "unknown"), LNA), rx())
    assert p["enable"] and p["dc_sensitive"] == [f"{switch} (antenna switch)"]
    assert any("also reaches" in w for w in p["warnings"])


@pytest.mark.parametrize("dc", ["unknown", "yes"])
def test_an_unblocked_filter_between_receiver_and_the_powered_part_is_warned_about(dc):
    p = bias_tee_plan(path(ANT, LNA, ("FM-STOP", "filter", "none", dc)), rx())
    assert p["enable"] and p["dc_sensitive"] == ["FM-STOP (filter not marked as blocking DC)"]


def test_a_dc_blocking_part_between_receiver_and_lna_warns_that_it_gets_no_power():
    p = bias_tee_plan(path(ANT, LNA, ("FM-STOP", "filter", "none", "no")), rx())
    assert p["enable"] and p["dc_sensitive"] == [] and p["dc_blocked_by"] == ["FM-STOP"]
    assert p["warnings"] == ["FM-STOP blocks DC between RX and LNA: LNA gets no bias-tee power"]


def test_a_dc_block_beyond_the_lna_is_not_a_problem():
    p = bias_tee_plan(path(ANT, ("DCB", "connector", "none", "no"), LNA), rx())
    assert p["enable"] and p["warnings"] == [] and p["dc_blocked_by"] == []


def test_a_filter_beyond_the_powered_part_is_only_reached_when_the_lna_passes_dc_on():
    assert bias_tee_plan(path(ANT, ("SAW", "filter", "none", "unknown"), LNA), rx())["warnings"] == []
    p = bias_tee_plan(path(ANT, ("SAW", "filter", "none", "unknown"), ("LNA", "amplifier", "bias_tee", "yes")), rx())
    assert p["enable"] and p["dc_sensitive"] == ["SAW (filter not marked as blocking DC)"]


def test_a_splitter_the_dc_reaches_is_warned_about():
    p = bias_tee_plan(path(ANT, LNA, ("SPLIT", "splitter", "none", "unknown")), rx())
    assert p["enable"] and "SPLIT (splitter not marked as blocking DC)" in p["dc_sensitive"]


def test_ab_halves():
    lay = path(ANT, LNA)
    assert not bias_tee_for_job(lay, rx(), {"metadata": {"bias_tee_ab": "off"}})["enable"]
    assert bias_tee_for_job(lay, rx(), {"metadata": {"bias_tee_ab": "on"}})["enable"]
    bare = path(("QFH", "antenna", "none", ""))                                # never forced on
    p = bias_tee_for_job(bare, rx(), {"metadata": {"bias_tee_ab": "on"}})
    assert not p["enable"] and "stays off" in p["reason"]


def test_hard_wired_and_uncontrolled_receivers_are_not_switched():
    lay = path(ANT, LNA)
    assert bias_tee_plan(lay, rx(control=False, fixed=True))["enable"] is True        # EEPROM: it is on regardless
    assert "permanently on" in bias_tee_plan(lay, rx(control=False, fixed=True))["reason"]
    p = bias_tee_plan(lay, rx(control=False))
    assert not p["enable"] and "cannot switch" in p["reason"]


@pytest.mark.parametrize("state", ["terminated", "open"])
def test_a_test_with_the_antenna_side_disconnected_keeps_it_off(state):
    lay = path(ANT, LNA)
    assert bias_tee_for_job(lay, rx(), {"metadata": {"input_state": "antenna"}})["enable"]
    p = bias_tee_for_job(lay, rx(), {"metadata": {"input_state": state}})
    assert not p["enable"] and "for this test" in p["reason"]


# ------------------------------------------------------------------ which receivers Starlab may switch
def test_rtl_sdr_blog_dongles_are_recognised():
    assert rtl_blog_bias_tee({"manufacturer": "RTLSDRBlog", "model": "Blog V4L"})
    assert rtl_blog_bias_tee({"manufacturer": "", "model": "", "identification": {
        "fields": {"variant": {"value": "RTL-SDR Blog V4 Detected"}}}})
    assert not rtl_blog_bias_tee({"manufacturer": "Realtek", "model": "RTL2838UHIDIR"})
    assert not rtl_blog_bias_tee({"manufacturer": "Nooelec", "model": "NESDR SMArTee XTR"})


def test_a_web_added_blog_dongle_gets_bias_tee_control(env):
    common = dict(driver="rtlsdr", identity="usb_path", usb_vid="0bda", usb_pid="2838", config={},
                  capabilities=None, actor="owner (web)")
    env.s.inventory.create(rid="OBS-R07", name="OBS-R07", serial="00000001", usb_path="5-1", manufacturer="RTLSDRBlog",
                           model="Blog V4L", **common)
    env.s.inventory.create(rid="OBS-R08", name="OBS-R08", serial="00000002", usb_path="5-2", manufacturer="Realtek",
                           model="RTL2838UHIDIR", **common)
    env.s.reload_receivers()
    assert env.s.receivers["OBS-R07"].bias_tee_control
    assert not env.s.receivers["OBS-R08"].bias_tee_control
    assert not env.s.receivers["OBS-R02"].bias_tee_control                 # observatory.toml: bias_tee_supported = false
    r05 = env.s.receivers["OBS-R05"]                                       # switchable here, but behind ANTSW-01
    assert not bias_tee_plan(env.s.rf.current["layout"] if env.s.rf else None, r05)["enable"]


def test_validation_allows_a_switchable_rtl_bias_tee(env):
    radio = {"center_frequency_hz": 137_600_000, "sample_rate_hz": 2_400_000, "gain_mode": "manual", "gain_db": 20.7,
             "agc": False, "bias_tee": True, "bandwidth_hz": None}
    r02 = env.s.receivers["OBS-R02"]
    for rcv, bad in ((r02, "has no bias tee"), (r02.__class__(**{**r02.__dict__, "bias_tee_supported": True,
                                                                     "amplifier_power": ""}), None),
                     (r02.__class__(**{**r02.__dict__, "bias_tee_supported": True, "amplifier_power": "",
                                       "host": "comet"}), "only available for receivers on this computer")):
        e: list[str] = []
        validate_radio(rcv, dict(radio), "rtl_sdr", "cu8", e, [])
        assert (bad in " ".join(e)) if bad else not [x for x in e if "bias" in x], e


# ------------------------------------------------------------------ booking, recording and testing on a switchable RTL
class RunDocs(ToneHw):
    """Keeps every recorder run file, so the tests see exactly what the capture process was told."""
    def __init__(self) -> None:
        super().__init__()
        self.runs: list[dict[str, Any]] = []

    def spawn(self, argv, log):
        self.runs.append(json.loads(pathlib.Path(argv[-1]).read_text()))
        return super().spawn(argv, log)


def lna_layout(rf: rflayout.RfLayout, *extra: tuple[str, str, str]) -> None:
    """OBS-R02: Discone > bias-tee LNA (> extra parts) > receiver."""
    lay = copy.deepcopy(rf.current["layout"])
    c = lay["components"]
    c["LNA-BT"] = {"id": "LNA-BT", "name": "SAWbird+ (bias tee)", "kind": "amplifier", "status": "working",
                   "power": "bias_tee", "model": "", "notes": "", "gain_db": 30}
    chain = ["DISCONE", "LNA-BT"]
    for cid, kind, dc in extra:
        c[cid] = {"id": cid, "name": cid, "kind": kind, "status": "working", "power": "none", "model": "", "notes": "",
                  "dc_pass": dc}
        chain.append(cid)
    lay["receivers"]["OBS-R02"]["chain"] = chain
    rf.save(lay, rf.current["version"], "owner (web)")


@pytest.fixture
def blab(env, lab):
    """The test lab with OBS-R02 made a bias-tee receiver (as a Blog V4 is) behind a bias-tee powered LNA."""
    env.cfg.raw["scheduler"]["receivers"]["OBS-R02"].update(bias_tee_supported=True, amplifier_power="bias_tee")
    env.s.inventory.sync_from_toml(env.cfg)
    env.s.reload_receivers()
    lab.hw = lab.runner.hw = RunDocs()
    lna_layout(env.s.rf)
    assert env.s.receivers["OBS-R02"].bias_tee_control
    return lab


def test_booking_takes_the_bias_tee_from_the_layout(env, blab):
    r = submit(env, rtl_job())                                   # the request says bias_tee false
    assert r["results"][0]["stored"], errors(r)
    job = env.s.get(r["results"][0]["job_id"])
    assert job["spec"]["radio"]["bias_tee"] is True
    assert any("radio.bias_tee=true (RF layout: on: SAWbird+ (bias tee) is powered" in d for d in r["results"][0]["job"]["resolved"]["defaults_applied"])


def test_booking_turns_it_on_even_with_an_unblocked_filter_in_the_dc_path(env, blab):
    lna_layout(env.s.rf, ("FM-STOP", "filter", "unknown"))
    r = submit(env, rtl_job())
    assert r["results"][0]["stored"] and env.s.get(r["results"][0]["job_id"])["spec"]["radio"]["bias_tee"] is True
    _, _, warns = rflayout.validate(env.s.rf.current["layout"], env.s)
    assert any(w.startswith("OBS-R02: bias tee ON: the bias-tee DC also reaches FM-STOP") for w in warns)


def test_booking_refuses_bias_tee_on_when_nothing_needs_it(env, blab):
    lay = copy.deepcopy(env.s.rf.current["layout"])
    lay["receivers"]["OBS-R02"]["chain"] = ["DISCONE"]
    env.s.rf.save(lay, env.s.rf.current["version"], "owner (web)")
    j = rtl_job()
    j["radio"]["bias_tee"] = True
    assert "bias tee stays off for this signal path" in errors(submit(env, j))


def test_a_signal_path_test_records_with_the_bias_tee_on_and_says_so(env, blab):
    o = blab.options("OBS-R02")["bias_tee"]
    assert o["on"] and o["control"] and o["note"].startswith("ON for this test, SAWbird+ (bias tee) is powered")
    assert "switched off when it ends" in o["note"]
    started = run_test(env, blab, dict(BASE))
    job = env.s.get(started["job_id"])
    run = blab.hw.runs[-1]
    assert run["bias_tee_control"] is True and run["radio"]["bias_tee"] is True
    assert job["spec"]["radio"]["bias_tee"] is True
    notes = [d["message"] for d in job["execution"]["diagnostics"] if d["stage"] == "bias tee"]
    assert notes == ["bias tee on: SAWbird+ (bias tee) is powered by the bias tee"]


def test_a_terminated_input_test_keeps_it_off(env, blab):
    body = {**BASE, "test_type": "interference", "preset": "satellite", "signal_type": "meteor_lrpt",
            "input_state": "terminated", "frequency": "137.9"}
    run_test(env, blab, body)
    assert blab.hw.runs[-1]["radio"]["bias_tee"] is False and blab.hw.runs[-1]["bias_tee_control"] is True


def test_the_layout_at_start_wins_over_the_booking(env, blab):
    r = submit(env, rtl_job(start=5, end=8))
    jid = r["results"][0]["job_id"]
    lay = copy.deepcopy(env.s.rf.current["layout"])                # the LNA taken out after the booking
    lay["receivers"]["OBS-R02"]["chain"] = ["DISCONE"]
    env.s.rf.save(lay, env.s.rf.current["version"], "owner (web)")
    env.clock.now = dt.datetime.fromisoformat(env.s.get(jid)["reservation_start"]) + dt.timedelta(seconds=1)
    blab.runner.tick(threaded=False)
    run = blab.hw.runs[-1]
    assert run["radio"]["bias_tee"] is False and run["bias_tee_control"] is True
    note = next(d for d in env.s.get(jid)["execution"]["diagnostics"] if d["stage"] == "bias tee")
    assert note["level"] == "warning" and "booked on; the RF layout changed since" in note["message"]


def test_receivers_starlab_does_not_switch_get_no_control_flag(env, lab):
    lab.hw = lab.runner.hw = RunDocs()
    run_test(env, lab, dict(BASE))
    assert "bias_tee_control" not in lab.hw.runs[-1] and lab.hw.runs[-1]["radio"]["bias_tee"] is False


# ------------------------------------------------------------------ the recorder switches it, and always off afterwards
@pytest.fixture
def switch_log(monkeypatch):
    log: list[Any] = []

    def fake_set(serial, on, lib=None):
        log.append(("bias", serial, on))
        return {"on": on, "index": 0}
    monkeypatch.setattr(stepcapture, "set_rtl_bias_tee", fake_set)
    monkeypatch.setattr(capture.time, "sleep", lambda s: None)
    return log


def rtl_run(bias: bool) -> dict[str, Any]:
    return {"serial": "OBS-R07", "bias_tee_control": True, "radio": {"bias_tee": bias}}


def test_recorder_turns_it_on_before_rtl_sdr_and_off_after(switch_log, monkeypatch, tmp_path):
    def rec(run, part, start, end):
        switch_log.append("rtl_sdr")
        return {"status": "recorded", "warning": None}
    monkeypatch.setattr(capture, "_record_rtl", rec)
    out = capture.capture_rtl(rtl_run(True), tmp_path / "x.part", None, None)
    assert switch_log == [("bias", "OBS-R07", True), "rtl_sdr", ("bias", "OBS-R07", False)]
    assert out["bias_tee"]["requested"] is True and out["bias_tee"]["after"]["on"] is False and not out["warning"]


def test_recorder_turns_it_off_even_when_the_recording_fails(switch_log, monkeypatch, tmp_path):
    def rec(run, part, start, end):
        raise RuntimeError("rtl_sdr exited")
    monkeypatch.setattr(capture, "_record_rtl", rec)
    with pytest.raises(RuntimeError):
        capture.capture_rtl(rtl_run(True), tmp_path / "x.part", None, None)
    assert switch_log == [("bias", "OBS-R07", True), ("bias", "OBS-R07", False)]


def test_off_jobs_set_it_off_once_so_a_leftover_does_not_carry_over(switch_log, monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "_record_rtl", lambda *a: {"status": "recorded", "warning": None})
    capture.capture_rtl(rtl_run(False), tmp_path / "x.part", None, None)
    assert switch_log == [("bias", "OBS-R07", False)]


def test_a_failed_off_is_reported_on_the_recording(monkeypatch, tmp_path):
    calls = []

    def fake_set(serial, on, lib=None):
        calls.append(on)
        if not on:
            raise RuntimeError("rtlsdr_open failed (-6)")
        return {"on": on}
    monkeypatch.setattr(stepcapture, "set_rtl_bias_tee", fake_set)
    monkeypatch.setattr(capture.time, "sleep", lambda s: None)
    monkeypatch.setattr(capture, "_record_rtl", lambda *a: {"status": "recorded", "warning": None})
    out = capture.capture_rtl(rtl_run(True), tmp_path / "x.part", None, None)
    assert calls == [True] + [False] * capture.BIAS_OFF_TRIES
    assert "could not be switched off" in out["warning"] and "may still be on" in out["warning"]


def test_without_control_rtl_sdr_runs_untouched(switch_log, monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "_record_rtl", lambda *a: {"status": "recorded"})
    assert "bias_tee" not in capture.capture_rtl({"serial": "OBS-R02", "radio": {"bias_tee": False}}, tmp_path, None, None)
    assert switch_log == []


# ------------------------------------------------------------------ librtlsdr by serial
class FakeLib:
    def __init__(self, serials: list[str]) -> None:
        self.serials, self.calls = serials, []
        self.rtlsdr_get_device_usb_strings = lambda *a: self._strings(*a)   # ctypes sets argtypes on it

    def rtlsdr_get_device_count(self) -> int:
        return len(self.serials)

    def _strings(self, i, m, p, s):
        s.value = self.serials[i.value].encode()
        return 0

    def rtlsdr_open(self, dev, idx):
        self.calls.append(("open", idx.value))
        return 0

    def rtlsdr_set_bias_tee(self, dev, on):
        self.calls.append(("bias", on.value))
        return 0

    def rtlsdr_close(self, dev):
        self.calls.append(("close",))
        return 0


def test_set_by_serial_opens_only_that_receiver(monkeypatch):
    lib = FakeLib(["OBS-R02", "00000001", "OBS-R05"])
    monkeypatch.setattr(stepcapture.RtlDevice, "_load", staticmethod(lambda p=None: lib))
    assert stepcapture.set_rtl_bias_tee("00000001", True)["index"] == 1
    assert lib.calls == [("open", 1), ("bias", 1), ("close",)]


@pytest.mark.parametrize("serials", [["00000001", "00000001"], ["OBS-R02"]])
def test_set_by_serial_refuses_an_ambiguous_or_missing_serial(monkeypatch, serials):
    lib = FakeLib(serials)
    monkeypatch.setattr(stepcapture.RtlDevice, "_load", staticmethod(lambda p=None: lib))
    with pytest.raises(RuntimeError, match="not switching any bias tee"):
        stepcapture.set_rtl_bias_tee("00000001", True)
    assert lib.calls == []


def test_stepped_captures_switch_it_off_when_the_receiver_closes():
    lib = FakeLib([])
    d = stepcapture.RtlDevice.__new__(stepcapture.RtlDevice)
    d.lib, d.dev, d.applied, d.bias_off = lib, ctypes.c_void_p(1), {}, True
    d.close()
    assert lib.calls == [("bias", 0), ("close",)] and d.applied["bias_tee_off_at_close"]


# ------------------------------------------------------------------ Airspy, SDRplay and RTL detection
from aobs.inventory.store import hardware_bias_tee  # noqa: E402
from aobs.scheduling.hardware import applied_bias_tee, bias_tee_text  # noqa: E402
from test_inventory import add_airspy, inv, sysfs  # noqa: E402,F401


def test_hardware_bias_tee_by_model():
    assert hardware_bias_tee({"driver": "airspy", "capabilities": {"bias_tee_setting": "biastee"}})[0] is True
    assert hardware_bias_tee({"driver": "airspy", "capabilities": {}})[0] is None
    assert hardware_bias_tee({"driver": "sdrplay", "model": "RSP1B"})[0] is True
    assert hardware_bias_tee({"driver": "sdrplay", "model": "SDRplay RSP1A"})[0] is True
    assert hardware_bias_tee({"driver": "sdrplay", "model": "RSPdx"})[0] is True
    assert hardware_bias_tee({"driver": "sdrplay", "model": "RSP1"})[0] is False
    assert hardware_bias_tee({"driver": "rtlsdr", "manufacturer": "RTLSDRBlog", "model": "Blog V4"})[0] is True
    assert hardware_bias_tee({"driver": "rtlsdr", "manufacturer": "Realtek", "model": "RTL2838UHIDIR"})[0] is None


def test_a_web_added_airspy_r2_is_bias_tee_capable_and_switched_by_starlab(env, inv):
    add_airspy(inv, "RX-02")
    r = env.s.receivers["RX-02"]
    assert r.bias_tee_supported and r.bias_tee_setting == "biastee" and r.bias_tee_control
    assert r.capabilities(env.s.switch_ports)["bias_tee"]["schedulable_values"] == [False, True]
    e: list[str] = []
    validate_radio(r, {"center_frequency_hz": 137_900_000, "sample_rate_hz": 3_000_000, "bias_tee": True,
                       "gain_elements": {"LNA": 8, "MIX": 8, "VGA": 8}}, "soapysdr", "cs16", e, [])
    assert not [x for x in e if "bias" in x], e


def test_rx02_sawbird_path_no_longer_says_it_cannot_supply_bias_tee_power(env, inv):
    add_airspy(inv, "RX-02")
    lay = copy.deepcopy(env.s.rf.current["layout"])
    c = lay["components"]
    c["QFH"] = {"id": "QFH", "name": "QFH", "kind": "antenna", "status": "working", "power": "none"}
    c["FMBS"] = {"id": "FMBS", "name": "NOOELEC FM Bandstop", "kind": "filter", "status": "working", "power": "none"}
    c["SAWB"] = {"id": "SAWB", "name": "NOOELEC Sawbird NOAA", "kind": "amplifier", "status": "working",
                 "power": "bias_tee", "gain_db": 30}
    lay["receivers"].setdefault("RX-02", {"id": "RX-02"})["chain"] = ["QFH", "FMBS", "SAWB"]
    lay["receivers"]["RX-02"]["gain_profiles"] = [{"name": "default", "gain_mode": "manual",
                                                   "gain_elements": {"LNA": 8, "MIX": 8, "VGA": 8}}]
    _, errs, warns = rflayout.validate(lay, env.s)
    assert not errs, errs
    assert not [w for w in warns if "cannot supply bias-tee power" in w], warns
    assert any("RX-02 at LNA 8 / MIX 8 / VGA 8 may overload with ~30 dB of external LNA gain" in w for w in warns)
    assert bias_tee_plan(lay, env.s.receivers["RX-02"])["enable"]


def test_a_dc_block_between_receiver_and_lna_is_warned_in_the_editor(env, inv):
    add_airspy(inv, "RX-02")
    lay = copy.deepcopy(env.s.rf.current["layout"])
    c = lay["components"]
    c["QFH"] = {"id": "QFH", "name": "QFH", "kind": "antenna", "status": "working", "power": "none"}
    c["SAWB"] = {"id": "SAWB", "name": "Sawbird", "kind": "amplifier", "status": "working", "power": "bias_tee"}
    c["DCB"] = {"id": "DCB", "name": "DC block", "kind": "connector", "status": "working", "power": "none",
                "dc_pass": "no"}
    lay["receivers"].setdefault("RX-02", {"id": "RX-02"})["chain"] = ["QFH", "SAWB", "DCB"]
    _, _, warns = rflayout.validate(lay, env.s)
    assert 'RX-02: DC block is marked "Blocks DC" and sits between RX-02 and Sawbird, so Sawbird gets no bias-tee ' \
           'power' in warns


def test_overload_risk():
    r = types.SimpleNamespace(id="RX-02", driver="airspy")
    saw = [{"kind": "amplifier", "name": "Sawbird", "power": "bias_tee", "gain_db": 30, "status": "working"}]
    assert "may overload" in rflayout.overload_risk(r, saw, None, {"LNA": 8, "MIX": 8, "VGA": 8})
    assert rflayout.overload_risk(r, saw, None, {"LNA": 4, "MIX": 4, "VGA": 4}) is None
    assert rflayout.overload_risk(r, [], None, {"LNA": 8, "MIX": 8, "VGA": 8}) is None
    nogain = [{**saw[0], "gain_db": None}]
    assert "gain not entered" in rflayout.overload_risk(r, nogain, None, {"LNA": 8, "MIX": 8, "VGA": 8})


# ------------------------------------------------------------------ the Airspy recorder (SoapyAirspy "biastee")
class FakeAirspy:
    log: list[Any] = []
    stuck = False

    def __init__(self, args: str) -> None:
        self.args, self.state = args, "false"

    def writeSetting(self, k, v):
        FakeAirspy.log.append(("write", k, v))
        if not FakeAirspy.stuck:
            self.state = v

    def readSetting(self, k):
        return self.state


@pytest.fixture
def airspy(monkeypatch):
    soapy = types.ModuleType("SoapySDR")
    soapy.Device = FakeAirspy
    monkeypatch.setitem(__import__("sys").modules, "SoapySDR", soapy)
    FakeAirspy.log, FakeAirspy.stuck = [], False
    return FakeAirspy


def airspy_run(bias: bool) -> dict[str, Any]:
    return {"serial": "637862DC2E9678D7", "soapy_driver": "airspy", "bias_tee_control": True,
            "bias_tee_setting": "biastee", "radio": {"bias_tee": bias}}


def test_airspy_recorder_switches_biastee_on_reads_it_back_and_off_after(airspy, monkeypatch, tmp_path):
    seen = []

    def rec(sdr, run, part, start, end, bias):
        seen.append(sdr.readSetting("biastee"))
        return {"status": "recorded", "applied_settings": {}, "bias_tee": bias}
    monkeypatch.setattr(capture, "_record_soapy", rec)
    out = capture.capture_soapy(airspy_run(True), tmp_path / "x.part", None, None)
    assert seen == ["true"]                                               # on while recording
    assert airspy.log == [("write", "biastee", "true"), ("write", "biastee", "false")]
    assert out["bias_tee"]["applied"] is True and out["bias_tee"]["readback"] == "true"
    assert out["bias_tee"]["after"] == "false" and applied_bias_tee(out, {"bias_tee": True}) is True


def test_airspy_recorder_turns_it_off_when_the_recording_fails(airspy, monkeypatch, tmp_path):
    def rec(*a):
        raise RuntimeError("no samples")
    monkeypatch.setattr(capture, "_record_soapy", rec)
    with pytest.raises(RuntimeError):
        capture.capture_soapy(airspy_run(True), tmp_path / "x.part", None, None)
    assert airspy.log[-1] == ("write", "biastee", "false")


def test_airspy_off_jobs_write_off_explicitly(airspy, monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "_record_soapy", lambda sdr, run, p, s, e, bias: {"status": "recorded", "bias_tee": bias})
    out = capture.capture_soapy(airspy_run(False), tmp_path / "x.part", None, None)
    assert airspy.log == [("write", "biastee", "false")] and out["bias_tee"]["applied"] is False


def test_airspy_readback_mismatch_refuses_to_record(airspy, monkeypatch, tmp_path):
    airspy.stuck = True
    monkeypatch.setattr(capture, "_record_soapy", lambda *a: pytest.fail("must not record"))
    with pytest.raises(RuntimeError, match="reads back 'false'"):
        capture.capture_soapy(airspy_run(True), tmp_path / "x.part", None, None)


def test_airspy_without_control_never_touches_the_setting(airspy, monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "_record_soapy", lambda *a: {"status": "recorded"})
    capture.capture_soapy({"serial": "x", "soapy_driver": "airspy", "radio": {"bias_tee": False}}, tmp_path, None, None)
    assert airspy.log == []


def test_the_run_doc_carries_the_airspy_setting():
    from aobs.scheduling.backends import RecordingRequest
    req = RecordingRequest(job_id="m20261108120000abcdef", receiver_id="RX-02", host="local", driver="airspy",
                           serial="637862DC2E9678D7", radio={"center_frequency_hz": 1, "sample_rate_hz": 1,
                                                             "bias_tee": True}, format="cs16",
                           record_start="2026-11-08T12:00:00+00:00", record_end="2026-11-08T12:01:00+00:00",
                           raw="/data/x.cs16", log="/data/x.log", soapy_driver="airspy", bias_tee_control=True,
                           bias_tee_setting="biastee")
    req.validate()
    run = req.run_doc("/data/x.cs16", "/data/x.json")
    assert run["bias_tee_control"] is True and run["bias_tee_setting"] == "biastee"


# ------------------------------------------------------------------ "as applied" in metadata, plots and pages
def test_applied_bias_tee():
    assert applied_bias_tee({"bias_tee": {"requested": True, "applied": True}}, {"bias_tee": False}) is True
    assert applied_bias_tee({"bias_tee": {"requested": True, "applied": None, "error": "x"}}, {}) is None
    assert applied_bias_tee({"bias_tee": {"requested": True, "error": "x"}}, {}) is None          # older RTL results
    assert applied_bias_tee({"applied_settings": {"biasT_ctrl": "false"}}, {"bias_tee": True}) is False
    assert applied_bias_tee({}, {"bias_tee": True}) is True                                       # EEPROM, not switched
    assert bias_tee_text(None) == "unknown"


def test_the_plot_footer_shows_the_bias_tee_next_to_the_gain():
    from aobs.scheduling import analysis as an
    lines = an.caption_lines({"receiver_id": "RX-02", "center_frequency_hz": 137_500_000, "sample_rate_hz": 3_000_000,
                              "format": "cs16", "gain": {"LNA": 8, "MIX": 8, "VGA": 8}, "bias_tee": "on"})
    assert "gain LNA 8, MIX 8, VGA 8 · bias tee on" in lines[1]


def test_the_sidecar_records_the_bias_tee_as_applied(env, blab):
    started = run_test(env, blab, dict(BASE))
    job = env.s.get(started["job_id"])
    side = json.loads(pathlib.Path(job["paths"]["sidecar"]).read_text())
    cap = (job.get("result") or {}).get("capture") or {}
    assert side["bias_tee"] == bias_tee_text(applied_bias_tee(cap, job["spec"]["radio"]))
    assert side["bias_tee_requested"] is True
    d = blab.detail(started["test_id"])
    assert "bias_tee_applied" in d["job"]


# ------------------------------------------------------------------ the A/B test
def test_bias_ab_books_off_then_on_with_the_same_settings(env, blab):
    r = blab.start("OBS-R02", {**BASE, "bias_tee_ab": True}, "owner (web)")
    off, on = blab._row(r["off_test_id"]), blab._row(r["on_test_id"])
    jo, jn = env.s.get(off["job_id"]), env.s.get(on["job_id"])
    assert jo["spec"]["radio"]["bias_tee"] is False and jn["spec"]["radio"]["bias_tee"] is True
    assert jo["spec"]["metadata"]["bias_tee_ab"] == "off" and jn["spec"]["metadata"]["bias_tee_ab"] == "on"
    for k in ("center_frequency_hz", "sample_rate_hz", "gain_db"):
        assert jo["spec"]["radio"][k] == jn["spec"]["radio"][k]
    assert jn["record_start"] > jo["record_end"]
    assert blab._row(r["test_id"])["test_type"] == "bias_ab"
    d = blab.detail(r["test_id"])
    assert not d["progress"]["done"] and d["progress"]["label"].startswith("Bias tee OFF run")


def test_bias_ab_runs_both_halves_and_the_recorder_gets_off_then_on(env, blab):
    r = blab.start("OBS-R02", {**BASE, "bias_tee_ab": True}, "owner (web)")
    for tid in (r["off_test_id"], r["on_test_id"]):
        job = env.s.get(blab._row(tid)["job_id"])
        env.clock.now = dt.datetime.fromisoformat(job["reservation_start"]) + dt.timedelta(seconds=1)
        blab.runner.tick(threaded=False)
        for _ in range(10):
            blab.proc.step()
    assert [x["radio"]["bias_tee"] for x in blab.hw.runs[-2:]] == [False, True]
    assert all(x["bias_tee_control"] for x in blab.hw.runs[-2:])
    d = blab.detail(r["test_id"])
    assert d["progress"]["done"] and d["result"]["noise_floor_change_db"] is not None


def test_bias_ab_refuses_a_path_with_nothing_to_power(env, blab):
    lay = copy.deepcopy(env.s.rf.current["layout"])
    lay["receivers"]["OBS-R02"]["chain"] = ["DISCONE"]
    env.s.rf.save(lay, env.s.rf.current["version"], "owner (web)")
    with pytest.raises(Exception, match="only put DC on the antenna"):
        blab.start("OBS-R02", {**BASE, "bias_tee_ab": True}, "owner (web)")


def test_bias_ab_refuses_receivers_starlab_does_not_switch(env, lab):
    with pytest.raises(Exception, match="cannot switch"):
        lab.start("OBS-R02", {**BASE, "bias_tee_ab": True}, "owner (web)")


def _side(floor, spread, applied, clip=0.0):
    return {"test": {"test_id": "t"}, "progress": {"phase": "complete", "label": "Complete", "done": True},
            "job": {"job_id": "j", "radio": {"gain_db": 20.7}, "bias_tee_applied": applied},
            "metrics": {"summary": {"noise_floor_dbfs_per_hz": floor, "clipped_fraction": clip},
                        "iq": {"sample_spread_lsb": spread}}}


@pytest.mark.parametrize("rise,code", [(24.0, "powered"), (10.0, "weak"), (1.2, "lna_not_working")])
def test_bias_ab_verdicts(rise, code):
    from aobs.scheduling.testlab import bias_ab_compare
    r = bias_ab_compare(_side(-120.0, 4.0, False), _side(-120.0 + rise, 40.0, True))
    assert r["verdict_code"] == code and r["noise_floor_change_db"] == rise and r["spread_change_lsb"] == 36.0
    if code == "lna_not_working":
        assert r["verdict"].startswith("LNA not powered or not working")


def test_bias_ab_flags_a_tee_that_was_not_actually_on():
    from aobs.scheduling.testlab import bias_ab_compare
    r = bias_ab_compare(_side(-120.0, 4.0, False), _side(-119.0, 4.0, None, clip=0.01))
    assert any("ON run's bias tee was applied as unknown" in w for w in r["warnings"])
    assert any("clipped" in w for w in r["warnings"])
