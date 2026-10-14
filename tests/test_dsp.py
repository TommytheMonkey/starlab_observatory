"""Synthetic-signal tests for channel extraction: shift sign, I/Q order, chunk continuity, passband limits."""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from aobs.scheduling import dsp


def tone(rate: int, f: float, n: int, amp: float = 0.5) -> np.ndarray:
    t = np.arange(n) / rate
    return amp * np.exp(2j * np.pi * f * t)


def write(path: pathlib.Path, x: np.ndarray, fmt: str) -> None:
    inter = np.empty(2 * len(x), np.float64)
    inter[0::2], inter[1::2] = x.real, x.imag            # I first, then Q
    if fmt == "cu8":
        raw = np.clip(np.round(inter * 127 + 127.4), 0, 255).astype(np.uint8)
    elif fmt == "cs16":
        raw = np.round(inter * 32767).astype("<i2")
    else:
        raw = inter.astype("<f4")
    path.write_bytes(raw.tobytes())


def peak_hz(x: np.ndarray, rate: int) -> float:
    spec = np.abs(np.fft.fftshift(np.fft.fft(x * np.hanning(len(x)))))
    return float(np.fft.fftshift(np.fft.fftfreq(len(x), 1 / rate))[spec.argmax()])


@pytest.mark.parametrize("fmt, rate", [("cu8", 2_400_000), ("cs16", 2_000_000), ("cf32", 2_000_000)])
def test_iq_order_puts_positive_offsets_above_center(tmp_path, fmt, rate):
    src = tmp_path / f"x.{fmt}"
    write(src, tone(rate, 400_000, 65536), fmt)
    x = dsp.to_complex(src.read_bytes(), fmt)
    assert abs(peak_hz(x, rate) - 400_000) < rate / 65536 * 2


@pytest.mark.parametrize("fmt, rate", [("cu8", 2_400_000), ("cs16", 2_000_000)])
def test_target_is_shifted_to_zero_and_image_is_rejected(tmp_path, fmt, rate):
    n = 1 << 18
    # wanted signal at +400 kHz (+5 kHz modulation offset), an equal-strength mirror at -400 kHz
    x = tone(rate, 405_000, n, 0.3) + tone(rate, -400_000, n, 0.3)
    src = tmp_path / f"x.{fmt}"
    write(src, x, fmt)
    plan = dsp.plan_channel(rate, 137_500_000, 137_900_000)
    assert plan.frequency_offset_hz == 400_000 and plan.output_sample_rate_hz == rate // plan.decimation
    out = tmp_path / "y.cf32"
    stats = dsp.extract(src, fmt, plan, out, chunk=50_000)        # many chunks, with an odd size
    y = np.fromfile(out, dtype="<f4").view(np.complex64)
    assert stats["output_samples"] == len(y) == n // plan.decimation
    y = y[200:]                                                    # skip the filter start-up
    assert abs(peak_hz(y, plan.output_sample_rate_hz) - 5_000) < 100   # 137.905 MHz now sits at +5 kHz
    spec = np.abs(np.fft.fft(y)) ** 2
    f = np.fft.fftfreq(len(y), 1 / plan.output_sample_rate_hz)
    wanted = spec[np.abs(f - 5_000) < 2_000].max()
    leak = spec[np.abs(f) > 50_000].max()
    assert 10 * np.log10(wanted / leak) > 40                        # the -400 kHz signal is filtered out


def test_chunking_does_not_change_the_output(tmp_path):
    rate = 2_000_000
    x = tone(rate, 410_000, 200_000) + 0.01 * (np.random.default_rng(0).normal(size=200_000)
                                               + 1j * np.random.default_rng(1).normal(size=200_000))
    src = tmp_path / "x.cf32"
    write(src, x, "cf32")
    plan = dsp.plan_channel(rate, 0, 400_000)
    a, b = tmp_path / "a.cf32", tmp_path / "b.cf32"
    dsp.extract(src, "cf32", plan, a, chunk=1 << 20)
    dsp.extract(src, "cf32", plan, b, chunk=4_000)
    assert np.allclose(np.fromfile(a, "<f4"), np.fromfile(b, "<f4"), atol=1e-5)


def test_time_slice_and_resampling_rates(tmp_path):
    rate = 2_400_000
    src = tmp_path / "x.cu8"
    write(src, tone(rate, 0, rate), "cu8")                        # 1 s
    plan = dsp.plan_channel(rate, 137_500_000, 137_500_000, out_rate=1_200_000)
    st = dsp.extract(src, "cu8", plan, tmp_path / "y.cf32", start_s=0.25, end_s=0.75)
    assert plan.decimation == 2 and st["output_seconds"] == pytest.approx(0.5, abs=1e-3)
    with pytest.raises(ValueError, match="must divide"):
        dsp.plan_channel(rate, 0, 0, out_rate=1_000_000)


def test_channel_must_fit_inside_the_recording():
    with pytest.raises(ValueError, match="not fully inside"):
        dsp.plan_channel(2_000_000, 137_500_000, 138_300_000)         # 800 kHz off a 1 MHz half-band
    assert dsp.plan_channel(2_000_000, 137_500_000, 137_900_000).output_sample_rate_hz == 500_000


def test_fine_shift_moves_residual_offset_to_zero_continuously(tmp_path):
    rate = 500_000
    x = tone(rate, -4_800, 300_000)                       # METEOR seen 4.8 kHz low after extraction
    src = tmp_path / "c.cf32"
    write(src, x, "cf32")
    a, b = tmp_path / "a.cf32", tmp_path / "b.cf32"
    assert dsp.shift_file(src, a, -4_800, rate) == 300_000
    dsp.shift_file(src, b, -4_800, rate, chunk=7_777)     # chunking must not change the result
    ya, yb = (np.fromfile(p, "<f4").view(np.complex64) for p in (a, b))
    assert abs(peak_hz(ya, rate)) < rate / len(ya) * 2
    assert np.allclose(ya, yb, atol=1e-5)
