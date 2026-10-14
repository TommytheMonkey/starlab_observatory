"""Wideband scans: plan a stepped sweep, stitch per-step Welch PSDs into one spectrum, compare two scans.

Methodology (shown with every result):
  * Each step records `dwell` seconds at one tuning. Its PSD is Welch's method: Hann windows of nfft
    samples, 50% overlap, |FFT|^2 / (sample_rate * sum(w^2)), averaged over the segments. Units are
    dBFS/Hz: density relative to the ADC's digital full scale. Nothing is calibrated to dBm.
  * Only the middle `usable_fraction` of each step's band is used (the anti-alias filter rolls off at the
    edges), and a notch of `dc_notch_hz` around each step's own center is dropped (the receiver's DC
    spike). Steps advance by half the usable span, so every frequency is seen by two steps and each
    step's DC notch is covered by its neighbours.
  * Step centers are placed on multiples of the FFT resolution, so all steps share one exact frequency
    grid; overlapping measurements are averaged in linear power. Peak hold is the maximum over every
    Welch segment of every step.
  * All steps use the same gain, so the stitching applies no per-step correction. Real level changes
    between two scans are never normalized away; differences are shown as measured.
  * The tuner's own response is not flat across a wide range, so a scan shows the receiver+antenna+path
    response, not the RF environment alone. That is why scans are compared like for like.
"""

from __future__ import annotations

import base64
import json
import math
import pathlib
from typing import Any

import numpy as np

from .dsp import DTYPES, to_complex

RESOLUTIONS = {"coarse": 50_000, "medium": 10_000, "fine": 2_000}
USABLE = 0.75
MAX_DISPLAY = 6000
FLOOR_WINDOW_HZ = 1_000_000
DEFAULT_THRESHOLD_DB = 10.0
FM_REGION = {"name": "FM broadcast", "start_hz": 88_000_000, "stop_hz": 108_000_000}


def _pow2(x: float) -> int:
    return int(2 ** round(math.log2(max(256, min(65536, x)))))


