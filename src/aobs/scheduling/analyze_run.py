"""Analysis attempts: run the analysis engine over one recording, or compare several."""

from __future__ import annotations

import pathlib
from typing import Any

import numpy as np

from . import analysis as an
from .postprocess import sha256


def _plt() -> Any:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def analyze(inp: dict[str, Any], p: dict[str, Any], out: pathlib.Path, tag: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Run the requested analyses on one recording. Parameters arrive validated by the processor."""
    tasks = set(p.get("analysis") or an.DEFAULT_TASKS)
    if tasks & {"rf_scan", "rf_gain_sweep"}:          # stepped captures: analysed step by step from <raw>.steps.json
        import json
        idx_path = pathlib.Path(str(inp["path"]) + ".steps.json")
        index = json.loads(idx_path.read_text())
        res: dict[str, Any] = {"input": str(inp["path"]), "steps": len(index["steps"])}
        arts: list[dict[str, str]] = [{"kind": "metadata", "path": str(idx_path), "label": "step index"}]
        if "rf_scan" in tasks:
            from . import scanmetrics
            res["rf_scan"], a = scanmetrics.analyze(pathlib.Path(inp["path"]), inp["format"], index, p["scan"], out)
            arts += a
        if "rf_gain_sweep" in tasks:
            from . import gainsweep
            res["rf_gain_sweep"], a = gainsweep.analyze(pathlib.Path(inp["path"]), inp["format"], index, p, out)
            arts += a
        return res, arts
    plt = _plt()
    rec = an.Recording(inp)
    cap = an.caption_lines(inp, p.get("start_s"), p.get("end_s"))
    nfft, rows = int(p.get("fft_size", 2048)), int(p.get("rows", 600))
    t0, t1 = p.get("start_s"), p.get("end_s")
    fmin, fmax = p.get("frequency_min_hz"), p.get("frequency_max_hz")
    res: dict[str, Any] = {"input": str(rec.path), "recording_seconds": round(rec.seconds, 2)}
    arts: list[dict[str, str]] = []
    rng = f" {fmin / 1e6:.4f}-{fmax / 1e6:.4f} MHz" if fmin and fmax else ""
    img = freqs = None
    floor = None
    if "checksum" in tasks:
        res["sha256"] = sha256(rec.path)
    if tasks & {"waterfall", "spectrum", "signal_detection", "noise_floor", "signal_quality", "clipping", "rf_test"}:
        img_full, times, freqs_full = an.spectrogram(rec, nfft, rows, t0, t1, p.get("fft_window", "hann"),
                                                     int(p.get("averages", 8)), float(p.get("overlap", 0.0)))
        img, freqs = an.crop(img_full, freqs_full, fmin, fmax)
        rbw = rec.rate / nfft
        m = an.spectrum_metrics(img, freqs, rbw)
        floor = m["_floor"]
        res.update(fft_size=nfft, fft_window=p.get("fft_window", "hann"), rows=int(img.shape[0]),
                   time_range_s=[round(float(times[0]), 2), round(float(times[-1]), 2)],
                   units="dBFS (relative to digital full scale; not calibrated received power)")
        if tasks & {"spectrum", "noise_floor", "signal_quality"}:
            res["spectrum"] = {k: v for k, v in m.items() if not k.startswith("_")}
            tgt = p.get("target_frequency_hz") or rec.target
            if tgt and freqs[0] <= tgt <= freqs[-1]:
                near = np.abs(freqs - tgt) <= max(20_000, rbw * 4)
                res["target"] = {"frequency_hz": int(tgt),
                                 "peak_over_floor_db": round(float(m["_peak"][near].max() - floor), 1),
                                 "mean_over_floor_db": round(float(m["_mean"][near].max() - floor), 1)}
        if "waterfall" in tasks:
            f = out / f"waterfall{tag}.png"
            an.plot_waterfall(img, times, freqs, f, f"{rec.path.name}{rng}  FFT {nfft} {p.get('fft_window', 'hann')}",
                              p.get("dynamic_range_db"), p.get("colormap", "viridis"), int(p.get("width", 1200)),
                              int(p.get("height", 800)), cap)
            arts.append({"kind": "waterfall", "path": str(f), "label": f"waterfall FFT {nfft}{rng}"})
        if "spectrum" in tasks:
            fig, ax = plt.subplots(figsize=(12, 4), dpi=100)
            ax.plot(freqs / 1e6, m["_mean"], lw=0.8, label="mean")
            ax.plot(freqs / 1e6, m["_peak"], lw=0.6, alpha=0.6, label="peak hold")
            ax.axhline(floor, ls="--", lw=0.7, color="gray", label=f"noise floor {floor:.1f} dBFS/bin")
            ax.set_xlabel("Frequency (MHz)")
            ax.set_ylabel("dBFS per bin (uncalibrated)")
            ax.grid(alpha=0.3)
            ax.legend()
            ax.set_title(f"{rec.path.name}{rng}  RBW {rbw:.0f} Hz", fontsize=9)
            an.add_caption(fig, cap)
            f = out / f"spectrum{tag}.png"
            fig.savefig(f)
            plt.close(fig)
            arts.append({"kind": "spectrum", "path": str(f), "label": f"spectrum FFT {nfft}{rng}"})
        if "signal_detection" in tasks or "rf_test" in tasks:
            res["signals"] = an.detect_signals(img, times, freqs, floor, float(p.get("threshold_db", 6.0)))
            res["signals_note"] = "candidates only; a signal near a satellite frequency is not identified as that satellite"
    if "frequency_offset" in tasks:
        around = p.get("target_frequency_hz") or rec.target
        if around is None and res.get("signals"):
            around = res["signals"][0]["frequency_hz"]
        if around is None:
            res["frequency_offset"] = {"measured": False, "note": "no target frequency and no detected signal to track"}
        else:
            tr = an.frequency_track(rec, float(around), float(p.get("search_hz", 30_000)), t0, t1)
            fig, ax = plt.subplots(figsize=(12, 3.5), dpi=100)
            ax.scatter(tr["_t"], (tr["_f"] - around) / 1e3, s=6, c=np.where(tr["_good"], "C0", "lightgray"))
            ax.set_xlabel("Time from recording start (s)")
            ax.set_ylabel(f"Offset from {around / 1e6:.4f} MHz (kHz)")
            ax.grid(alpha=0.3)
            ax.set_title("measured peak frequency (gray: below 10 dB SNR)", fontsize=9)
            an.add_caption(fig, cap)
            f = out / f"frequency_track{tag}.png"
            fig.savefig(f)
            plt.close(fig)
            arts.append({"kind": "diagnostics", "path": str(f), "label": "frequency offset / drift track"})
            res["frequency_offset"] = {k: v for k, v in tr.items() if not k.startswith("_")}
    if tasks & {"clipping", "signal_quality"}:
        full_band = fmin is None and fmax is None
        ov = an.overload(rec, t0, t1, img if full_band else None, freqs if full_band else None, floor)
        fig, ax = plt.subplots(figsize=(12, 3.5), dpi=100)
        ax.plot(ov["_t"], ov["_p"], lw=0.8)
        ax.set_xlabel("Time from recording start (s)")
        ax.set_ylabel("Block RMS (dBFS)")
        ax.grid(alpha=0.3)
        ax.set_title(f"power vs time; clipping {ov['clipped_fraction'] * 100:.4f}%", fontsize=9)
        an.add_caption(fig, cap)
        f = out / f"power{tag}.png"
        fig.savefig(f)
        plt.close(fig)
        arts.append({"kind": "diagnostics", "path": str(f), "label": "power vs time"})
        res["overload"] = {k: v for k, v in ov.items() if not k.startswith("_")}
    if "integrity" in tasks:
        res["integrity"] = an.integrity(rec)
    if "rf_test" in tasks and img is not None:
        from .testmetrics import rf_test_metrics
        data = rf_test_metrics(rec, img, times, freqs, floor, p, res)
        f = out / f"rf_test{tag}.json"
        import json
        f.write_text(json.dumps(data, separators=(",", ":")))
        res["rf_test"] = data["summary"]
        arts.append({"kind": "diagnostics", "path": str(f), "label": "signal-path test metrics and plot data"})
    return res, arts


def compare(inputs: list[dict[str, Any]], p: dict[str, Any], out: pathlib.Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if p.get("mode") == "satellite":
        from .satquality import sat_compare
        return sat_compare(inputs, p, out)
    return _compare(inputs, p, out)


def _compare(inputs: list[dict[str, Any]], p: dict[str, Any], out: pathlib.Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Side-by-side measurements of several recordings, each relative to its own noise floor."""
    plt = _plt()
    rows, warnings = [], []
    fig, ax = plt.subplots(figsize=(12, 4.5), dpi=100)
    for inp in inputs:
        rec = an.Recording(inp)
        nfft = int(p.get("fft_size", 8192))
        rbw = rec.rate / nfft
        img, _times, freqs = an.spectrogram(rec, nfft, int(p.get("rows", 200)), p.get("start_s"), p.get("end_s"))
        m = an.spectrum_metrics(img, freqs, rbw)
        ov = an.overload(rec, p.get("start_s"), p.get("end_s"), img, freqs, m["_floor"])
        tgt = p.get("target_frequency_hz") or rec.target
        row = {"recording_id": inp.get("recording_id"), "receiver": inp.get("receiver_id"), "file": rec.path.name,
               "start": inp.get("record_start"), "duration_s": round(rec.seconds, 1), "sample_rate_hz": rec.rate,
               "format": rec.fmt, "gain": inp.get("gain"),
               "noise_floor_dbfs_per_hz": round(m["_floor"] - 10 * np.log10(rbw), 2),
               "clipped_fraction": ov["clipped_fraction"], "rms_dbfs": ov["rms_dbfs"],
               "findings": [f for f in ov["findings"] if f["status"] in ("present", "suspected")]}
        if tgt and freqs[0] <= tgt <= freqs[-1]:
            near = np.abs(freqs - tgt) <= 20_000
            row["target_mean_over_floor_db"] = round(float(m["_mean"][near].max() - m["_floor"]), 1)
            row["target_peak_over_floor_db"] = round(float(m["_peak"][near].max() - m["_floor"]), 1)
        rows.append(row)
        ax.plot(freqs / 1e6, m["_mean"] - m["_floor"], lw=0.8, label=f"{inp.get('recording_id')} {inp.get('receiver_id')}")
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("dB over own noise floor")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    ax.set_title("mean spectra, each normalized to its own noise floor", fontsize=9)
    an.add_caption(fig, [" · ".join([an.caption_lines(i)[0], an.caption_lines(i)[1].split(" · ")[0]]) for i in inputs])
    f = out / "comparison_spectrum.png"
    fig.savefig(f)
    plt.close(fig)
    if len({r["start"] for r in rows}) > 1:
        overlap = _overlap(inputs)
        warnings.append("recordings were not made at the same time: pass geometry and interference differ"
                        if not overlap else "recordings overlap in time, but start at different moments")
    if len({r["sample_rate_hz"] for r in rows}) > 1:
        warnings.append("different sample rates: noise floors are compared per Hz, not per bin")
    if len({r["receiver"] for r in rows}) > 1:
        warnings.append("different receivers: their gains are not calibrated against each other, so absolute dBFS "
                        "levels are not comparable; compare dB over floor only")
    warnings.append("all figures are relative (dBFS / dB over floor), not calibrated received power")
    return {"comparison": rows, "warnings": warnings}, [{"kind": "diagnostics", "path": str(f), "label": "comparison spectrum"}]


def _overlap(inputs: list[dict[str, Any]]) -> bool:
    import datetime as dt
    spans = []
    for i in inputs:
        if i.get("record_start") and i.get("record_end"):
            spans.append((dt.datetime.fromisoformat(i["record_start"]), dt.datetime.fromisoformat(i["record_end"])))
    return len(spans) == len(inputs) and max(s for s, _ in spans) < min(e for _, e in spans)
