"""Analysis engine on synthetic IQ: axes, detection, drift, clipping, noise floor, integrity, compare."""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from aobs.scheduling import analysis as an
from aobs.scheduling.analyze_run import analyze, compare
from test_dsp import write

RATE = 2_000_000
CENTER = 137_500_000


def make(tmp: pathlib.Path, x: np.ndarray, fmt: str = "cs16", name: str = "x", **meta) -> dict:
    p = tmp / f"{name}.{fmt}"
    write(p, x, fmt)
    return {"path": str(p), "format": fmt, "sample_rate_hz": RATE, "center_frequency_hz": CENTER, **meta}


def noise(n: int, level: float = 0.01, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return level * (rng.normal(size=n) + 1j * rng.normal(size=n))


def test_frequency_and_time_axes_are_accurate(tmp_path):
    n = RATE * 2
    t = np.arange(n) / RATE
    x = noise(n)
    burst = (t >= 0.8) & (t < 1.2)
    x[burst] += 0.2 * np.exp(2j * np.pi * 400_000 * t[burst])       # 137.9 MHz from 0.8 s to 1.2 s
    rec = an.Recording(make(tmp_path, x))
    img, times, freqs = an.spectrogram(rec, 8192, 100, None, None)
    m = an.spectrum_metrics(img, freqs, RATE / 8192)
    assert abs(m["peak_frequency_hz"] - 137_900_000) <= RATE / 8192
    sig = an.detect_signals(img, times, freqs, m["_floor"])
    top = sig[0]
    assert abs(top["frequency_hz"] - 137_900_000) <= RATE / 8192
    assert 0.7 <= top["start_s"] <= 0.9 and 1.1 <= top["end_s"] <= 1.3
    assert top["confidence"] == "high"


def test_noise_floor_and_snr_estimates(tmp_path):
    n = RATE
    t = np.arange(n) / RATE
    x = noise(n, 0.01) + 0.01 * np.exp(2j * np.pi * -250_000 * t)
    m = an.spectrum_metrics(*an.spectrogram(an.Recording(make(tmp_path, x)), 4096, 50, None, None)[::2], RATE / 4096)
    # tone power 1e-4 vs noise 2e-4 spread over 4096 bins (window loss aside): roughly 30 dB per bin
    assert 25 < m["peak_snr_db"] < 40
    assert m["peak_frequency_hz"] == pytest.approx(CENTER - 250_000, abs=RATE / 4096)


def test_drift_is_measured(tmp_path):
    n = RATE * 4
    t = np.arange(n) / RATE
    f = 400_000 + 1_000 * t                                         # +1 kHz/s Doppler-like drift
    phase = 2 * np.pi * np.cumsum(f) / RATE
    x = noise(n, 0.005) + 0.2 * np.exp(1j * phase)
    tr = an.frequency_track(an.Recording(make(tmp_path, x)), 137_900_000, 30_000, None, None)
    assert tr["measured"] and tr["drift_hz_per_s"] == pytest.approx(1000, rel=0.05) and tr["doppler_like"]


def test_clipping_dc_spike_and_iq_imbalance(tmp_path):
    n = RATE // 2
    x = 1.2 * np.exp(2j * np.pi * 300_000 * np.arange(n) / RATE) + 0.05     # overdriven + DC offset
    x = x.real * 1.0 + 1j * x.imag * 0.7                                    # amplitude imbalance
    rec = an.Recording(make(tmp_path, x, "cu8"))
    img, times, freqs = an.spectrogram(rec, 4096, 40, None, None)
    m = an.spectrum_metrics(img, freqs, RATE / 4096)
    ov = an.overload(rec, None, None, img, freqs, m["_floor"])
    f = {x["finding"]: x for x in ov["findings"]}
    assert f["adc_clipping"]["status"] == "present" and f["adc_clipping"]["basis"] == "measured"
    assert f["excessive_gain"]["status"] == "suspected"
    assert f["iq_imbalance"]["status"] == "present"


def test_clean_recording_reports_nothing(tmp_path):
    rec = an.Recording(make(tmp_path, noise(RATE // 2, 0.02)))
    img, times, freqs = an.spectrogram(rec, 4096, 40, None, None)
    ov = an.overload(rec, None, None, img, freqs, an.spectrum_metrics(img, freqs, RATE / 4096)["_floor"])
    assert {x["finding"]: x["status"] for x in ov["findings"]}["adc_clipping"] == "not detected"
    assert ov["clipped_fraction"] == 0


def test_integrity_detects_truncation_and_metadata_mismatch(tmp_path):
    inp = make(tmp_path, noise(100_000), expected_bytes=800_000, metadata_bytes=123)
    p = pathlib.Path(inp["path"])
    p.write_bytes(p.read_bytes() + b"\x01")                     # half a sample at the end
    res = an.integrity(an.Recording(inp))
    assert not res["consistent"]
    assert any("whole number" in i for i in res["issues"]) and any("expected bytes" in i for i in res["issues"])
    assert any("metadata says" in i for i in res["issues"])


def test_streaming_large_file_reads_only_what_it_needs(tmp_path, monkeypatch):
    inp = make(tmp_path, noise(RATE * 3))
    reads = []
    real = an.Recording.read

    def counting(self, fh, start, n):
        reads.append(n)
        return real(self, fh, start, n)
    monkeypatch.setattr(an.Recording, "read", counting)
    an.spectrogram(an.Recording(inp), 1024, 50, None, None)
    assert sum(reads) < RATE * 3 / 5                         # sampled rows, not the whole file


def test_analyze_run_produces_indexed_artifacts_and_zoom(tmp_path):
    n = RATE
    t = np.arange(n) / RATE
    inp = make(tmp_path, noise(n) + 0.1 * np.exp(2j * np.pi * 400_000 * t), target_frequency_hz=137_900_000)
    out = tmp_path / "out"
    out.mkdir()
    res, arts = analyze(inp, {"analysis": ["waterfall", "spectrum", "signal_detection", "frequency_offset", "clipping",
                                           "integrity"], "fft_size": 4096, "frequency_min_hz": 137_800_000,
                              "frequency_max_hz": 138_000_000, "dynamic_range_db": 50, "colormap": "inferno"}, out, "")
    kinds = sorted(a["kind"] for a in arts)
    assert kinds == ["diagnostics", "diagnostics", "spectrum", "waterfall"]
    assert res["target"]["peak_over_floor_db"] > 20 and res["signals"][0]["frequency_hz"] == pytest.approx(137_900_000, abs=600)
    assert "not calibrated" in res["units"] and res["frequency_offset"]["measured"]


def test_compare_warns_when_not_comparable(tmp_path):
    a = make(tmp_path, noise(RATE // 2, 0.01), name="a", recording_id="m1", receiver_id="OBS-R01",
             record_start="2026-10-13T14:00:00+00:00", record_end="2026-10-13T14:10:00+00:00")
    b = make(tmp_path, noise(RATE // 2, 0.03, 1), name="b", recording_id="m2", receiver_id="RSP1B",
             record_start="2026-10-13T16:00:00+00:00", record_end="2026-10-13T16:10:00+00:00")
    out = tmp_path / "cmp"
    out.mkdir()
    res, arts = compare([a, b], {"fft_size": 4096}, out)
    assert [r["receiver"] for r in res["comparison"]] == ["OBS-R01", "RSP1B"]
    w = " ".join(res["warnings"])
    assert "not made at the same time" in w and "different receivers" in w and "not calibrated" in w
    # noise floor per Hz reflects the 3x amplitude difference (about +9.5 dB)
    diff = res["comparison"][1]["noise_floor_dbfs_per_hz"] - res["comparison"][0]["noise_floor_dbfs_per_hz"]
    assert diff == pytest.approx(9.54, abs=0.5)


def test_underdriven_adc_is_reported(tmp_path):
    quiet = make(tmp_path, noise(RATE // 4, 0.004), "cu8", name="quiet")      # ~0.5 LSB, like R02 at 10 dB gain
    good = make(tmp_path, noise(RATE // 4, 0.07), "cu8", name="good")
    for inp, expect in ((quiet, "present"), (good, "not detected")):
        ov = an.overload(an.Recording(inp), None, None, None, None, None)
        assert {f["finding"]: f["status"] for f in ov["findings"]}["adc_underdriven"] == expect


def test_every_plot_carries_a_caption(tmp_path):
    inp = make(tmp_path, noise(RATE), recording_id="l2026101315396511ec", receiver_id="OBS-R01", antenna="QFH-137",
               target="METEOR M2-3", record_start="2026-10-13T15:39:56+00:00", record_end="2026-10-13T15:55:18+00:00",
               gain=29.7, target_frequency_hz=137_900_000, display_tz="America/New_York")
    lines = an.caption_lines(inp, 180, 420)
    assert lines[0] == "OBS-R01 · QFH-137 · METEOR M2-3 · recording l2026101315396511ec"
    assert lines[1].startswith("2026-10-13 11:39:56–11:55:18 EDT (15:39:56Z) · center 137.500 MHz · target 137.900 MHz")
    assert "2.000 MS/s cs16" in lines[1] and "gain 29.7 dB" in lines[1] and "showing 180–420 s" in lines[1]
    out = tmp_path / "o"
    out.mkdir()
    import matplotlib.pyplot as plt
    texts = []
    real_savefig = plt.Figure.savefig

    def spy(self, *a, **k):
        texts.extend(t.get_text() for t in self.texts)
        return real_savefig(self, *a, **k)
    plt.Figure.savefig = spy
    try:
        _, arts = analyze(inp, {"analysis": ["waterfall", "spectrum", "clipping", "frequency_offset"], "fft_size": 2048}, out, "")
    finally:
        plt.Figure.savefig = real_savefig
    assert len(arts) == 4 and len(texts) == 4 and all("METEOR M2-3" in t and "OBS-R01" in t for t in texts)
