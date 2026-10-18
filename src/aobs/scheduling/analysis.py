"""The one RF analysis engine (used by /radio-analyze, /radio-waterfall, comparisons and post-recording).

All power figures are relative to digital full scale (dBFS) or to the measured noise floor (dB);
none of them is a calibrated received power. Every finding says whether it was measured
directly or is only suspected from indirect evidence.
"""

from __future__ import annotations

import pathlib
from typing import Any

import numpy as np

from .dsp import DTYPES, to_complex

WINDOWS = {"hann": np.hanning, "hamming": np.hamming, "blackman": np.blackman, "rect": np.ones}
COLORMAPS = ("viridis", "inferno", "magma", "plasma", "gray", "turbo")
TASKS = ("waterfall", "spectrum", "signal_detection", "frequency_offset", "signal_quality", "clipping",
         "noise_floor", "integrity", "checksum", "rf_test", "rf_scan", "rf_gain_sweep")
DEFAULT_TASKS = ["waterfall", "spectrum", "signal_detection", "signal_quality", "clipping", "integrity"]


def bps(fmt: str) -> int:
    dtype, comps = DTYPES[fmt]
    return np.dtype(dtype).itemsize * comps


class Recording:
    def __init__(self, inp: dict[str, Any]):
        self.path = pathlib.Path(inp["path"])
        self.fmt = inp["format"]
        self.rate = int(inp["sample_rate_hz"])
        self.center = int(inp["center_frequency_hz"])
        self.target = inp.get("target_frequency_hz")
        self.meta = inp
        self.size = self.path.stat().st_size
        self.samples = self.size // bps(self.fmt)
        self.seconds = self.samples / self.rate

    def span(self, t0: float | None, t1: float | None) -> tuple[int, int]:
        a = int((t0 or 0) * self.rate)
        b = min(self.samples, int(t1 * self.rate)) if t1 else self.samples
        if not 0 <= a < b:
            raise ValueError(f"time range {t0}-{t1} s is outside the recording ({self.seconds:.1f} s)")
        return a, b

    def read(self, fh: Any, start: int, n: int) -> np.ndarray:
        fh.seek(start * bps(self.fmt))
        return to_complex(fh.read(n * bps(self.fmt)), self.fmt)


