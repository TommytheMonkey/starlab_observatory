"""Gain sweeps: the same frequency recorded at several receiver gain settings, one open stream.

Per step: sample RMS and peak (dBFS / fraction of full scale), digital clipping, a Welch PSD (Hann,
2048 points, 50% overlap, dBFS/Hz), the noise floor (median of the PSD outside the DC bins and the
target channel), the target-channel power, its contrast over the noise floor predicted for the same
bandwidth, and the number of spectral peaks 10 dB above the floor.

Gain semantics are kept as the hardware defines them: RTL-SDR tuner gain in dB (higher = more gain);
SDRplay IFGR/RFGR are gain REDUCTIONS (higher = less gain). Higher digital power is not better in
itself; what matters is the target's contrast over the noise without clipping.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import numpy as np

from .dsp import DTYPES, to_complex

NFFT = 2048


def _label(g: Any) -> str:
    if isinstance(g, dict):
        return f"IFGR {g.get('IFGR')} / RFGR {g.get('RFGR')}"
    return "auto" if g in (None, "auto") else f"{g} dB"


def _clip(raw: np.ndarray, fmt: str) -> int:
    if fmt == "cu8":
        return int(np.count_nonzero((raw == 0) | (raw == 255)))
    if fmt == "cs16":
        return int(np.count_nonzero(np.abs(raw.astype(np.int32)) >= 32760))
    if fmt == "cs8":
        return int(np.count_nonzero(np.abs(raw.astype(np.int16)) >= 127))
    return 0


def measure(raw: np.ndarray, fmt: str, rate: int, center: int, target: int | None, chbw: float) -> dict[str, Any]:
    x = to_complex(raw.tobytes(), fmt)
    p = np.abs(x) ** 2
    win = np.hanning(NFFT)
    hop = NFFT // 2
    nseg = max(1, (len(x) - NFFT) // hop + 1)
    acc = np.zeros(NFFT)
    for k in range(nseg):
        seg = x[k * hop:k * hop + NFFT]
        if len(seg) < NFFT:
            break
        acc += np.abs(np.fft.fftshift(np.fft.fft(seg * win))) ** 2
    psd = acc / max(1, nseg) / (rate * float((win * win).sum()))
    f = center + (np.arange(NFFT) - NFFT // 2) * rate / NFFT
    rbw = rate / NFFT
    keep = np.abs(f - center) > 3 * rbw
    edge = np.abs(f - center) < 0.45 * rate
    ch = (np.abs(f - target) <= chbw / 2) if target else np.zeros(NFFT, bool)
    ref = keep & edge & ~((np.abs(f - target) <= chbw) if target else ch)
    floor = float(np.median(psd[ref]))
    out: dict[str, Any] = {
        "rms_dbfs": round(float(10 * np.log10(p.mean() + 1e-30)), 2),
        "peak_fraction_of_full_scale": round(float(max(np.abs(x.real).max(), np.abs(x.imag).max())), 4),
        "clipped_fraction": round(_clip(raw, fmt) / max(1, raw.size), 7),
        "noise_floor_dbfs_per_hz": round(float(10 * np.log10(floor + 1e-30)), 2),
        "spectral_peaks": int(np.count_nonzero(np.diff((10 * np.log10(psd[keep & edge] + 1e-30) >
                                                        10 * np.log10(floor) + 10).astype(np.int8)) == 1)),
    }
    if target and ch.any():
        pch = float(psd[ch].sum() * rbw)
        pn = floor * ch.sum() * rbw
        out["channel_power_dbfs"] = round(float(10 * np.log10(pch + 1e-30)), 2)
        out["channel_contrast_db"] = round(float(10 * np.log10(pch / pn)), 2)
    return out


def analyze(raw_path: pathlib.Path, fmt: str, index: dict[str, Any], p: dict[str, Any], out: pathlib.Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    rate = int(index["sample_rate_hz"])
    dtype, comps = DTYPES[fmt]
    bps = np.dtype(dtype).itemsize * comps
    target = p.get("target_frequency_hz")
    chbw = float(p.get("channel_bandwidth_hz") or 25_000)
    rows = []
    with raw_path.open("rb") as fh:
        for st in index["steps"]:
            fh.seek(int(st["offset_samples"]) * bps)
            raw = np.frombuffer(fh.read(int(st["samples"]) * bps), dtype=dtype)
            m = measure(raw, fmt, rate, int(st.get("tuned_hz") or st["center_hz"]), target, chbw)
            rows.append({"gain": st.get("gain"), "applied_gain": st.get("applied_gain"), "label": _label(st.get("gain")),
                         "overflows": st.get("overflows", 0), **m})
    summary = interpret(rows, bool(target))
    data = {"version": 1, "rows": rows, "summary": summary, "target_frequency_hz": target, "channel_bandwidth_hz": chbw,
            "gain_semantics": "RTL-SDR: tuner gain dB (higher = more gain)" if not isinstance(rows[0]["gain"], dict)
            else "SDRplay: IFGR/RFGR are gain reductions (higher = less gain)",
            "methodology": __doc__.split("\n\n")[1].strip()}
    (out / "rf_gain_sweep.json").write_text(json.dumps(data, indent=1))
    _plot(data, out / "gain_sweep.png")
    return summary, [{"kind": "diagnostics", "path": str(out / "rf_gain_sweep.json"), "label": "gain sweep metrics"},
                     {"kind": "diagnostics", "path": str(out / "gain_sweep.png"), "label": "gain sweep plots"}]


def interpret(rows: list[dict[str, Any]], has_target: bool) -> dict[str, Any]:
    facts = []
    clip_at = next((r["label"] for r in rows if r["clipped_fraction"] > 1e-5), None)
    if clip_at:
        facts.append(f"Digital clipping starts at {clip_at}.")
    else:
        facts.append("No digital clipping at any tested setting (this does not rule out analog compression).")
    best = None
    if has_target:
        ok = [r for r in rows if r["clipped_fraction"] <= 1e-5 and r.get("channel_contrast_db") is not None]
        if ok:
            best = max(ok, key=lambda r: r["channel_contrast_db"])
            near = [r["label"] for r in ok if r["channel_contrast_db"] >= best["channel_contrast_db"] - 1]
            facts.append(f"Highest target contrast without clipping: {best['channel_contrast_db']} dB at {best['label']}"
                         + (f"; within 1 dB of that at {', '.join(near)}." if len(near) > 1 else "."))
            lo, hi = min(r["channel_contrast_db"] for r in ok), max(r["channel_contrast_db"] for r in ok)
            if hi - lo < 1:
                facts.append("Target contrast changed by less than 1 dB across the sweep: gain made no measurable "
                             "difference to how far the target stands above the noise.")
    peaks = [r["spectral_peaks"] for r in rows]
    if peaks and max(peaks) - min(peaks) >= 3:
        facts.append(f"Spectral peaks counted ranged from {min(peaks)} to {max(peaks)} across the sweep; peaks that appear "
                     "only at high gain can be overload products, or weak real signals rising above the floor.")
    return {"facts": facts, "clipping_starts_at": clip_at, "best_contrast": best and {"label": best["label"],
            "gain": best["gain"], "channel_contrast_db": best["channel_contrast_db"]},
            "caution": "Higher digital power is not better in itself; gain does not improve sensitivity once the "
                       "receiver's own noise is above the ADC's."}


def _plot(data: dict[str, Any], path: pathlib.Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    rows = data["rows"]
    x = np.arange(len(rows))
    fig, axs = plt.subplots(2, 2, figsize=(12, 7), dpi=100)
    for ax, key, lab in ((axs[0, 0], "rms_dbfs", "RMS (dBFS)"), (axs[0, 1], "noise_floor_dbfs_per_hz", "noise floor (dBFS/Hz)"),
                         (axs[1, 0], "channel_contrast_db", "target contrast (dB)"), (axs[1, 1], "clipped_fraction", "clipping (fraction)")):
        y = [r.get(key) for r in rows]
        ax.plot(x, [np.nan if v is None else v for v in y], marker="o")
        ax.set_ylabel(lab)
        ax.set_xticks(x, [r["label"] for r in rows], rotation=45, ha="right", fontsize=7)
        ax.grid(alpha=0.3)
    fig.suptitle(f"Gain sweep · {data['gain_semantics']}", fontsize=9)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