def plan(start_hz: int, stop_hz: int, rate: int, resolution: str | int = "auto", dwell_s: float = 0.25,
         usable: float = USABLE) -> dict[str, Any]:
    """Step centers on a grid aligned to the FFT resolution; half-usable-span stride."""
    if stop_hz <= start_hz:
        raise ValueError("stop frequency must be above start")
    span = stop_hz - start_hz
    if resolution == "auto":
        target = max(1_000, span / 40_000)                       # ~40k bins at most across the range
        rbw_goal = min(RESOLUTIONS, key=lambda k: abs(RESOLUTIONS[k] - target))
        rbw_goal = max(RESOLUTIONS[rbw_goal], target)
    elif isinstance(resolution, str):
        rbw_goal = RESOLUTIONS[resolution]
    else:
        rbw_goal = float(resolution)
    nfft = _pow2(rate / rbw_goal)
    rbw = rate / nfft
    stride = max(rbw, math.floor(usable * rate / 2 / rbw) * rbw)
    first = math.floor((start_hz + usable * rate / 2 - stride) / rbw) * rbw
    centers = []
    c = first
    while c - usable * rate / 2 < stop_hz:
        centers.append(int(round(c)))
        c += stride
    # need at least one full segment set per step
    segs = max(1, int(dwell_s * rate // (nfft // 2)) - 1)
    return {"start_hz": int(start_hz), "stop_hz": int(stop_hz), "sample_rate_hz": int(rate), "nfft": nfft,
            "rbw_hz": rbw, "stride_hz": stride, "usable_fraction": usable, "dc_notch_hz": max(3 * rbw, 2_000),
            "centers_hz": centers, "dwell_s": dwell_s, "welch_segments_per_step": segs}


def estimate_seconds(n_steps: int, dwell_s: float, settle_s: float, repeats: int, driver: str) -> float:
    per = dwell_s + settle_s + (0.03 if driver == "rtlsdr" else 0.06)       # retune + read overhead
    return 4.0 + n_steps * repeats * per


def _psd_step(x: np.ndarray, nfft: int, rate: int, win: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    hop = nfft // 2
    nseg = max(1, (len(x) - nfft) // hop + 1)
    idx = np.arange(nseg)[:, None] * hop + np.arange(nfft)[None, :]
    if idx[-1, -1] >= len(x):
        nseg = 1
        idx = np.arange(nfft)[None, :]
        x = np.pad(x, (0, max(0, nfft - len(x))))
    seg = np.fft.fftshift(np.fft.fft(x[idx] * win, axis=1), axes=1)
    p = (np.abs(seg) ** 2) / (rate * float((win * win).sum()))
    return p.mean(axis=0), p.max(axis=0)


def stitch(raw: pathlib.Path, fmt: str, index: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    """Per-step Welch PSDs -> per-sweep and overall spectra on one grid. Returns full-resolution arrays."""
    rate = int(index["sample_rate_hz"])
    nfft = int(p["nfft"])
    rbw = rate / nfft
    usable = float(p.get("usable_fraction", USABLE))
    notch = float(p.get("dc_notch_hz", max(3 * rbw, 2_000)))
    start, stop = int(p["start_hz"]), int(p["stop_hz"])
    g0 = math.floor(start / rbw) * rbw
    nb = int(math.ceil((stop - g0) / rbw)) + 1
    sweeps = sorted({s["sweep"] for s in index["steps"]})
    acc = np.zeros((len(sweeps), nb))
    cnt = np.zeros((len(sweeps), nb))
    peak = np.full(nb, -np.inf)
    win = np.hanning(nfft)
    dtype, comps = DTYPES[fmt]
    bps = np.dtype(dtype).itemsize * comps
    rel = (np.arange(nfft) - nfft // 2) * rbw
    keep = (np.abs(rel) <= usable * rate / 2) & (np.abs(rel) > notch)
    per_step = []
    with raw.open("rb") as fh:
        for s in index["steps"]:
            fh.seek(int(s["offset_samples"]) * bps)
            x = to_complex(fh.read(int(s["samples"]) * bps), fmt)
            if len(x) < nfft:
                continue
            mean, pk = _psd_step(x, nfft, rate, win)
            f = s.get("tuned_hz") or s["center_hz"]
            k = np.round((f + rel - g0) / rbw).astype(np.int64)
            ok = keep & (k >= 0) & (k < nb)
            w = sweeps.index(s["sweep"])
            np.add.at(acc[w], k[ok], mean[ok])
            np.add.at(cnt[w], k[ok], 1)
            np.maximum.at(peak, k[ok], pk[ok])
            per_step.append({"center_hz": s["center_hz"], "tuned_hz": f, "median_dbfs_per_hz":
                             round(float(10 * np.log10(np.median(mean[keep]) + 1e-30)), 2), "overflows": s.get("overflows", 0)})
    freqs = g0 + np.arange(nb) * rbw
    inside = (freqs >= start) & (freqs <= stop)
    with np.errstate(invalid="ignore", divide="ignore"):
        per_sweep = np.where(cnt > 0, acc / np.maximum(cnt, 1), np.nan)
        mean = np.nanmean(per_sweep, axis=0) if len(sweeps) > 1 else per_sweep[0]
    return {"freqs": freqs[inside], "mean_lin": mean[inside], "peak_lin": np.where(np.isfinite(peak), peak, np.nan)[inside],
            "sweeps_lin": per_sweep[:, inside], "rbw": rbw, "per_step": per_step,
            "covered": (cnt.sum(axis=0) > 0)[inside]}


def _db(x: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return 10 * np.log10(np.maximum(x, 1e-30))


def floor_curve(mean_db: np.ndarray, rbw: float, window_hz: float = FLOOR_WINDOW_HZ) -> np.ndarray:
    """Frequency-dependent noise floor: running 30th percentile over `window_hz` (robust to carriers)."""
    from scipy.ndimage import percentile_filter
    w = max(5, int(window_hz / rbw) | 1)
    filled = np.where(np.isfinite(mean_db), mean_db, np.nanmedian(mean_db))
    return percentile_filter(filled, 30, size=w, mode="nearest")


def carriers(freqs: np.ndarray, mean_db: np.ndarray, floor_db: np.ndarray, sweeps_db: np.ndarray, rbw: float,
             threshold_db: float = DEFAULT_THRESHOLD_DB, limit: int = 200) -> list[dict[str, Any]]:
    over = np.nan_to_num(mean_db - floor_db, nan=-99) > threshold_db
    out, i = [], 0
    while i < len(over):
        if not over[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(over) and over[j + 1]:
            j += 1
        k = i + int(np.nanargmax(mean_db[i:j + 1]))
        per = sweeps_db[:, i:j + 1].max(axis=1) - floor_db[k] if sweeps_db.size else np.array([mean_db[k] - floor_db[k]])
        out.append({"frequency_hz": int(freqs[k]), "power_dbfs_per_hz": round(float(mean_db[k]), 2),
                    "over_floor_db": round(float(mean_db[k] - floor_db[k]), 1), "bandwidth_hz": int((j - i + 1) * rbw),
                    "persistence": round(float(np.mean(per > threshold_db)), 2) if per.size else None,
                    "variation_db": round(float(np.nanstd(per)), 2) if per.size > 1 else None})
        i = j + 1
    return sorted(out, key=lambda c: -c["over_floor_db"])[:limit]


def _decimate(freqs: np.ndarray, mean_lin: np.ndarray, peak_lin: np.ndarray, n: int) -> tuple[np.ndarray, ...]:
    k = max(1, int(math.ceil(len(freqs) / n)))
    m = (len(freqs) // k) * k
    f = freqs[:m].reshape(-1, k).mean(axis=1)
    with np.errstate(invalid="ignore"):
        mean = np.nanmean(mean_lin[:m].reshape(-1, k), axis=1)
        pk = np.nanmax(peak_lin[:m].reshape(-1, k), axis=1)
    return f, mean, pk, k


def _r(v: Any, n: int = 2) -> Any:
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(float(v), n)


def analyze(raw: pathlib.Path, fmt: str, index: dict[str, Any], p: dict[str, Any], out: pathlib.Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    st = stitch(raw, fmt, index, p)
    mean_db, peak_db = _db(st["mean_lin"]), _db(st["peak_lin"])
    floor_db = floor_curve(mean_db, st["rbw"])
    sweeps_db = _db(st["sweeps_lin"])
    thr = float(p.get("threshold_db", DEFAULT_THRESHOLD_DB))
    car = carriers(st["freqs"], mean_db, floor_db, sweeps_db, st["rbw"], thr)
    np.savez_compressed(out / "rf_scan.npz", freqs=st["freqs"], mean_db=mean_db.astype(np.float32),
                        peak_db=peak_db.astype(np.float32), floor_db=floor_db.astype(np.float32),
                        sweeps_db=sweeps_db.astype(np.float32), covered=st["covered"])
    f, m, pk, k = _decimate(st["freqs"], st["mean_lin"], st["peak_lin"], MAX_DISPLAY)
    fl = floor_curve(_db(m), st["rbw"] * k)
    nsw = st["sweeps_lin"].shape[0]
    heat = None
    if nsw > 1:
        hk = max(1, int(math.ceil(st["sweeps_lin"].shape[1] / 1500)))
        hm = (st["sweeps_lin"].shape[1] // hk) * hk
        with np.errstate(invalid="ignore"):
            hv = _db(np.nanmean(st["sweeps_lin"][:, :hm].reshape(nsw, -1, hk), axis=2))
        lo, hi = float(np.nanpercentile(hv, 2)), float(np.nanpercentile(hv, 99.9))
        q = np.clip((np.nan_to_num(hv, nan=lo) - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)
        heat = {"rows": nsw, "cols": int(q.shape[1]), "min_dbfs": round(lo, 2), "max_dbfs": round(hi, 2),
                "f_start_hz": int(st["freqs"][0]), "f_end_hz": int(st["freqs"][hm - 1]),
                "u8_b64": base64.b64encode(q.tobytes()).decode()}
    gaps = int(np.count_nonzero(~st["covered"]))
    regions = [r for r in (p.get("regions") or []) if r.get("stop_hz", 0) > st["freqs"][0] and r.get("start_hz", 0) < st["freqs"][-1]]
    summary = {
        "units": "dBFS/Hz (digital full scale; uncalibrated, not dBm)",
        "start_hz": int(st["freqs"][0]), "stop_hz": int(st["freqs"][-1]), "rbw_hz": _r(st["rbw"], 1),
        "bins": int(len(st["freqs"])), "uncovered_bins": gaps, "sweeps": nsw,
        "steps_completed": len(index["steps"]), "steps_planned": index.get("planned_steps"),
        "noise_floor_median_dbfs_per_hz": _r(np.nanmedian(floor_db)),
        "noise_floor_range_db": [_r(np.nanpercentile(floor_db, 5)), _r(np.nanpercentile(floor_db, 95))],
        "carriers": len(car), "strong_carriers": sum(1 for c in car if c["over_floor_db"] >= 20),
        "overflows": sum(s.get("overflows", 0) for s in index["steps"]),
        "regions": [{**r, **region_stats(st["freqs"], mean_db, floor_db, r)} for r in regions],
    }
    data = {"version": 1, "summary": summary, "methodology": __doc__.split("Methodology (shown with every result):")[1].strip(),
            "plan": {k: v for k, v in p.items() if k not in ("regions",)}, "carriers": car, "per_step": st["per_step"],
            "spectrum": {"freqs_hz": [int(x) for x in f], "mean_dbfs_per_hz": [_r(v) for v in _db(m)],
                         "peak_dbfs_per_hz": [_r(v) for v in _db(pk)], "floor_dbfs_per_hz": [_r(v) for v in fl],
                         "display_bin_hz": _r(st["rbw"] * k, 1)},
            "heatmap": heat}
    (out / "rf_scan.json").write_text(json.dumps(data, separators=(",", ":")))
    _plot(data, out / "scan_spectrum.png")
    arts = [{"kind": "diagnostics", "path": str(out / "rf_scan.json"), "label": "wideband scan metrics and plot data"},
            {"kind": "diagnostics", "path": str(out / "rf_scan.npz"), "label": "wideband scan full-resolution arrays"},
            {"kind": "spectrum", "path": str(out / "scan_spectrum.png"), "label": "wideband scan spectrum"}]
    return summary, arts


def region_stats(freqs: np.ndarray, mean_db: np.ndarray, floor_db: np.ndarray, r: dict[str, Any]) -> dict[str, Any]:
    m = (freqs >= r["start_hz"]) & (freqs <= r["stop_hz"])
    if not m.any():
        return {"bins": 0}
    over = mean_db[m] - floor_db[m]
    return {"bins": int(m.sum()), "mean_dbfs_per_hz": _r(10 * np.log10(np.nanmean(10 ** (mean_db[m] / 10)))),
            "median_over_floor_db": _r(np.nanmedian(over)), "max_dbfs_per_hz": _r(np.nanmax(mean_db[m]))}


def _plot(data: dict[str, Any], path: pathlib.Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    s = data["spectrum"]
    f = np.array(s["freqs_hz"]) / 1e6
    fig, ax = plt.subplots(figsize=(14, 4.5), dpi=100)
    for r in data["summary"]["regions"]:
        ax.axvspan(r["start_hz"] / 1e6, r["stop_hz"] / 1e6, color="orange", alpha=0.12)
    ax.plot(f, np.array(s["peak_dbfs_per_hz"], float), lw=0.6, alpha=0.6, label="peak hold")
    ax.plot(f, np.array(s["mean_dbfs_per_hz"], float), lw=0.8, label="average")
    ax.plot(f, np.array(s["floor_dbfs_per_hz"], float), lw=0.8, ls="--", color="gray", label="noise floor estimate")
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("dBFS/Hz (uncalibrated)")
    ax.grid(alpha=0.3)
    ax.legend(loc="upper right")
    ax.set_title(f"Wideband scan, RBW {data['summary']['rbw_hz']} Hz, {data['summary']['sweeps']} sweep(s)", fontsize=9)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


# ==================================================================== comparing two scans
def load(npz: pathlib.Path) -> dict[str, np.ndarray]:
    with np.load(npz) as z:
        return {k: z[k] for k in z.files}


def _common(a: dict[str, np.ndarray], b: dict[str, np.ndarray]) -> tuple[np.ndarray, dict[str, np.ndarray], dict[str, np.ndarray]]:
    lo, hi = max(a["freqs"][0], b["freqs"][0]), min(a["freqs"][-1], b["freqs"][-1])
    if hi <= lo:
        raise ValueError("the two scans do not overlap in frequency")
    rb = max(float(np.diff(a["freqs"][:2])[0]), float(np.diff(b["freqs"][:2])[0]))
    grid = np.arange(lo, hi, rb)

    def on(d: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        out = {}
        for k in ("mean_db", "peak_db", "floor_db"):
            out[k] = np.interp(grid, d["freqs"], np.nan_to_num(d[k].astype(float), nan=np.nanmedian(d[k])))
        return out
    return grid, on(a), on(b)


def compare(a: dict[str, np.ndarray], b: dict[str, np.ndarray], regions: list[dict[str, Any]],
            min_signal_db: float = 6.0) -> dict[str, Any]:
    """B − A on a common grid. Bins where A shows no signal above its own floor cannot show filtering."""
    grid, A, B = _common(a, b)
    diff = B["mean_db"] - A["mean_db"]
    measurable = (A["mean_db"] - A["floor_db"]) >= min_signal_db
    floor_shift = float(np.median(B["floor_db"] - A["floor_db"]))
    k = max(1, int(math.ceil(len(grid) / MAX_DISPLAY)))
    m = (len(grid) // k) * k

    def dec(x: np.ndarray) -> list[Any]:
        return [_r(v) for v in x[:m].reshape(-1, k).mean(axis=1)]
    reg = []
    in_any = np.zeros(len(grid), bool)
    for r in regions:
        sel = (grid >= r["start_hz"]) & (grid <= r["stop_hz"])
        if not sel.any():
            continue
        in_any |= sel
        meas = sel & measurable
        reg.append({**r, "bins": int(sel.sum()), "measurable_bins": int(meas.sum()),
                    "median_change_db": _r(np.median(diff[meas])) if meas.any() else None,
                    "median_change_all_bins_db": _r(np.median(diff[sel])),
                    "fraction_reduced_3db": _r(np.mean(diff[meas] <= -3), 3) if meas.any() else None})
    outside = ~in_any
    meas_out = outside & measurable
    passband = {"bins": int(outside.sum()), "measurable_bins": int(meas_out.sum()),
                "median_change_db": _r(np.median(diff[meas_out])) if meas_out.any() else None,
                "median_change_all_bins_db": _r(np.median(diff[outside])) if outside.any() else None}
    car_a = carriers(grid, A["mean_db"], A["floor_db"], np.empty((0, 0)), float(grid[1] - grid[0]))
    per_car = []
    for c in car_a[:150]:
        i = int(np.argmin(np.abs(grid - c["frequency_hz"])))
        lo, hi = max(0, i - 2), min(len(grid), i + 3)
        d = float(B["mean_db"][lo:hi].max() - A["mean_db"][lo:hi].max())
        per_car.append({"frequency_hz": c["frequency_hz"], "a_dbfs_per_hz": c["power_dbfs_per_hz"],
                        "b_dbfs_per_hz": _r(B["mean_db"][lo:hi].max()), "change_db": _r(d),
                        "still_above_floor_db": _r(B["mean_db"][lo:hi].max() - B["floor_db"][i]),
                        "in_region": next((r["name"] for r in regions if r["start_hz"] <= c["frequency_hz"] <= r["stop_hz"]), None)})
    return {"grid": {"freqs_hz": dec(grid), "a_mean": dec(A["mean_db"]), "b_mean": dec(B["mean_db"]),
                     "a_floor": dec(A["floor_db"]), "b_floor": dec(B["floor_db"]), "diff_db": dec(diff),
                     "measurable": [bool(x) for x in measurable[:m].reshape(-1, k).any(axis=1)]},
            "regions": reg, "outside_regions": passband, "floor_shift_db": _r(floor_shift), "carriers": per_car,
            "carriers_reduced": sum(1 for c in per_car if (c["change_db"] or 0) <= -3),
            "carriers_increased": sum(1 for c in per_car if (c["change_db"] or 0) >= 3),
            "measurable_fraction": _r(float(measurable.mean()), 3), "rbw_hz": _r(float(grid[1] - grid[0]), 1)}


def filter_response(cmp: dict[str, Any], a: dict[str, np.ndarray], b: dict[str, np.ndarray],
                    stopband: dict[str, Any], min_signal_db: float = 6.0) -> dict[str, Any]:
    """Over-the-air comparative response of a filter: attenuation = A − B where A had signal to lose."""
    grid, A, B = _common(a, b)
    att = A["mean_db"] - B["mean_db"]
    meas = (A["mean_db"] - A["floor_db"]) >= min_signal_db
    sb = (grid >= stopband["start_hz"]) & (grid <= stopband["stop_hz"])
    margin = 0.15 * (stopband["stop_hz"] - stopband["start_hz"])
    pb = (grid < stopband["start_hz"] - margin) | (grid > stopband["stop_hz"] + margin)
    sm = meas & sb
    pm = meas & pb
    out: dict[str, Any] = {"stopband": stopband, "stopband_measurable_bins": int(sm.sum()),
                           "passband_measurable_bins": int(pm.sum())}
    out["stopband_median_attenuation_db"] = _r(np.median(att[sm])) if sm.any() else None
    out["stopband_p90_attenuation_db"] = _r(np.percentile(att[sm], 90)) if sm.any() else None
    out["passband_median_change_db"] = _r(-np.median(att[pm])) if pm.any() else None
    if meas.any():
        from scipy.ndimage import median_filter
        smooth = median_filter(np.where(meas, att, np.nan_to_num(np.nanmedian(att[meas]))), size=9)
        j = int(np.argmax(np.where(meas, smooth, -np.inf)))
        out["max_attenuation_db"] = _r(smooth[j])
        out["max_attenuation_frequency_hz"] = int(grid[j])
        half = (out["stopband_median_attenuation_db"] or 0) / 2
        trans = []
        if half > 1:
            for edge, direction in ((stopband["start_hz"], -1), (stopband["stop_hz"], 1)):
                idx = int(np.argmin(np.abs(grid - edge)))
                k = idx
                while 0 < k < len(grid) - 1 and (not meas[k] or smooth[k] > half):
                    k += direction
                    if abs(grid[k] - edge) > 3 * margin:
                        k = None
                        break
                trans.append({"edge_hz": int(edge), "half_attenuation_hz": int(grid[k]) if k else None})
        out["transitions"] = trans
    k = max(1, int(math.ceil(len(grid) / MAX_DISPLAY)))
    m = (len(grid) // k) * k
    out["curve"] = {"freqs_hz": [int(x) for x in grid[:m].reshape(-1, k).mean(axis=1)],
                    "attenuation_db": [_r(x) for x in np.where(meas, att, np.nan)[:m].reshape(-1, k).max(axis=1)]}
    return out