def spectrogram(rec: Recording, nfft: int, rows: int, t0: float | None, t1: float | None, window: str = "hann",
                avg: int = 8, overlap: float = 0.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """rows x nfft power (dBFS per bin) at evenly spaced times, each row the mean of `avg` FFTs
    stepped by nfft*(1-overlap). Returns (img, row_center_times_s, bin_frequencies_hz). Streams the file."""
    a, b = rec.span(t0, t1)
    hop = max(1, int(nfft * (1 - overlap)))
    need = nfft + hop * (avg - 1)
    if b - a < need:
        raise ValueError("selected time range is too short for this FFT size")
    rows = max(1, min(rows, (b - a) // need))
    win = WINDOWS[window](nfft).astype(np.float32)
    norm = (win.sum() ** 2)
    img = np.empty((rows, nfft), np.float32)
    times = np.empty(rows)
    with rec.path.open("rb") as fh:
        for i in range(rows):
            s = a + int(i * (b - a - need) / max(1, rows - 1))
            x = rec.read(fh, s, need)
            idx = np.arange(avg)[:, None] * hop + np.arange(nfft)[None, :]
            p = np.abs(np.fft.fftshift(np.fft.fft(x[idx] * win, axis=1), axes=1)) ** 2 / norm
            img[i] = 10 * np.log10(p.mean(axis=0) + 1e-15)
            times[i] = (s + need / 2) / rec.rate
    freqs = rec.center + np.fft.fftshift(np.fft.fftfreq(nfft, 1 / rec.rate))
    return img, times, freqs


def crop(img: np.ndarray, freqs: np.ndarray, fmin: float | None, fmax: float | None) -> tuple[np.ndarray, np.ndarray]:
    if fmin is None and fmax is None:
        return img, freqs
    keep = (freqs >= (fmin if fmin is not None else -np.inf)) & (freqs <= (fmax if fmax is not None else np.inf))
    if keep.sum() < 8:
        raise ValueError("frequency range is narrower than 8 FFT bins; raise fft_size or widen the range")
    return img[:, keep], freqs[keep]


def caption_lines(inp: dict[str, Any], start_s: float | None = None, end_s: float | None = None) -> list[str]:
    """Footer burned into every plot, so an image saved or forwarded on its own still says what it shows."""
    import datetime as dt
    import zoneinfo

    tz = zoneinfo.ZoneInfo(inp.get("display_tz") or "America/New_York")
    line1 = " · ".join(x for x in (
        inp.get("receiver_id") or "receiver ?", inp.get("antenna") or None, inp.get("target") or "target ?",
        f"recording {inp['recording_id']}" if inp.get("recording_id") else None) if x)
    when = "time ?"
    if inp.get("record_start"):
        a = dt.datetime.fromisoformat(str(inp["record_start"]).replace("Z", "+00:00"))
        b = dt.datetime.fromisoformat(str(inp["record_end"]).replace("Z", "+00:00")) if inp.get("record_end") else None
        when = (f"{a.astimezone(tz):%Y-%m-%d %-I:%M:%S %p}" + (f"–{b.astimezone(tz):%-I:%M:%S %p}" if b else "")
                + f" {a.astimezone(tz):%Z}")
    gain = inp.get("gain")
    if isinstance(gain, dict):
        gain = ", ".join(f"{k} {v}" for k, v in gain.items())
    bits = [when, f"center {inp['center_frequency_hz'] / 1e6:.3f} MHz" if inp.get("center_frequency_hz") else None,
            f"target {inp['target_frequency_hz'] / 1e6:.3f} MHz" if inp.get("target_frequency_hz") else None,
            f"{int(inp['sample_rate_hz']) / 1e6:.3f} MS/s {inp.get('format', '')}" if inp.get("sample_rate_hz") else None,
            f"gain {gain}" + (" dB" if isinstance(gain, (int, float)) else "") if gain not in (None, "") else None]
    if start_s is not None or end_s is not None:
        bits.append(f"showing {start_s or 0:g}–{end_s if end_s is not None else 'end'} s")
    return [line1, " · ".join(b for b in bits if b)]


def add_caption(fig: Any, lines: list[str]) -> None:
    """Reserve a strip at the bottom of the figure and write the caption into it."""
    h = fig.get_figheight()
    strip = min(0.5, (0.22 * len(lines) + 0.1) / h)
    fig.tight_layout(rect=(0, strip, 1, 1))
    fig.text(0.01, strip * 0.45, "\n".join(lines), fontsize=8, family="monospace", va="center", ha="left",
             bbox={"boxstyle": "round,pad=0.3", "facecolor": "#f2f2f2", "edgecolor": "#bbbbbb"})


def plot_waterfall(img: np.ndarray, times: np.ndarray, freqs: np.ndarray, path: pathlib.Path, title: str,
                   dynamic_range_db: float | None, cmap: str, width: int, height: int,
                   caption: list[str] | None = None) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    hi = float(np.percentile(img, 99.8))
    lo = hi - dynamic_range_db if dynamic_range_db else float(np.percentile(img, 2))
    fig, ax = plt.subplots(figsize=(width / 100, height / 100), dpi=100)
    df = (freqs[1] - freqs[0]) / 2
    dt_ = (times[1] - times[0]) / 2 if len(times) > 1 else 0.5
    im = ax.imshow(img, aspect="auto", cmap=cmap, vmin=lo, vmax=hi, interpolation="nearest",
                   extent=((freqs[0] - df) / 1e6, (freqs[-1] + df) / 1e6, times[-1] + dt_, times[0] - dt_))
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("Time from recording start (s)")
    ax.set_title(title, fontsize=9)
    fig.colorbar(im, ax=ax, label="dBFS per bin (relative, uncalibrated)")
    if caption:
        add_caption(fig, caption)
    else:
        fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def spectrum_metrics(img: np.ndarray, freqs: np.ndarray, rbw: float) -> dict[str, Any]:
    mean = 10 * np.log10(np.mean(10 ** (img / 10), axis=0))
    peak = img.max(axis=0)
    floor = float(np.median(mean))
    i = int(np.argmax(mean))
    # occupied bandwidth of the strongest signal: contiguous bins within 10 dB of its peak and >3 dB over floor
    lo = hi = i
    while lo > 0 and mean[lo - 1] > max(mean[i] - 10, floor + 3):
        lo -= 1
    while hi < len(mean) - 1 and mean[hi + 1] > max(mean[i] - 10, floor + 3):
        hi += 1
    return {"noise_floor_dbfs_per_bin": round(floor, 2), "resolution_bandwidth_hz": round(rbw, 1),
            "noise_floor_dbfs_per_hz": round(floor - 10 * np.log10(rbw), 2),
            "peak_frequency_hz": int(freqs[i]), "peak_power_dbfs_per_bin": round(float(mean[i]), 2),
            "peak_snr_db": round(float(mean[i] - floor), 2),
            "peak_hold_max_over_floor_db": round(float(peak.max() - floor), 2),
            "occupied_bandwidth_hz": int((hi - lo + 1) * rbw) if mean[i] - floor > 3 else 0,
            "_mean": mean, "_peak": peak, "_floor": floor}


def detect_signals(img: np.ndarray, times: np.ndarray, freqs: np.ndarray, floor: float, threshold_db: float = 6.0,
                   limit: int = 25) -> list[dict[str, Any]]:
    """Candidate signals: frequency clusters whose peak-hold exceeds the floor, with their activity in time.
    Nothing here identifies what a signal is."""
    rbw = float(freqs[1] - freqs[0])
    over = img.max(axis=0) > floor + threshold_db
    clusters, i = [], 0
    while i < len(over):
        if over[i]:
            j = i
            while j + 1 < len(over) and (over[j + 1] or (j + 2 < len(over) and over[j + 2])):
                j += 1
            clusters.append((i, j))
            i = j + 1
        else:
            i += 1
    center_bin = int(np.argmin(np.abs(freqs - (freqs[0] + freqs[-1]) / 2 - rbw / 2)))
    out = []
    for lo, hi in clusters:
        band = img[:, lo:hi + 1].max(axis=1)
        active = band > floor + threshold_db
        if not active.any():
            continue
        snr = float(band.max() - floor)
        k = lo + int(np.argmax(img[:, lo:hi + 1].max(axis=0)))
        rows_active = int(active.sum())
        conf = "high" if snr >= 15 and rows_active >= 3 else "medium" if snr >= 10 and rows_active >= 2 else "low"
        entry = {"frequency_hz": int(freqs[k]), "bandwidth_hz": int((hi - lo + 1) * rbw),
                 "start_s": round(float(times[active.argmax()]), 2),
                 "end_s": round(float(times[len(active) - 1 - active[::-1].argmax()]), 2),
                 "duty_cycle": round(rows_active / len(active), 3), "peak_over_floor_db": round(snr, 1),
                 "confidence": conf}
        if lo <= center_bin <= hi and hi - lo <= 3:
            entry["note"] = "at the tuned center: possibly the receiver's DC spike, not a signal"
        out.append(entry)
    return sorted(out, key=lambda e: -e["peak_over_floor_db"])[:limit]


def frequency_track(rec: Recording, around_hz: float, search_hz: float, t0: float | None, t1: float | None,
                    nfft: int = 16384, points: int = 120) -> dict[str, Any]:
    """Measured carrier/centroid frequency over time near `around_hz` (parabolic peak interpolation)."""
    a, b = rec.span(t0, t1)
    points = max(4, min(points, (b - a) // nfft))
    win = np.hanning(nfft)
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / rec.rate)) + rec.center
    sel = np.abs(f - around_hz) <= search_hz
    if sel.sum() < 5:
        raise ValueError("search window too narrow for this FFT size")
    idx = np.where(sel)[0]
    ts, fs, snrs = [], [], []
    with rec.path.open("rb") as fh:
        for k in range(points):
            s = a + int(k * (b - a - nfft) / max(1, points - 1))
            p = np.abs(np.fft.fftshift(np.fft.fft(rec.read(fh, s, nfft) * win))) ** 2
            seg = 10 * np.log10(p[idx] + 1e-15)
            j = int(np.argmax(seg))
            off = 0.0
            if 0 < j < len(seg) - 1:
                y0, y1, y2 = seg[j - 1], seg[j], seg[j + 1]
                den = y0 - 2 * y1 + y2
                off = 0.5 * (y0 - y2) / den if den else 0.0
            ts.append((s + nfft / 2) / rec.rate)
            fs.append(f[idx[j]] + off * rec.rate / nfft)
            snrs.append(float(seg[j] - np.median(seg)))
    ts_a, fs_a, sn = np.array(ts), np.array(fs), np.array(snrs)
    good = sn >= 10
    out: dict[str, Any] = {"search_center_hz": int(around_hz), "search_half_width_hz": int(search_hz),
                           "points": len(ts), "points_with_signal": int(good.sum()), "_t": ts_a, "_f": fs_a, "_good": good}
    if good.sum() >= 4:
        slope, icpt = np.polyfit(ts_a[good], fs_a[good], 1)
        resid = fs_a[good] - (slope * ts_a[good] + icpt)
        out.update(measured=True, mean_offset_from_search_center_hz=round(float(fs_a[good].mean() - around_hz), 1),
                   drift_hz_per_s=round(float(slope), 2), excursion_hz=round(float(np.ptp(fs_a[good])), 1),
                   fit_residual_rms_hz=round(float(np.sqrt(np.mean(resid ** 2))), 1),
                   doppler_like=bool(abs(slope) > 5 and np.ptp(fs_a[good]) > 500),
                   note="measured drift of the strongest component in the window; not a Doppler prediction")
    else:
        out.update(measured=False, note="no component stood 10 dB over the local floor often enough to track")
    return out


def overload(rec: Recording, t0: float | None, t1: float | None, img: np.ndarray | None, freqs: np.ndarray | None,
             floor: float | None, chunks: int = 300, n: int = 65536) -> dict[str, Any]:
    dtype, _ = DTYPES[rec.fmt]
    a, b = rec.span(t0, t1)
    chunks = max(1, min(chunks, (b - a) // n))
    clip = cnt = zero_runs = 0
    acc = np.zeros(5)
    raw_acc = np.zeros(2)                  # sum, sum of squares of raw integer values (ADC usage)
    pw, tt = [], []
    with rec.path.open("rb") as fh:
        for k in range(chunks):
            s = a + int(k * (b - a - n) / max(1, chunks - 1))
            fh.seek(s * bps(rec.fmt))
            raw = np.frombuffer(fh.read(n * bps(rec.fmt)), dtype=dtype)
            if rec.fmt == "cu8":
                clip += int(np.count_nonzero((raw == 0) | (raw == 255)))
            elif rec.fmt == "cs16":
                clip += int(np.count_nonzero(np.abs(raw.astype(np.int32)) >= 32760))
            elif rec.fmt == "cs8":
                clip += int(np.count_nonzero(np.abs(raw.astype(np.int16)) >= 127))
            if rec.fmt != "cf32":
                rf = raw.astype(np.float64)
                raw_acc += [rf.sum(), (rf * rf).sum()]
            x = to_complex(raw.tobytes(), rec.fmt)
            if rec.fmt == "cs16" and np.count_nonzero(raw == 0) > 0.5 * raw.size:
                zero_runs += 1
            i, q = x.real.astype(np.float64), x.imag.astype(np.float64)
            acc += [i.sum(), q.sum(), (i * i).sum(), (q * q).sum(), (i * q).sum()]
            cnt += len(x)
            pw.append(float(10 * np.log10(np.mean(np.abs(x) ** 2) + 1e-15)))
            tt.append(s / rec.rate)
    mi, mq = acc[0] / cnt, acc[1] / cnt
    vi, vq, cov = acc[2] / cnt - mi * mi, acc[3] / cnt - mq * mq, acc[4] / cnt - mi * mq
    clipped = clip / (cnt * 2)
    rms = float(10 * np.log10(vi + vq + 1e-15))
    peak_rms = max(pw)
    findings = []

    def add(name: str, status: str, basis: str, detail: str) -> None:
        findings.append({"finding": name, "status": status, "basis": basis, "detail": detail})
    add("adc_clipping", "present" if clipped > 1e-5 else "not detected", "measured",
        f"{clipped * 100:.4f}% of I/Q values at full scale")
    if clipped > 1e-4 or peak_rms > -6:
        add("excessive_gain", "suspected", "inferred from clipping/level", f"peak RMS {peak_rms:.1f} dBFS; reduce gain")
    if img is not None and freqs is not None and floor is not None:
        mean = 10 * np.log10(np.mean(10 ** (img / 10), axis=0))
        c = int(np.argmin(np.abs(freqs - rec.center)))
        nb = np.r_[mean[max(0, c - 40):max(0, c - 5)], mean[c + 5:c + 40]]
        spike = float(mean[c] - np.median(nb)) if nb.size else 0.0
        add("dc_spike", "present" if spike > 6 else "not detected", "measured", f"center bin {spike:.1f} dB over its neighbours")
        edges = float(np.median(mean[: len(mean) // 10]) + np.median(mean[-len(mean) // 10:])) / 2
        mid = float(np.median(mean[len(mean) // 3: 2 * len(mean) // 3]))
        strong = mean - floor > 30
        if strong.any():
            add("strong_interference", "present", "measured", f"{int(strong.sum())} bins more than 30 dB over the floor")
        if mid - floor > 3 and clipped > 1e-5:
            add("broadband_overload", "suspected", "inferred", "raised floor across the band together with clipping")
        pk = np.where(img.max(axis=0) - floor > 12)[0]
        if len(pk) >= 4:
            sp = np.diff(freqs[pk])
            sp = sp[sp > 3 * (freqs[1] - freqs[0])]
            if len(sp) >= 3 and np.std(sp) < 0.05 * np.mean(sp):
                add("intermodulation_or_spur_comb", "suspected", "inferred from evenly spaced peaks",
                    f"{len(pk)} peaks spaced ~{np.mean(sp) / 1e3:.1f} kHz")
        add("filter_rolloff", "info", "measured", f"band edges {edges - mid:+.1f} dB relative to mid-band")
    imb = float(10 * np.log10((vi + 1e-15) / (vq + 1e-15)))
    corr = float(cov / np.sqrt(vi * vq + 1e-30))
    add("iq_imbalance", "present" if abs(imb) > 0.5 or abs(corr) > 0.05 else "not detected", "measured",
        f"amplitude {imb:+.3f} dB, I/Q correlation {corr:+.4f}")
    if rec.fmt != "cf32" and cnt:
        n_vals = cnt * 2
        std_lsb = float(np.sqrt(max(0.0, raw_acc[1] / n_vals - (raw_acc[0] / n_vals) ** 2)))
        low = {"cu8": 3.0, "cs8": 3.0, "cs16": 16.0}[rec.fmt]
        add("adc_underdriven", "present" if std_lsb < low else "not detected", "measured",
            f"sample spread {std_lsb:.2f} LSB (below {low:g} LSB the ADC barely moves; raise the gain)")
    if zero_runs:
        add("zero_filled_blocks", "present", "measured", f"{zero_runs} sampled blocks mostly zeros (possible dropped samples)")
    return {"clipped_fraction": round(clipped, 7), "rms_dbfs": round(rms, 2), "peak_block_rms_dbfs": round(peak_rms, 2),
            "dc_offset_i": round(mi, 5), "dc_offset_q": round(mq, 5), "iq_amplitude_imbalance_db": round(imb, 3),
            "iq_correlation": round(corr, 4), "findings": findings, "_t": tt, "_p": pw}


def integrity(rec: Recording) -> dict[str, Any]:
    meta = rec.meta
    exp = meta.get("expected_bytes")
    issues = []
    if rec.size % bps(rec.fmt):
        issues.append(f"file size is not a whole number of {rec.fmt} samples (truncated write)")
    if exp:
        ratio = rec.size / exp
        if ratio < 0.98:
            issues.append(f"{rec.size} of {exp} expected bytes ({ratio * 100:.1f}%): truncated or dropped samples")
    dur = meta.get("expected_seconds")
    if dur and abs(rec.seconds - dur) > max(2.0, 0.02 * dur):
        issues.append(f"duration {rec.seconds:.1f} s vs {dur:.1f} s scheduled")
    for k in ("capture_overflows",):
        if meta.get(k):
            issues.append(f"{meta[k]} driver overflow event(s) during capture (samples dropped)")
    if meta.get("metadata_bytes") is not None and meta["metadata_bytes"] != rec.size:
        issues.append(f"metadata says {meta['metadata_bytes']} bytes, file has {rec.size}")
    return {"file": str(rec.path), "bytes": rec.size, "expected_bytes": exp, "samples": rec.samples,
            "duration_s": round(rec.seconds, 3), "format": rec.fmt, "sample_rate_hz": rec.rate,
            "consistent": not issues, "issues": issues}
