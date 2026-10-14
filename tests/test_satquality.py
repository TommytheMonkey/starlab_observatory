from __future__ import annotations

import datetime as dt
import pathlib
import shutil
import sys
from typing import Any

import numpy as np
import pytest

from aobs.scheduling import satquality as sq
from aobs.scheduling.store import SchedulerError
from test_scheduler import env  # noqa: F401
from test_testlab import BASE, lab as _lab_fixture, run_test  # noqa: F401

LOG_GOOD = """\x1b[32m[10:10:20 - 14/10/2026] (I) Progress 10.0%, SNR : 6.2dB, Peak SNR: 9.8dB\x1b[m
[10:10:20 - 14/10/2026] (I) Progress 10.0%, Viterbi : SYNCED BER : 0.012000, Deframer : SYNCED
[10:10:21 - 14/10/2026] (I) Progress 20.0%, SNR : 7.0dB, Peak SNR: 10.1dB
[10:10:21 - 14/10/2026] (I) Progress 20.0%, Viterbi : NOSYNC BER : 0.405273, Deframer : NOSYNC
[10:10:24 - 14/10/2026] (I) MSU-MR Channel 1 Lines  : 272
[10:10:24 - 14/10/2026] (I) MSU-MR Channel 2 Lines  : 272
[10:10:24 - 14/10/2026] (I) MSU-MR Channel 3 Lines  : 0
"""

FAKE_SATDUMP = r'''#!PYTHON
import os, sys, pathlib
if sys.argv[1] == "version":
    print("[00:00:00] (I) This is SatDump v1.2.3"); sys.exit(0)
out = pathlib.Path(sys.argv[4]); out.mkdir(parents=True, exist_ok=True)
mode = os.environ.get("FAKE_SATDUMP_MODE", "none")
if mode == "good" and "+0Hz" not in str(out) and "_" in out.name:
    mode = "none"            # only the on-frequency run of the search locks
if mode == "good":
    print("[x] (I) Progress 50.0%, SNR : 7.5dB, Peak SNR: 11.0dB")
    print("[x] (I) Progress 50.0%, Viterbi : SYNCED BER : 0.010000, Deframer : SYNCED")
    print("[x] (I) MSU-MR Channel 1 Lines  : 304"); print("[x] (I) MSU-MR Channel 2 Lines  : 304")
    (out / "meteor_m2-x_lrpt.cadu").write_bytes(b"\x1a" * 1024 * 40)
    import numpy as np
    from PIL import Image
    d = out / "MSU-MR"; d.mkdir(exist_ok=True)
    Image.fromarray((np.random.default_rng(0).random((304, 1568)) * 255).astype("uint8")).save(d / "msu_mr_1.png")
else:
    print("[x] (I) Progress 50.0%, SNR : 0.2dB, Peak SNR: 0.9dB")
    print("[x] (I) Progress 50.0%, Viterbi : NOSYNC BER : 0.405273, Deframer : NOSYNC")
    print("[x] (I) MSU-MR Channel 1 Lines  : 0")
    (out / "meteor_m2-x_lrpt.cadu").write_bytes(b"")
sys.exit(1)                 # this SatDump build exits 1 even after good decodes
'''


@pytest.fixture
def satlab(env, _lab_fixture, tmp_path, monkeypatch):
    f = tmp_path / "satdump"
    f.write_text(FAKE_SATDUMP.replace("#!PYTHON", f"#!{sys.executable}"))
    f.chmod(0o755)
    env.s.settings["satdump_path"] = str(f)
    env.s.settings["satdump_nice"] = 0
    return _lab_fixture


def test_log_parser_reports_only_what_satdump_prints():
    m = sq.parse_satdump_log(LOG_GOOD)
    assert m["demod_snr_db_max"] == 7.0 and m["peak_snr_db"] == 10.1
    assert m["viterbi_sync_fraction"] == 0.5 and m["viterbi_ber_min"] == 0.012
    assert m["deframer_sync_fraction"] == 0.5 and m["image_lines_max"] == 272 and m["channels_with_lines"] == [1, 2]
    empty = sq.parse_satdump_log("")
    assert empty["viterbi_sync_fraction"] is None and empty["image_lines_max"] is None


