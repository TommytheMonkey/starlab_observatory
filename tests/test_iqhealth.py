"""RTL-SDR IQ health, tuner start-up logs, receiver inventory, and artifact retrieval over HTTP."""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import numpy as np
import pytest

from aobs.scheduling import iqhealth, rtllog
from test_scheduler import (NOW, FakeHandle, FakeRunnerHw, drain, env, planner, rtl_job, run_job,  # noqa: F401
                            submit)

RATE = 2_400_000


def cu8(path: pathlib.Path, i: np.ndarray, q: np.ndarray) -> pathlib.Path:
    iq = np.empty(2 * len(i), np.uint8)
    iq[0::2], iq[1::2] = i, q
    path.write_bytes(iq.tobytes())
    return path


def noise(n: int, std: float, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    f = lambda: np.clip(np.round(rng.normal(127.5, std, n)), 0, 255).astype(np.uint8)  # noqa: E731
    return f(), f()


def stuck(n: int, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """What OBS-R02 produced before its USB reset: codes 127/128 (an occasional 126), ~0.5 LSB."""
    rng = np.random.default_rng(seed)
    f = lambda: rng.choice(np.array([126, 127, 128], np.uint8), n, p=[0.01, 0.5, 0.49])  # noqa: E731
    return f(), f()


N = RATE * 2          # 2 s


# ---------------------------------------------------------------- IQ statistics
def test_healthy_cu8_recording(tmp_path):
    p = cu8(tmp_path / "ok.cu8", *noise(N, 9.4))
    h = iqhealth.assess(p, "cu8", RATE, expected_bytes=2 * N, gain_db=8.7)
    assert h["status"] == "healthy" and h["healthy"] and not h["alert"]
    assert h["rms_dbfs"] == pytest.approx(-19.65, abs=0.3)          # the user's post-reset measurement
    assert h["sample_spread_lsb"] == pytest.approx(9.4, abs=0.2)
    assert h["i_unique_codes"] > 50 and h["clipped_fraction"] == 0
    assert abs(h["dc_offset_i_lsb"]) < 0.2 and h["repeated_sample_fraction"] < 0.01
    assert h["samples"] == N and h["duration_s"] == pytest.approx(2.0) and h["fraction_of_expected"] == 1.0


def test_nearly_constant_iq_is_suspicious_flat_and_alerts(tmp_path):
    p = cu8(tmp_path / "flat.cu8", *stuck(N))
    h = iqhealth.assess(p, "cu8", RATE, expected_bytes=2 * N, gain_db=38.6)
    assert h["status"] == "suspicious_flat" and h["alert"] and not h["healthy"]
    assert h["rms_dbfs"] == pytest.approx(-45.1, abs=0.3) and h["sample_spread_lsb"] < 0.6
    assert h["i_unique_codes"] <= 3 and h["repeated_sample_fraction"] > 0.2 and h["flat_block_fraction"] == 1.0
    text = iqhealth.alert_text("OBS-R02", "OBS-R02", "m1", h)
    for s in ("WARNING: OBS-R02 — POSSIBLE RECEIVER FAILURE", "completed successfully", "abnormally low variation",
              "Receiver serial: OBS-R02", "Recording: m1", "Requested gain: 38.6 dB", "Measured RMS: -4",
              "ADC sample spread: 0.", "Unique sample values: I 3", "Recommended:", "Do not reset"):
        assert s in text, s


def test_low_level_alone_is_not_a_failure(tmp_path):
    weak = iqhealth.assess(cu8(tmp_path / "weak.cu8", *noise(N, 2.0)), "cu8", RATE, gain_db=0.9)
    assert weak["status"] == "weak_signal" and not weak["alert"]           # quiet but varying normally
    low_gain = iqhealth.assess(cu8(tmp_path / "lowgain.cu8", *stuck(N)), "cu8", RATE, gain_db=0.0)
    assert low_gain["status"] == "weak_signal" and not low_gain["alert"]   # flat at 0 dB gain is not proof
    assert any("very low" in e for e in low_gain["evidence"])


def test_stall_part_way_through_is_flagged(tmp_path):
    i, q = noise(N, 9.0)
    si, sq = stuck(N)
    i[N // 2:], q[N // 2:] = si[N // 2:], sq[N // 2:]
    h = iqhealth.assess(cu8(tmp_path / "stall.cu8", i, q), "cu8", RATE, gain_db=29.7)
    assert h["status"] == "suspicious_flat" and 0.3 < h["flat_block_fraction"] < 0.7
    assert any("only in part" in e for e in h["evidence"])


def test_clipped_iq_is_possible_overload(tmp_path):
    h = iqhealth.assess(cu8(tmp_path / "hot.cu8", *noise(N, 70)), "cu8", RATE, gain_db=49.6)
    assert h["status"] == "possible_overload" and h["clipped_fraction"] > 0.01 and not h["alert"]


def test_truncated_empty_missing_and_zero_files_are_capture_failures(tmp_path):
    p = cu8(tmp_path / "short.cu8", *noise(N // 2, 9))
    h = iqhealth.assess(p, "cu8", RATE, expected_bytes=2 * N)
    assert h["status"] == "capture_failure" and "truncated" in h["summary"] and h["alert"]
    odd = tmp_path / "odd.cu8"
    odd.write_bytes(p.read_bytes() + b"\x80")
    h = iqhealth.assess(odd, "cu8", RATE, expected_bytes=N + 1)
    assert h["status"] == "healthy" and any("half a sample" in x for x in h["issues"])
    (tmp_path / "empty.cu8").write_bytes(b"")
    assert iqhealth.assess(tmp_path / "empty.cu8", "cu8", RATE)["status"] == "capture_failure"
    assert iqhealth.assess(tmp_path / "missing.cu8", "cu8", RATE)["status"] == "capture_failure"
    (tmp_path / "zero.cu8").write_bytes(bytes(2 * N))
    z = iqhealth.assess(tmp_path / "zero.cu8", "cu8", RATE)
    assert z["status"] == "capture_failure" and "zero bytes" in z["summary"]


def test_non_cu8_is_not_judged(tmp_path):
    (tmp_path / "x.cs16").write_bytes(bytes(4096))
    assert iqhealth.assess(tmp_path / "x.cs16", "cs16", RATE)["applicable"] is False


def test_gain_response_confirms_or_clears(tmp_path):
    a = iqhealth.assess(cu8(tmp_path / "a.cu8", *stuck(N, 1)), "cu8", RATE, gain_db=8.7)
    b = iqhealth.assess(cu8(tmp_path / "b.cu8", *stuck(N, 2)), "cu8", RATE, gain_db=38.6)
    gr = iqhealth.gain_response(b, [{"recording_id": "a", "requested_gain_db": 8.7, "rms_dbfs": a["rms_dbfs"]}])
    assert gr and not gr["responds_to_gain"] and "no gain response" in gr["note"]
    gr = iqhealth.gain_response({"requested_gain_db": 20.7, "rms_dbfs": -14.15},
                                [{"recording_id": "x", "requested_gain_db": 8.7, "rms_dbfs": -19.65}])
    assert gr["responds_to_gain"]
    assert iqhealth.gain_response({"requested_gain_db": 10, "rms_dbfs": -40},
                                  [{"recording_id": "y", "requested_gain_db": 12, "rms_dbfs": -40}]) is None


# ---------------------------------------------------------------- tuner start-up log
REAL_LOG = """exec: /usr/bin/rtl_sdr -d OBS-R02 -f 137500000 -s 2400000 -g 38.6 -p 0 /data/x.cu8.part
Found 3 device(s):
  0:  Nooelec, NESDR SMArt v5, SN: OBS-R05
  1:  Realtek, RTL2838UHIDIR, SN: OBS-R01
  2:  Realtek, RTL2838UHIDIR, SN: OBS-R02
Using device 2: Generic RTL2832U OEM
Found Rafael Micro R820T tuner
Enabled direct sampling mode, input 2
Sampling at 2400000 S/s.
[R82XX] PLL not locked!
Disabled direct sampling mode
Tuned to 137500000 Hz.
Tuner gain set to 38.60 dB.
Reading samples in async mode...
Signal caught, exiting!
"""


def test_tuner_log_parsing_of_a_real_start_up():
    d = rtllog.parse(REAL_LOG, "OBS-R02")
    assert d["selected_serial"] == "OBS-R02" and d["tuner"] == "Rafael Micro R820T"
    assert d["requested"] == {"executable": "/usr/bin/rtl_sdr", "device": "OBS-R02", "frequency_hz": 137500000,
                              "sample_rate_hz": 2400000, "gain_db": 38.6, "ppm": 0}
    assert d["tuned_frequency_hz"] == 137500000 and d["reported_sample_rate_hz"] == 2400000
    assert d["reported_gain_db"] == 38.6 and d["direct_sampling_final"] == "disabled"
    assert d["pll_warnings"] == 1 and d["pll_warnings_after_tune"] == 0
    assert d["status"] == "ok_transient_pll_warning" and not d["fault"]       # a single start-up warning is normal


def test_tuner_log_faults():
    late = REAL_LOG.replace("Reading samples", "[R82XX] PLL not locked!\nReading samples")
    assert rtllog.parse(late, "OBS-R02")["status"] == "tuning_failure_suspected"
    assert rtllog.parse(REAL_LOG, "OBS-R01")["status"] == "wrong_device"
    no_tune = "\n".join(ln for ln in REAL_LOG.splitlines() if not ln.startswith("Tuned"))
    assert rtllog.parse(no_tune, "OBS-R02")["status"] == "tuning_failure_suspected"
    stuck_ds = REAL_LOG.replace("Disabled direct sampling mode\n", "")
    assert rtllog.parse(stuck_ds, "OBS-R02")["status"] == "tuning_failure_suspected"
    fail = "exec: /usr/bin/rtl_sdr -d OBS-R09 x\nNo matching devices found.\n"
    assert rtllog.parse(fail, "OBS-R09")["status"] == "open_failed"
    near = REAL_LOG.replace("-g 38.6", "-g 38.0")
    assert any("nearest supported step" in e for e in rtllog.parse(near, "OBS-R02")["evidence"])


def test_only_repeated_tuning_failures_are_persistent():
    bad = {"status": "tuning_failure_suspected"}
    ok = {"status": "ok_transient_pll_warning"}
    assert not rtllog.persistent_tuning_failure([ok, ok, bad])
    assert not rtllog.persistent_tuning_failure([bad, ok, bad])
    assert rtllog.persistent_tuning_failure([ok, bad, bad, bad])


# ---------------------------------------------------------------- end to end: capture -> health -> Slack
class FlatHw(FakeRunnerHw):
    """A capture that 'succeeds' with stuck samples, like OBS-R02 before its USB reset."""

    def spawn(self, argv, log):
        run = json.loads(pathlib.Path(argv[-1]).read_text())
        self.calls.append(("spawn", run["serial"]))
        raw = pathlib.Path(run["raw"])
        raw.parent.mkdir(parents=True, exist_ok=True)
        cu8(raw, *stuck(RATE))
        pathlib.Path(log).write_text(REAL_LOG.replace("38.6", str(run["radio"]["gain_db"]))
                                     .replace("38.60", f"{run['radio']['gain_db']:.2f}"))
        pathlib.Path(run["result"]).write_text(json.dumps(
            {"status": "recorded", "bytes": raw.stat().st_size, "expected_bytes": raw.stat().st_size,
             "actual_start": run["record_start"]}))
        return FakeHandle(4242)


def test_flat_recording_is_stored_alerted_and_never_reported_healthy(env):
    jid, runner = run_job(env, FlatHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    job = env.s.get(jid)
    cap = job["result"]["capture"]
    assert cap["iq_health"]["status"] == "suspicious_flat"
    assert cap["tuner"]["status"] == "ok_transient_pll_warning" and cap["tuner"]["reported_gain_db"] == 33.8
    assert "IQ health: suspiciously flat" in job["status_reason"]
    health_file = pathlib.Path(job["paths"]["raw"] + ".health.json")
    assert json.loads(health_file.read_text())["iq_health"]["status"] == "suspicious_flat"
    assert any(a["path"] == str(health_file) and a["kind"] == "diagnostics" for a in runner.proc.artifacts(jid))
    with env.s.engine.connect() as conn:
        from aobs.db.schema import sched_notifications
        notes = [dict(r) for r in conn.execute(sched_notifications.select()).mappings()]
    alert = [n for n in notes if n["kind"] == "health"]
    assert len(alert) == 1 and "POSSIBLE RECEIVER FAILURE" in alert[0]["text"] and "Requested gain: 33.8 dB" in alert[0]["text"]
    done = [n for n in notes if n["kind"] == "completed"]
    assert done and "NOT healthy" in done[0]["text"]
    from aobs.scheduling.rxhealth import latest
    lt = latest(env.s, "OBS-R02")
    assert lt["status"] == "suspicious_flat" and lt["recording_id"] == jid
    from aobs.scheduling.health import health
    chk = next(c for c in health(env.s)["checks"] if c["check"] == "OBS-R02 IQ health")
    assert chk["status"] == "FAIL" and "reconnect its USB" in chk["action"]


# ---------------------------------------------------------------- receiver inventory
def test_receiver_inventory_matches_the_physical_rf_configuration():
    from aobs.config import load_config
    from aobs.scheduling.hardware import load_receivers, validate_radio
    cfg = load_config(pathlib.Path(__file__).resolve().parents[1] / "config" / "observatory.toml")
    rx = load_receivers(cfg)
    assert {r.id: r.serial for r in rx.values()} == {"OBS-R01": "OBS-R01", "OBS-R02": "OBS-R02", "OBS-R05": "OBS-R05",
                                                    "RSP1B": "240526D360"}
    r01 = rx["OBS-R01"]
    assert r01.antenna == "V-DIPOLE-COPPER" and "Sawbird NOAA" in r01.signal_path and r01.amplifier_power == "external"
    assert not r01.bias_tee_supported and r01.auto["gain_db"] == 8.7
    ants = cfg.section("antennas")
    assert ants["QFH-137"]["description"] == "Diamond DP-KE137 QFH" and "hardwired_receiver" not in ants["QFH-137"]
    assert [k for k, v in ants.items() if v.get("hardwired_receiver") == "OBS-R01"] == ["V-DIPOLE-COPPER"]
    r02 = rx["OBS-R02"]
    assert r02.antenna == "DISCONE" and "LaNA" in r02.amplifier and r02.amplifier_power == "external"
    assert r02.signal_path == "Discone > Nooelec LaNA (externally powered) > OBS-R02" and not r02.bias_tee_supported
    errs: list[str] = []
    validate_radio(r02, {"bias_tee": True}, "rtl_sdr", "cu8", errs, [])
    assert any("bias_tee must stay false" in e for e in errs)                  # never power the LaNA from the dongle
    assert rx["RSP1B"].antenna == "QFH-137" and rx["RSP1B"].filters == ("AM bandstop", "FM bandstop")
    assert rx["OBS-R05"].filters == ("FM bandstop",) and rx["OBS-R05"].antenna == "ANTSW-01"
    errs = []
    validate_radio(rx["OBS-R01"], {"bias_tee": True}, "rtl_sdr", "cu8", errs, [])
    assert errs                                                                 # never power the SAWbird from R01
    assert rx["OBS-R05"].switched
    for r in rx.values():
        assert r.signal_path.endswith(r.id)
        caps = r.capabilities({})
        assert caps["serial"] == r.serial and caps["rf_chain"]["amplifier_power"] == (r.amplifier_power or None)


# ---------------------------------------------------------------- artifact retrieval over HTTP
@pytest.fixture
def api(env):
    import uvicorn

    from aobs.coordination.api import create_app
    from aobs.coordination.broker import Broker
    from aobs.scheduling.runner import Runner
    from aobs.scheduling.service import SchedulerService
    from test_broker import FakeHw

    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    env.cfg.raw["broker"] = {"receivers": ["OBS-R05"]}
    svc = SchedulerService(env.s, planner(env, []), Runner(env.s, FakeRunnerHw()), processor=runner.proc)
    svc.start = lambda: None
    app = create_app(Broker(env.cfg, FakeHw(), env.clock), "t", scheduler=svc)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)

    def call(path: str) -> tuple[int, Any]:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Authorization": "Bearer t"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")
    try:
        yield jid, runner.proc, call, port
    finally:
        server.should_exit = True


def test_capture_log_metadata_results_and_satdump_log_are_readable(api, env):
    jid, proc, call, _ = api
    job = env.s.get(jid)
    pathlib.Path(job["paths"]["log"]).write_text(REAL_LOG)          # the fake capture writes no log; add one
    # a SatDump log in a different directory from the recording, like real decode attempts
    sd = env.tmp / "processing" / jid / "p1" / "satdump"
    sd.mkdir(parents=True)
    (sd / "satdump.log").write_text("[16:07:36] (I) Deframer state: SYNCED\n[16:07:36] (I) SNR 3.1 dB\n")
    proc.index(jid, None, [{"kind": "capture_log", "path": job["paths"]["log"], "label": "capture log"}])
    proc.index(jid, None, [{"kind": "log", "path": str(sd / "satdump.log"), "label": "SatDump log"}])
    arts = {a["kind"] + ":" + pathlib.Path(a["path"]).name: a["artifact_id"] for a in proc.artifacts(jid)}
    name = pathlib.Path(job["paths"]["raw"]).name
    want = {f"capture_log:{name}.log": "Tuned to 137500000", f"metadata:{name}.json": '"job_id"',
            "log:satdump.log": "Deframer", "diagnostics:result.json": '"ok"',
            f"diagnostics:{name}.health.json": '"iq_health"'}
    for key, needle in want.items():
        code, body = call(f"/v1/artifacts/{arts[key]}/text?tail_lines=2000")
        assert code == 200, (key, body)
        assert needle in body["text"] and body["recording_id"] == jid, key
    code, body = call(f"/v1/artifacts/{arts['log:satdump.log']}/text?contains=snr")
    assert code == 200 and body["matching_lines"] == 1


def test_artifact_errors_are_specific_and_traversal_is_refused(api, env):
    jid, proc, call, port = api
    code, body = call("/v1/artifacts/f20990101000000abcdef/text")
    assert code == 404 and "in the index" in body["error"]
    code, body = call("/v1/artifacts/not-an-id/text")
    assert code == 400 and "not an artifact id" in body["error"]
    code, _ = call("/v1/artifacts/..%2F..%2Fetc%2Fpasswd/text")
    assert code in (400, 404)
    # a file deleted behind the index's back
    gone = env.tmp / "gone.log"
    gone.write_text("x")
    proc.index(jid, None, [{"kind": "capture_log", "path": str(gone), "label": "x"}])
    aid = next(a["artifact_id"] for a in proc.artifacts(jid) if a["path"] == str(gone))
    gone.unlink()
    code, body = call(f"/v1/artifacts/{aid}/text")
    assert code == 410 and "no longer exists" in body["error"]
    # an indexed path that is a symlink out of the allowed roots
    outside = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / f"outside-{os.getpid()}.log"
    outside.write_text("secret")
    try:
        link = env.tmp / "link.log"
        link.symlink_to(outside)
        proc.index(jid, None, [{"kind": "capture_log", "path": str(link), "label": "x"}])
        aid = next(a["artifact_id"] for a in proc.artifacts(jid) if a["path"] == str(link))
        code, body = call(f"/v1/artifacts/{aid}/text")
        assert code == 403 and "outside" in body["error"]
        code, _ = call(f"/v1/artifacts/{aid}/file")
        assert code == 403
    finally:
        outside.unlink()
    raw = proc.artifacts(jid, kind="raw_iq")[0]["artifact_id"]
    code, body = call(f"/v1/artifacts/{raw}/text")
    assert code == 415


def test_bot_explains_a_missing_broker_route(api):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "agent"))
    from obsagent.broker_client import BrokerClient, BrokerError
    _, _, _, port = api
    b = BrokerClient(f"http://127.0.0.1:{port}", "t")
    with pytest.raises(BrokerError, match="no such endpoint.*restart observatory-broker"):
        b.get("/v1/artifacts/f20261013160737b17fb4/nonexistent-route")
    with pytest.raises(BrokerError, match="in the index") as exc:
        b.get("/v1/artifacts/f20990101000000abcdef/text")
    assert exc.value.status == 404
