"""Signal-path test metrics: what the /rf Test Lab shows and compares.

Input is the spectrogram the analysis engine already computed (rows x nfft, dBFS per bin, each row the
mean of several Hann-windowed FFTs; see analysis.spectrogram) plus a sampled pass over the raw IQ.
Nothing loads the whole recording.

Conventions (shown to the user as `methodology`):
  * Every level is dBFS: relative to the ADC's digital full scale. Nothing is calibrated to dBm.
  * Per-bin values are dBFS per FFT bin (RBW = sample_rate / fft_size).
  * Band and channel powers integrate bins and divide by the window's equivalent noise bandwidth
    (ENBW, 1.5 bins for Hann), so they are comparable with the time-domain RMS of the samples.
  * Noise floor = median across frequency of the time-averaged spectrum (robust to a few signals).
    The frequency-dependent floor is the 20th percentile over time of each bin.
  * SNR is reported only when the channel holds measurably more power than the noise floor predicts
    for that bandwidth; otherwise it is "unavailable" with the reason. No SNR is ever guessed.
"""

from __future__ import annotations

import base64
from typing import Any

import numpy as np

from . import analysis as an

PLOT_BINS = 1024
WATERFALL_ROWS = 256
SNR_MIN_EXCESS_DB = 1.0          # channel must exceed the floor's prediction by this before an SNR is given
TRANSIENT_DB = 6.0
DEFAULT_CHANNEL_HZ = 25_000


def _db(x: np.ndarray | float) -> Any:
    return 10 * np.log10(np.maximum(x, 1e-20))


def _r(x: Any, n: int = 2) -> Any:
    return None if x is None else round(float(x), n)