def test_ladder_keeps_rungs_separate_and_never_treats_failure_as_absence():
    prof = sq.SAT_PROFILES["METEOR-M2-4"]
    f = np.linspace(137.6e6 - 1.2e6, 137.6e6 + 1.2e6, 1024)
    mean = np.full(1024, -60.0)
    mean[np.abs(f - 137.9e6) <= 60_000] = -50.0
    rf = {"summary": {"channel": {"measured": True, "power_over_noise_db": 9.0, "bandwidth_hz": 150_000,
                                  "target_frequency_hz": 137_900_000}},
          "spectrum": {"freqs_hz": list(f), "mean_dbfs": list(mean), "noise_floor_dbfs": -60.0}}
    lad = sq.ladder(prof, rf, [])
    st = {x["level"]: x["status"] for x in lad["levels"]}
    assert st["RF energy detected"] == "yes" and st["Modulation potentially present"] == "possible"
    assert st["Valid frames decoded"] == "not tested" and lad["highest"] == "Modulation potentially present"
    failed = {"cadu_frames": 0, "images": 0, "sat": sq.parse_satdump_log("[x] Viterbi : NOSYNC BER : 0.4, Deframer : NOSYNC")}
    lad = sq.ladder(prof, rf, [failed])
    assert {x["level"]: x["status"] for x in lad["levels"]}["Synchronization achieved"] == "no"
    assert "does not prove" in lad["notes"][0]
    nolog = sq.ladder(prof, rf, [{"cadu_frames": 0, "images": 0, "sat": sq.parse_satdump_log("")}])
    assert {x["level"]: x["status"] for x in nolog["levels"]}["Synchronization achieved"] == sq.NOT_REPORTED


@pytest.mark.skipif(shutil.which("satdump") is None, reason="SatDump not installed")
def test_installed_satdump_is_detected_not_assumed():
    info = sq.satdump_info()
    assert info["version"] and "meteor_m2-x_lrpt" in info["pipelines"]


def test_existing_recording_decodes_with_metrics_and_experimental_variant(env, satlab, monkeypatch):
    monkeypatch.setenv("FAKE_SATDUMP_MODE", "good")
    rec = env.s.get(satlab.detail(run_test(env, satlab, BASE)["test_id"])["job"]["job_id"])["job_id"]
    r = satlab.satellite_existing({"recording_id": rec, "satellite": "METEOR-M2-4", "try_conjugate": True}, "owner (web)")
    for _ in range(40):
        satlab.proc.step()
    d = satlab.detail(r["test_id"])
    assert d["progress"]["done"], d["progress"]
    sat = d["satellite"]
    std = next(x for x in sat["decodes"] if x["variant"] == "standard")
    assert std["cadu_frames"] == 40 and std["sat"]["image_lines_max"] == 304 and std["sat"]["viterbi_sync_fraction"] == 1.0
    assert std["image_artifacts"] and std["log_artifacts"]
    assert any(x["variant"].startswith("EXPERIMENTAL") for x in sat["decodes"])
    st = {x["level"]: x["status"] for x in sat["ladder"]["levels"]}
    assert st["Image data recovered"] == "yes" and sat["ladder"]["highest"] == "Image data recovered"
    assert satlab.list(test_type="satellite")[0]["test_id"] == r["test_id"]


def test_failed_decode_is_reported_without_claiming_no_signal(env, satlab, monkeypatch):
    monkeypatch.setenv("FAKE_SATDUMP_MODE", "none")
    rec = satlab.detail(run_test(env, satlab, BASE)["test_id"])["job"]["job_id"]
    r = satlab.satellite_existing({"recording_id": rec}, "owner (web)")
    for _ in range(30):
        satlab.proc.step()
    lad = satlab.detail(r["test_id"])["satellite"]["ladder"]
    st = {x["level"]: x["status"] for x in lad["levels"]}
    assert st["Valid frames decoded"] == "no" and st["Synchronization achieved"] == "no"
    assert st["RF energy detected"] == "yes" and "does not prove" in lad["notes"][0]


