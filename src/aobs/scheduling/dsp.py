"""Streaming channel extraction: shift a target signal to 0 Hz and decimate, chunk by chunk.

Convention (tested with synthetic tones): a recording centered at C contains a signal at
F at baseband offset +(F - C). Multiplying by exp(-j*2*pi*(F - C)*t) moves it to 0 Hz.
I is the first value of each pair and Q the second, which is how rtl_sdr (cu8) and SoapySDR
(cs16, little-endian int16) write samples.

Decimation is by an integer factor M behind a windowed-sinc low-pass (stateful lfilter,
so there are no chunk-edge artifacts). The output is cf32, which SatDump reads directly.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

import numpy as np

DTYPES = {"cu8": (np.uint8, 2), "cs8": (np.int8, 2), "cs16": (np.dtype("<i2"), 2), "cf32": (np.dtype("<f4"), 2)}
GUARD = 0.9            # use at most 90% of the recorded half-band (anti-alias roll-off)


def to_complex(raw: bytes, fmt: str) -> np.ndarray:
    dtype, _ = DTYPES[fmt]
    a = np.frombuffer(raw, dtype=dtype)
    if fmt == "cu8":
        a = (a.astype(np.float32) - 127.4) / 128.0
    elif fmt == "cs8":
        a = a.astype(np.float32) / 128.0
    elif fmt == "cs16":
        a = a.astype(np.float32) / 32768.0
    else:
        a = a.astype(np.float32)
    return a[0::2] + 1j * a[1::2]


@dataclass(frozen=True)
class ChannelPlan:
    input_sample_rate_hz: int
    center_frequency_hz: int
    target_frequency_hz: int
    frequency_offset_hz: int
    decimation: int
    output_sample_rate_hz: int
    taps: int

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def plan_channel(in_rate: int, center: int, target: int, out_rate: int | None = None, taps_per_m: int = 32) -> ChannelPlan:
    """Validate and plan the extraction. Raises ValueError with an explanation if it does not fit."""
    offset = int(target) - int(center)
    if out_rate is None:
        m = max(1, in_rate // 500_000)                      # default: the largest decimation keeping >= 500 kS/s
        while m > 1 and in_rate % m:
            m -= 1
    else:
        if out_rate <= 0 or in_rate % out_rate:
            ok = [in_rate // m for m in range(1, 17) if in_rate % m == 0 and in_rate // m >= 100_000]
            raise ValueError(f"output_sample_rate_hz must divide the input rate {in_rate} exactly; choose one of {ok}")
        m = in_rate // out_rate
    out = in_rate // m
    if abs(offset) + out / 2 > in_rate / 2 * GUARD:
        raise ValueError(f"target {target / 1e6:.4f} MHz with a {out / 1e3:.0f} kHz output band needs "
                         f"{(abs(offset) + out / 2) / 1e3:.0f} kHz of the {in_rate / 2 * GUARD / 1e3:.0f} kHz usable "
                         "half-band; the channel is not fully inside the recording")
    return ChannelPlan(in_rate, int(center), int(target), offset, m, out, taps_per_m * m + 1)


def extract(src: pathlib.Path, fmt: str, plan: ChannelPlan, dst: pathlib.Path, start_s: float | None = None,
            end_s: float | None = None, chunk: int = 1 << 20, conjugate: bool = False) -> dict[str, Any]:
    """Stream src -> shifted, filtered, decimated cf32 at dst. Returns sample counts and levels."""
    from scipy.signal import firwin, lfilter

    dtype, comps = DTYPES[fmt]
    bps = np.dtype(dtype).itemsize * comps
    total = src.stat().st_size // bps
    a = int((start_s or 0) * plan.input_sample_rate_hz)
    b = min(total, int(end_s * plan.input_sample_rate_hz)) if end_s else total
    if not 0 <= a < b:
        raise ValueError("time range is outside the recording")
    m = plan.decimation
    h = firwin(plan.taps, cutoff=0.45 / m * 2, window=("kaiser", 8.0)).astype(np.float64) if m > 1 else None
    zi = np.zeros(plan.taps - 1, np.complex128) if h is not None else None
    step = -2.0 * np.pi * plan.frequency_offset_hz / plan.input_sample_rate_hz
    n_in = n_out = 0
    keep_phase = 0                       # decimation phase carried across chunks
    peak = 0.0
    chunk -= chunk % max(1, m)
    with src.open("rb") as fi, dst.open("wb") as fo:
        fi.seek(a * bps)
        left = b - a
        while left > 0:
            raw = fi.read(min(left, chunk) * bps)
            if not raw:
                break
            x = to_complex(raw, fmt)
            if conjugate:
                x = np.conj(x)
            idx = np.arange(n_in, n_in + len(x), dtype=np.float64)
            if plan.frequency_offset_hz:
                x = x * np.exp(1j * np.mod(step * idx, 2 * np.pi))      # phase continuous across chunks
            if h is not None:
                y, zi = lfilter(h, 1.0, x, zi=zi)
                y = y[keep_phase::m]
                keep_phase = (keep_phase - len(x)) % m
            else:
                y = x
            y = y.astype(np.complex64)
            peak = max(peak, float(np.abs(y).max(initial=0.0)))
            fo.write(y.view(np.float32).tobytes())
            n_in += len(x)
            n_out += len(y)
            left -= len(x)
    return {"input_samples": n_in, "output_samples": n_out, "output_peak": round(peak, 4),
            "output_seconds": round(n_out / plan.output_sample_rate_hz, 3), "filter_taps": plan.taps}


def shift_file(src: pathlib.Path, dst: pathlib.Path, offset_hz: float, rate: int, chunk: int = 1 << 20) -> int:
    """Phase-continuous frequency shift of a cf32 file: content at +offset_hz moves to 0 Hz."""
    step = -2.0 * np.pi * offset_hz / rate
    n = 0
    with src.open("rb") as fi, dst.open("wb") as fo:
        while True:
            raw = fi.read(chunk * 8)
            if not raw:
                break
            x = np.frombuffer(raw, dtype="<f4").view(np.complex64)
            idx = np.arange(n, n + len(x), dtype=np.float64)
            fo.write((x * np.exp(1j * np.mod(step * idx, 2 * np.pi))).astype(np.complex64).view(np.float32).tobytes())
            n += len(x)
    return n