def _bin_reduce(lin: np.ndarray, bins: int) -> np.ndarray:
    """Average adjacent bins (in linear power) down to `bins` columns."""
    n = lin.shape[-1]
    k = max(1, n // bins)
    m = (n // k) * k
    return lin[..., :m].reshape(*lin.shape[:-1], m // k, k).mean(axis=-1)


def _row_reduce(a: np.ndarray, rows: int) -> np.ndarray:
    k = max(1, int(np.ceil(a.shape[0] / rows)))
    m = (a.shape[0] // k) * k
    if m == 0:
        return a
    return a[:m].reshape(m // k, k, *a.shape[1:]).mean(axis=1)


def enbw_bins(window: str, nfft: int) -> float:
    w = an.WINDOWS[window](nfft).astype(np.float64)
    return float(nfft * (w * w).sum() / w.sum() ** 2)


def iq_stats(rec: an.Recording, chunks: int = 120, n: int = 65536, hist_bins: int = 64) -> dict[str, Any]:
    """Sampled pass over the raw samples: ADC use, peak, RMS, DC, I/Q balance, histograms, a small scatter."""
    dtype, _ = an.DTYPES[rec.fmt]
    total = rec.samples
    chunks = max(1, min(chunks, total // n))
    full = {"cu8": 127.5, "cs8": 128.0, "cs16": 32768.0, "cf32": 1.0}[rec.fmt]
    codes = np.zeros(65536 if rec.fmt == "cs16" else 256, np.int64) if rec.fmt != "cf32" else None
    hi = np.zeros(hist_bins, np.int64)
    hq = np.zeros(hist_bins, np.int64)
    edges = np.linspace(-1, 1, hist_bins + 1)
    acc = np.zeros(5)
    cnt = clip = 0
    peak = 0.0
    scatter: list[list[float]] = []
    rng = np.random.default_rng(0)
    with rec.path.open("rb") as fh:
        for k in range(chunks):
            s = int(k * (total - n) / max(1, chunks - 1))
            fh.seek(s * an.bps(rec.fmt))
            raw = np.frombuffer(fh.read(n * an.bps(rec.fmt)), dtype=dtype)
            if raw.size < 2:
                continue
            if codes is not None:
                off = 32768 if rec.fmt == "cs16" else (128 if rec.fmt == "cs8" else 0)
                codes += np.bincount(raw.astype(np.int64) + off, minlength=codes.size)[:codes.size]
            if rec.fmt == "cu8":
                clip += int(np.count_nonzero((raw == 0) | (raw == 255)))
            elif rec.fmt == "cs16":
                clip += int(np.count_nonzero(np.abs(raw.astype(np.int32)) >= 32760))
            elif rec.fmt == "cs8":
                clip += int(np.count_nonzero(np.abs(raw.astype(np.int16)) >= 127))
            x = an.to_complex(raw.tobytes(), rec.fmt)
            i, q = x.real.astype(np.float64), x.imag.astype(np.float64)
            acc += [i.sum(), q.sum(), (i * i).sum(), (q * q).sum(), (i * q).sum()]
            cnt += len(x)
            peak = max(peak, float(np.abs(i).max()), float(np.abs(q).max()))
            hi += np.histogram(np.clip(i, -1, 1), edges)[0]
            hq += np.histogram(np.clip(q, -1, 1), edges)[0]
            if len(scatter) < 2000:
                pick = rng.choice(len(x), size=min(20, len(x)), replace=False)
                scatter += [[round(float(x[j].real), 4), round(float(x[j].imag), 4)] for j in pick]
    if cnt == 0:
        return {"samples_examined": 0}
    mi, mq = acc[0] / cnt, acc[1] / cnt
    vi, vq = acc[2] / cnt - mi * mi, acc[3] / cnt - mq * mq
    cov = acc[4] / cnt - mi * mq
    out: dict[str, Any] = {
        "samples_examined": cnt,
        "rms_dbfs": _r(_db(vi + vq)),
        "peak_sample_fraction_of_full_scale": _r(peak, 4),
        "peak_sample_dbfs": _r(20 * np.log10(max(peak, 1e-9))),
        "clipped_fraction": _r(clip / (cnt * 2), 7),
        "dc_offset_i": _r(mi, 5), "dc_offset_q": _r(mq, 5),
        "iq_amplitude_imbalance_db": _r(10 * np.log10(max(vi, 1e-20) / max(vq, 1e-20)), 3),
        "iq_correlation": _r(cov / np.sqrt(max(vi * vq, 1e-30)), 4),
        "histogram": {"edges": [round(float(e), 4) for e in edges], "i": hi.tolist(), "q": hq.tolist()},
        "scatter": scatter[:2000],
        "full_scale": full,
    }
    if codes is not None:
        used = int(np.count_nonzero(codes))
        out["adc_codes_used"] = used
        out["adc_codes_possible"] = int(codes.size)
        nz = np.nonzero(codes)[0]
        out["adc_effective_bits"] = _r(np.log2(max(2, nz[-1] - nz[0] + 1)), 2) if nz.size else None
    return out


def _intermod(signals: list[dict[str, Any]], rbw: float) -> list[dict[str, Any]]:
    """Hypotheses only: a detected peak sitting at 2*f1 - f2 of two stronger detected peaks."""
    strong = [s for s in signals if s["peak_over_floor_db"] >= 15][:8]
    hits = []
    tol = max(3 * rbw, 2_000)
    for a in strong:
        for b in strong:
            if a is b:
                continue
            f = 2 * a["frequency_hz"] - b["frequency_hz"]
            for s in signals:
                if s is a or s is b or s["peak_over_floor_db"] >= min(a["peak_over_floor_db"], b["peak_over_floor_db"]):
                    continue
                if abs(s["frequency_hz"] - f) <= tol:
                    hits.append({"frequency_hz": s["frequency_hz"], "from_hz": [a["frequency_hz"], b["frequency_hz"]],
                                 "note": "possible third-order intermodulation (2·f1 − f2); a hypothesis, not a measurement"})
    seen, out = set(), []
    for h in hits:
        if h["frequency_hz"] not in seen:
            seen.add(h["frequency_hz"])
            out.append(h)
    return out[:10]


def rf_test_metrics(rec: an.Recording, img: np.ndarray, times: np.ndarray, freqs: np.ndarray, floor: float,
                    p: dict[str, Any], res: dict[str, Any]) -> dict[str, Any]:
    window = p.get("fft_window", "hann")
    nfft = img.shape[1]
    rbw = rec.rate / nfft
    enbw = enbw_bins(window, nfft)
    lin = 10 ** (img.astype(np.float64) / 10)                 # rows x bins, linear power per bin
    mean_lin = lin.mean(axis=0)
    mean_db = _db(mean_lin)
    peak_db = img.max(axis=0)
    floor_by_bin = np.percentile(img, 20, axis=0)             # dBFS/bin, frequency-dependent floor
    floor_lin = 10 ** (floor / 10)

    # ---- channel (target) power and SNR
    tgt = p.get("target_frequency_hz") or rec.target
    ch_bw = float(p.get("channel_bandwidth_hz") or DEFAULT_CHANNEL_HZ)
    channel: dict[str, Any] = {"measured": False}
    ch_mask = None
    if tgt and freqs[0] <= tgt <= freqs[-1]:
        ch_mask = np.abs(freqs - tgt) <= max(ch_bw / 2, rbw / 2)
        nb = int(ch_mask.sum())
        ch_series = _db(lin[:, ch_mask].sum(axis=1) / enbw)
        p_ch = float(mean_lin[ch_mask].sum() / enbw)
        outside = ~(np.abs(freqs - tgt) <= max(ch_bw, 3 * rbw))
        ref_floor = float(np.median(mean_db[outside])) if outside.any() else floor
        p_noise = 10 ** (ref_floor / 10) * nb / enbw
        excess = 10 * np.log10(p_ch / p_noise)
        channel = {"measured": True, "target_frequency_hz": int(tgt), "bandwidth_hz": int(ch_bw), "bins": nb,
                   "mean_power_dbfs": _r(_db(p_ch)), "peak_power_dbfs": _r(ch_series.max()),
                   "power_over_noise_db": _r(excess),
                   "noise_reference_dbfs_per_bin": _r(ref_floor)}
        if excess >= SNR_MIN_EXCESS_DB:
            snr = 10 * np.log10(max(p_ch - p_noise, 1e-30) / p_noise)
            channel["snr_db"] = _r(snr, 1)
            channel["snr_method"] = (f"(channel power − predicted noise) / predicted noise, over {int(ch_bw)} Hz around the "
                                     f"target. Noise = median of the time-averaged spectrum outside ±{int(ch_bw)} Hz "
                                     f"({ref_floor:.1f} dBFS/bin) × {nb} bins / ENBW {enbw:.2f}.")
        else:
            channel["snr_db"] = None
            channel["snr_unavailable"] = (f"SNR unavailable: the channel holds only {excess:.1f} dB more power than the "
                                          "noise floor predicts, so no signal can be separated from the noise.")
    else:
        ch_series = None
        channel["note"] = "no target frequency inside the recorded band; channel power and SNR not measured"

    # ---- time series (each spectrogram row)
    band_series = _db(lin.sum(axis=1) / enbw)
    peak_series = img.max(axis=1)
    floor_series = np.median(img, axis=1)
    med_band = float(np.median(band_series))
    events, start = [], None
    for k, v in enumerate(band_series):
        hot = v > med_band + TRANSIENT_DB
        if hot and start is None:
            start = k
        if (not hot or k == len(band_series) - 1) and start is not None:
            end = k if hot else k - 1
            events.append({"start_s": _r(times[start], 2), "end_s": _r(times[end], 2),
                           "peak_dbfs": _r(band_series[start:end + 1].max()),
                           "over_median_db": _r(band_series[start:end + 1].max() - med_band, 1)})
            start = None
    floor_med = float(np.median(floor_series))
    floor_rises = int(np.count_nonzero(floor_series > floor_med + 3))

    # ---- spectral shape
    keep = np.abs(freqs - rec.center) > rbw * 3                  # leave the DC bin out of shape metrics
    m = mean_lin[keep]
    flatness = float(np.exp(np.mean(np.log(np.maximum(m, 1e-30)))) / m.mean())
    lower = mean_lin[(freqs < rec.center) & keep]
    upper = mean_lin[(freqs > rec.center) & keep]
    signals = res.get("signals") or an.detect_signals(img, times, freqs, floor, float(p.get("threshold_db", 6.0)))
    for s in signals:
        s["kind"] = ("persistent" if s["duty_cycle"] >= 0.8 else "intermittent" if s["duty_cycle"] >= 0.2 else "transient")
        if s["bandwidth_hz"] <= 3 * rbw and s["peak_over_floor_db"] >= 20:
            s["kind"] += ", strong narrow carrier"
        if s.get("note"):
            s["kind"] += " (DC spike?)"
        s["mean_power_dbfs_per_bin"] = _r(mean_db[int(np.argmin(np.abs(freqs - s["frequency_hz"])))])
    spurs = [s for s in signals if s["duty_cycle"] >= 0.9 and s["bandwidth_hz"] <= 3 * rbw]
    ov = res.get("overload") or {}

    # ---- arrays for the browser (plots, overlays and differences)
    red_mean = _db(_bin_reduce(mean_lin, PLOT_BINS))
    red_peak = _bin_reduce(peak_db, PLOT_BINS)                 # already dB; averaging is fine for display
    red_floor = _bin_reduce(floor_by_bin, PLOT_BINS)
    red_freqs = _bin_reduce(freqs, PLOT_BINS)
    wf = _db(_row_reduce(_bin_reduce(lin, PLOT_BINS), WATERFALL_ROWS))
    wf_times = _row_reduce(times, WATERFALL_ROWS)
    lo, hi = float(np.percentile(wf, 1)), float(np.percentile(wf, 99.9))
    q8 = np.clip((wf - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)

    iq = iq_stats(rec)
    summary = {
        "units": "dBFS (digital full scale; uncalibrated, not dBm)",
        "mean_power_dbfs": iq.get("rms_dbfs"),
        "peak_power_dbfs": _r(band_series.max()),
        "band_power_dbfs": _r(_db(mean_lin.sum() / enbw)),
        "noise_floor_dbfs_per_bin": _r(floor),
        "noise_floor_dbfs_per_hz": _r(floor - 10 * np.log10(rbw)),
        "noise_floor_variation_db": _r(np.percentile(mean_db[keep], 75) - np.percentile(mean_db[keep], 25)),
        "noise_floor_time_std_db": _r(np.std(floor_series)),
        "channel": channel,
        "snr_db": channel.get("snr_db"),
        "clipped_fraction": iq.get("clipped_fraction"),
        "peak_sample_fraction_of_full_scale": iq.get("peak_sample_fraction_of_full_scale"),
        "adc_codes_used": iq.get("adc_codes_used"), "adc_codes_possible": iq.get("adc_codes_possible"),
        "dc_offset_i": iq.get("dc_offset_i"), "dc_offset_q": iq.get("dc_offset_q"),
        "iq_amplitude_imbalance_db": iq.get("iq_amplitude_imbalance_db"), "iq_correlation": iq.get("iq_correlation"),
        "signals_detected": len(signals),
        "strong_peaks": sum(1 for s in signals if s["peak_over_floor_db"] >= 15),
        "persistent_spurs": len(spurs),
        "spectral_flatness": _r(flatness, 4),
        "peak_to_average_db": _r(_db(m.max() / m.mean())),
        "spectral_asymmetry_db": _r(_db(upper.mean()) - _db(lower.mean())) if lower.size and upper.size else None,
        "occupied_bandwidth_hz": (res.get("spectrum") or {}).get("occupied_bandwidth_hz"),
        "band_power_time_std_db": _r(np.std(band_series)),
        "transient_events": len(events),
        "broadband_noise_rises": floor_rises,
        "findings": ov.get("findings") or [],
        "rbw_hz": _r(rbw, 1), "fft_size": nfft, "window": window, "enbw_bins": _r(enbw, 3),
    }
    data = {
        "version": 1,
        "summary": summary,
        "quality": quality_notes(summary, rec),
        "methodology": __doc__.split("Conventions (shown to the user as `methodology`):")[1].strip(),
        "recording": {"sample_rate_hz": rec.rate, "center_frequency_hz": rec.center, "format": rec.fmt,
                      "seconds": _r(rec.seconds, 2), "samples": rec.samples},
        "signals": signals,
        "spurs": spurs,
        "intermod_hypotheses": _intermod(signals, rbw),
        "transients": events[:50],
        "iq": iq,
        "spectrum": {"freqs_hz": [int(f) for f in red_freqs], "mean_dbfs": [_r(v) for v in red_mean],
                     "peak_dbfs": [_r(v) for v in red_peak], "floor_dbfs": [_r(v) for v in red_floor],
                     "noise_floor_dbfs": _r(floor), "bin_hz": _r(rbw * (nfft / len(red_freqs)), 1),
                     "per": "dBFS per original FFT bin (RBW above), averaged into display columns"},
        "time": {"t_s": [_r(t, 2) for t in times], "band_dbfs": [_r(v) for v in band_series],
                 "peak_bin_dbfs": [_r(v) for v in peak_series], "floor_dbfs": [_r(v) for v in floor_series],
                 "channel_dbfs": [_r(v) for v in ch_series] if ch_series is not None else None},
        "waterfall": {"rows": int(q8.shape[0]), "cols": int(q8.shape[1]), "min_dbfs": _r(lo), "max_dbfs": _r(hi),
                      "t_start_s": _r(wf_times[0]), "t_end_s": _r(wf_times[-1]),
                      "f_start_hz": int(red_freqs[0]), "f_end_hz": int(red_freqs[-1]),
                      "u8_b64": base64.b64encode(q8.tobytes()).decode()},
    }
    from .interference import diagnose
    data["investigation"] = diagnose(data, str(p.get("preset") or "general"))
    return data


def quality_notes(s: dict[str, Any], rec: an.Recording) -> list[dict[str, str]]:
    """Plain statements, each tagged with how solid it is: measured / interpretation."""
    out = []
    clip = s.get("clipped_fraction") or 0
    if clip > 1e-4:
        out.append({"basis": "measured", "text": f"{clip * 100:.3f}% of samples are at digital full scale (clipping). "
                    "Lower the gain before comparing anything else."})
    elif clip > 0:
        out.append({"basis": "measured", "text": f"Occasional clipping ({clip * 100:.4f}% of samples)."})
    else:
        out.append({"basis": "measured", "text": "No digital clipping in the examined samples. That does not prove the "
                    "analog front end (LNA, mixer) is free of compression."})
    used, possible = s.get("adc_codes_used"), s.get("adc_codes_possible")
    if rec.fmt in ("cu8", "cs8") and used is not None and used < 24:
        out.append({"basis": "measured", "text": f"Only {used} of {possible} ADC codes in use: the signal barely moves the "
                    "ADC (gain too low, or a receiver that is not delivering samples)."})
    ch = s.get("channel") or {}
    if ch.get("measured"):
        if ch.get("snr_db") is not None:
            out.append({"basis": "measured", "text": f"Channel at {ch['target_frequency_hz'] / 1e6:.4f} MHz: estimated SNR "
                        f"{ch['snr_db']:.1f} dB over {ch['bandwidth_hz'] / 1e3:.1f} kHz."})
        else:
            out.append({"basis": "measured", "text": ch.get("snr_unavailable", "SNR unavailable")})
    if (s.get("persistent_spurs") or 0) > 0:
        out.append({"basis": "measured", "text": f"{s['persistent_spurs']} narrow carrier(s) present the whole time; "
                    "these are often local interference or the receiver's own spurs."})
    if (s.get("transient_events") or 0) > 0:
        out.append({"basis": "measured", "text": f"{s['transient_events']} short burst(s) more than {TRANSIENT_DB:.0f} dB "
                    "above the typical band power."})
    for f in s.get("findings") or []:
        if f.get("status") in ("present", "suspected") and f.get("finding") not in ("adc_clipping",):
            out.append({"basis": "measured" if f["status"] == "present" else "interpretation",
                        "text": f"{f['finding'].replace('_', ' ')}: {f['detail']}"})
    return out