def _lrpt(path: pathlib.Path, seconds: float, level: float, rate: int = 1_024_000, center: int = 137_600_000,
          fmt: str = "cu8") -> None:
    rng = np.random.default_rng(int(level * 10))
    n = int(seconds * rate)
    x = rng.normal(0, 3, n) + 1j * rng.normal(0, 3, n)
    band = rng.normal(0, 1, n) + 1j * rng.normal(0, 1, n)
    spec = np.fft.fft(band)
    f = np.fft.fftfreq(n, 1 / rate)
    spec[np.abs(f - 300_000) > 60_000] = 0                       # ~120 kHz-wide signal at +300 kHz
    x += level * np.fft.ifft(spec) * np.sqrt(n / (np.abs(f - 300_000) <= 60_000).sum())
    if fmt == "cu8":
        iq = np.empty(2 * n)
        iq[0::2], iq[1::2] = x.real + 127.5, x.imag + 127.5
        path.write_bytes(iq.clip(0, 255).round().astype(np.uint8).tobytes())
    else:
        iq = np.empty(2 * n)
        iq[0::2], iq[1::2] = x.real * 300, x.imag * 300
        path.write_bytes(iq.round().astype("<i2").tobytes())


def test_pass_comparison_aligns_in_time_and_uses_relative_contrast(tmp_path):
    t0 = dt.datetime(2026, 10, 14, 8, 44, tzinfo=dt.timezone.utc)
    a, b = tmp_path / "a.cu8", tmp_path / "b.cs16"
    _lrpt(a, 12, 4.0)
    _lrpt(b, 10, 1.0, fmt="cs16")
    common = {"sample_rate_hz": 1_024_000, "center_frequency_hz": 137_600_000}
    ins = [{"recording_id": "rA", "receiver_id": "OBS-R01", "path": str(a), "format": "cu8", "actual_start": t0.isoformat(), **common},
           {"recording_id": "rB", "receiver_id": "RSP1B", "path": str(b), "format": "cs16",
            "actual_start": (t0 + dt.timedelta(seconds=3)).isoformat(), **common}]
    s, arts = sq.sat_compare(ins, {"recordings": ["rA", "rB"], "target_frequency_hz": 137_900_000,
                                   "channel_bandwidth_hz": 150_000, "block_s": 0.25}, tmp_path)
    assert 8.5 <= s["overlap_s"] <= 9.5                         # only the shared part of the pass
    pa, pb = s["per_recording"]["rA"], s["per_recording"]["rB"]
    assert pa["contrast_median_db"] > pb["contrast_median_db"] > 0
    import json
    data = json.loads((tmp_path / "sat_compare.json").read_text())
    assert data["series"]["rA"]["spectrum_relative"]["db_over_floor"]
    with pytest.raises(ValueError, match="do not overlap"):
        sq.sat_compare([ins[0], {**ins[1], "actual_start": (t0 + dt.timedelta(hours=1)).isoformat()}],
                       {"recordings": ["rA", "rB"], "target_frequency_hz": 137_900_000}, tmp_path)


def test_unknown_satellite_is_refused(env, satlab):
    with pytest.raises(SchedulerError, match="satellite must be one of"):
        satlab.start("OBS-R02", {**BASE, "test_type": "satellite", "satellite": "NOAA-99"}, "owner (web)")


def test_pass_comparison_through_the_lab_reports_relative_measures(env, satlab, monkeypatch):
    monkeypatch.setenv("FAKE_SATDUMP_MODE", "none")
    ids = []
    for amp in (8.0, 2.0):
        satlab.hw.tone = amp
        ids.append(satlab.detail(run_test(env, satlab, BASE)["test_id"])["job"]["job_id"])
    t0 = "2026-10-14T08:44:00+00:00"
    for jid in ids:                                    # make them two receivers' views of the same pass
        job = env.s.get(jid)
        res = job["result"]
        res["capture"]["actual_start"] = t0
        env.s.update(jid, result=res)
    r = satlab.sat_compare({"recording_a": ids[0], "recording_b": ids[1], "satellite": "METEOR-M2-4"}, "owner (web)")
    for _ in range(60):
        satlab.proc.step()
    d = satlab.detail(r["test_id"])
    assert d["progress"]["done"], d["progress"]
    sc = d["sat_compare"]
    assert sc["data"]["overlap"]["seconds"] > 2
    assert any("Only relative measures" in w for w in sc["report"]["warnings"])
    assert any("never proves" in h for h in sc["report"]["hypotheses"])
    assert satlab.list(test_type="sat_compare")[0]["test_id"] == r["test_id"]
