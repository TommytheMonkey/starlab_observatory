"""Did a capture produce real receiver samples? The evidence behind a receiver's readiness.

A device that opens, or a capture process that exits 0, proves nothing: a stuck dongle can
write full-size files of constant codes (OBS-R02, 2026-10-13). This reads the samples
themselves, in three windows (early, middle, late) so a stream that dies part-way is caught:

  * enough bytes for the requested duration (98 %, the capture's own threshold);
  * every value finite, not all zero, I and Q each actually varying (spread and distinct codes);
  * not stuck: consecutive identical samples are rare in real noise;
  * I and Q are not copies of each other.

Saturation is reported as a warning, not a failure: an overloaded receiver still delivers samples,
which is what readiness is about; the gain is a separate question.
"""

from __future__ import annotations

import math
import pathlib
from typing import Any

import numpy as np

BYTES = {"cu8": 2, "cs8": 2, "cs16": 4, "cf32": 8}
FULL_SCALE = {"cu8": 128.0, "cs8": 128.0, "cs16": 32768.0, "cf32": 1.0}
MIN_DISTINCT = {"cu8": 4, "cs8": 4, "cs16": 16}
MIN_BYTES_FRACTION = 0.98
WINDOW = 200_000                 # complex samples per window
STUCK_FRACTION = 0.5
CLIP_WARN = 0.01


def _window(path: pathlib.Path, fmt: str, start: int, n: int) -> np.ndarray:
    """Complex samples [start, start+n) as I, Q float arrays in raw units (cu8 re-centered on 0)."""
    b = BYTES[fmt]
    with path.open("rb") as fh:
        fh.seek(start * b)
        raw = fh.read(n * b)
    if fmt == "cu8":
        a = np.frombuffer(raw, np.uint8).astype(np.float64) - 127.5
    elif fmt == "cs8":
        a = np.frombuffer(raw, np.int8).astype(np.float64)
    elif fmt == "cs16":
        a = np.frombuffer(raw[: len(raw) // 4 * 4], "<i2").astype(np.float64)
    else:
        a = np.frombuffer(raw[: len(raw) // 8 * 8], "<f4").astype(np.float64)
    a = a[: len(a) // 2 * 2]
    return a.reshape(-1, 2)


def verify_iq(path: str | pathlib.Path, fmt: str, sample_rate_hz: int, expected_bytes: int | None = None,
              skip_s: float = 0.25) -> dict[str, Any]:
    p = pathlib.Path(path)
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []

    def check(name: str, ok: bool, detail: str) -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return ok

    def result(samples: int = 0, rms: float | None = None) -> dict[str, Any]:
        bad = [c for c in checks if not c["ok"]]
        return {"ok": not bad and bool(checks), "checks": checks, "problems": [c["detail"] for c in bad],
                "warnings": warnings, "samples_checked": samples, "rms_dbfs": rms, "format": fmt, "file": str(p)}

    if fmt not in BYTES:
        check("format", False, f"unknown sample format {fmt!r}")
        return result()
    if not check("file", p.is_file(), f"{p.name} {'exists' if p.is_file() else 'is missing'}"):
        return result()
    size = p.stat().st_size
    total = size // BYTES[fmt]
    if expected_bytes:
        check("length", size >= expected_bytes * MIN_BYTES_FRACTION,
              f"{size:,} of {expected_bytes:,} expected bytes ({size / expected_bytes:.0%})")
    if not check("samples", total >= max(1000, int(sample_rate_hz * 0.5)),
                 f"{total:,} complex samples ({total / sample_rate_hz:.2f} s at {sample_rate_hz / 1e6:.3f} MS/s)"):
        return result()
    skip = min(int(sample_rate_hz * skip_s), total // 4)
    span = total - skip
    n = min(WINDOW, span // 3)
    starts = [skip, skip + (span - n) // 2, total - n]
    checked, power = 0, []
    for label, s in zip(("early", "middle", "late"), starts):
        iq = _window(p, fmt, s, n)
        checked += len(iq)
        i, q = iq[:, 0], iq[:, 1]
        if not check(f"{label}: finite", bool(np.isfinite(iq).all()), f"{label} window: values are finite"
                     if np.isfinite(iq).all() else f"{label} window: NaN or infinite values"):
            continue
        if not check(f"{label}: not zero", bool(np.any(iq != 0)), f"{label} window: "
                     + ("samples are not all zero" if np.any(iq != 0) else "every sample is zero")):
            continue
        si, sq = float(i.std()), float(q.std())
        if fmt == "cf32":
            varying = si > 1e-7 and sq > 1e-7
            desc = f"I/Q spread {si:.2e}/{sq:.2e}"
        else:
            di, dq = len(np.unique(i)), len(np.unique(q))
            varying = di >= MIN_DISTINCT[fmt] and dq >= MIN_DISTINCT[fmt] and si > 0 and sq > 0
            desc = f"I/Q use {di}/{dq} distinct codes, spread {si:.2f}/{sq:.2f} LSB"
        check(f"{label}: varying", varying, f"{label} window: {desc}" + ("" if varying else " (not real receiver noise)"))
        same = float(np.mean(np.all(iq[1:] == iq[:-1], axis=1))) if len(iq) > 1 else 0.0
        check(f"{label}: not stuck", same < STUCK_FRACTION,
              f"{label} window: {same:.1%} of samples repeat the previous one")
        if len(i) and float(np.mean(i == q)) > 0.99 and si > 0:
            check(f"{label}: I differs from Q", False, f"{label} window: I and Q are identical (not a complex stream)")
        fs = FULL_SCALE[fmt]
        clipped = float(np.mean((np.abs(i) >= fs * 0.99) | (np.abs(q) >= fs * 0.99)))
        if clipped > CLIP_WARN:
            warnings.append(f"{label} window: {clipped:.1%} of samples at full scale (overload; lower the gain)")
        power.append(float(np.mean(i ** 2 + q ** 2)) / fs ** 2)
    rms = round(10 * math.log10(max(np.mean(power), 1e-30)), 1) if power else None
    return result(checked, rms)
