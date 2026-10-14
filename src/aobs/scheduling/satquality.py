"""Satellite reception quality: is a recording carrying a usable satellite transmission?

Evidence is reported as a ladder; each rung needs its own proof and a failed higher rung never
denies a lower one:
  1. RF energy detected            channel power over the noise floor (our own measurement)
  2. Modulation potentially present the occupied width matches the signal's expected spectrum
  3. Synchronization achieved      the decoder's Viterbi reported SYNCED
  4. Valid frames decoded          CADU frames written / deframer SYNCED
  5. Image data recovered          image lines reported by the decoder, images with real content
Decoder figures come only from what the installed SatDump prints (demodulator SNR, Viterbi state
and BER, deframer state, per-channel image lines); anything else is "Not reported by decoder".
A failed decode never proves that no satellite signal was present.

Two-receiver comparisons use only relative measures (channel contrast over each recording's own
noise floor, frequency offset, occupied width, decoder outcomes): the receivers differ in ADC
format, gain, antenna and front end, so absolute dBFS values are not comparable.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import re
import subprocess
from typing import Any

import numpy as np

from .dsp import DTYPES, to_complex

NOT_REPORTED = "Not reported by decoder"

# Known transmissions. Frequencies come from the station configuration; flags say how sure we are.
SAT_PROFILES: dict[str, dict[str, Any]] = {
    "METEOR-M2-3": {"norad_id": 57166, "signal": "LRPT", "frequency_hz": 137_900_000, "pipeline": "meteor_m2-x_lrpt",
                    "symbol_rate": 72_000, "occupied_hz": [80_000, 200_000], "channel_bandwidth_hz": 150_000,
                    "note": "137.900 MHz per the station configuration"},
    "METEOR-M2-4": {"norad_id": 59051, "signal": "LRPT", "frequency_hz": 137_900_000, "pipeline": "meteor_m2-x_lrpt",
                    "symbol_rate": 72_000, "occupied_hz": [80_000, 200_000], "channel_bandwidth_hz": 150_000,
                    "note": "137.900 MHz per the station configuration, which marks it unverified; it has also "
                            "been reported on 137.100 MHz. Check the waterfall before trusting a failed decode."},
}
SIGNAL_TYPES = {"LRPT": "meteor_lrpt"}


def satdump_info(binary: str = "satdump", pipelines_dir: str = "/usr/share/satdump/pipelines") -> dict[str, Any]:
    """Installed SatDump version and pipelines, detected (not assumed)."""
    out: dict[str, Any] = {"binary": binary, "version": None, "pipelines": []}
    try:
        p = subprocess.run([binary, "version"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        m = re.search(r"SatDump v?([0-9][0-9A-Za-z.\-+]*)", (p.stdout or "") + (p.stderr or ""))
        out["version"] = m.group(1) if m else None
    except (OSError, subprocess.TimeoutExpired) as exc:
        out["error"] = str(exc)
    from .spec import satdump_pipeline_ids
    out["pipelines"] = sorted(satdump_pipeline_ids(pipelines_dir))
    return out


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def parse_satdump_log(text: str) -> dict[str, Any]:
    t = _ANSI.sub("", text or "")
    snr = [float(x) for x in re.findall(r"(?<!Peak )\bSNR\s*:\s*(-?[0-9.]+)\s*dB", t)]
    peak = [float(x) for x in re.findall(r"Peak SNR\s*:\s*(-?[0-9.]+)\s*dB", t)]
    vit = re.findall(r"Viterbi\s*:\s*(SYNCED|NOSYNC)\s*BER\s*:\s*([0-9.]+)", t)
    defr = re.findall(r"Deframer\s*:\s*(SYNCED|NOSYNC)", t)
    lines = {int(c): int(n) for c, n in re.findall(r"Channel\s+(\d+)\s+Lines\s*:\s*(\d+)", t)}
    ber = [float(b) for _, b in vit]
    return {
        "demod_snr_db_median": round(float(np.median(snr)), 2) if snr else None,
        "demod_snr_db_max": round(max(snr), 2) if snr else None,
        "peak_snr_db": round(max(peak), 2) if peak else None,
        "viterbi_reports": len(vit),
        "viterbi_sync_fraction": round(sum(1 for s, _ in vit if s == "SYNCED") / len(vit), 3) if vit else None,
        "viterbi_ber_min": round(min(ber), 4) if ber else None,
        "viterbi_ber_median": round(float(np.median(ber)), 4) if ber else None,
        "deframer_reports": len(defr),
        "deframer_sync_fraction": round(sum(1 for s in defr if s == "SYNCED") / len(defr), 3) if defr else None,
        "channel_lines": lines or None,
        "image_lines_max": max(lines.values()) if lines else None,
        "channels_with_lines": sorted(c for c, n in lines.items() if n > 0) if lines else None,
    }


def occupied_width(rf: dict[str, Any], target: int, search_hz: int = 150_000) -> dict[str, Any] | None:
    """-3 dB-over-floor width around the target in the stored mean spectrum, and its power-weighted center."""
    sp = rf.get("spectrum") or {}
    if not sp.get("freqs_hz"):
        return None
    f = np.array(sp["freqs_hz"], float)
    mean = np.array([v if v is not None else np.nan for v in sp["mean_dbfs"]], float)
    floor = float(sp.get("noise_floor_dbfs"))
    near = np.abs(f - target) <= search_hz
    if not near.any():
        return None
    above = near & (mean > floor + 3)
    if not above.any():
        return {"width_hz": 0, "center_hz": None}
    idx = np.nonzero(above)[0]
    k = int(idx[np.argmin(np.abs(f[idx] - target))])        # the run that contains / is nearest the target
    lo = hi = k
    while lo > 0 and above[lo - 1]:
        lo -= 1
    while hi < len(f) - 1 and above[hi + 1]:
        hi += 1
    lin = 10 ** (mean[lo:hi + 1] / 10)
    return {"width_hz": int(f[hi] - f[lo] + (f[1] - f[0])), "center_hz": int(np.sum(f[lo:hi + 1] * lin) / np.sum(lin)),
            "offset_hz": int(np.sum(f[lo:hi + 1] * lin) / np.sum(lin) - target)}


def ladder(profile: dict[str, Any], rf: dict[str, Any] | None, decodes: list[dict[str, Any]]) -> dict[str, Any]:
    levels = []

    def lvl(name: str, status: str, evidence: str) -> None:
        levels.append({"level": name, "status": status, "evidence": evidence})
    target = profile.get("frequency_hz")
    ch = ((rf or {}).get("summary") or {}).get("channel") or {}
    over = ch.get("power_over_noise_db")
    if rf is None or not ch.get("measured"):
        lvl("RF energy detected", "not tested", "no channel measurement for this recording")
        lvl("Modulation potentially present", "not tested", "needs the channel measurement")
    else:
        lvl("RF energy detected", "yes" if over >= 3 else "no",
            f"channel ({ch['bandwidth_hz'] / 1e3:.0f} kHz at {ch['target_frequency_hz'] / 1e6:.3f} MHz) holds {over:+.1f} dB "
            "relative to the noise floor's prediction" + (" (3 dB needed)" if over < 3 else ""))
        ow = occupied_width(rf, target or ch["target_frequency_hz"])
        lo, hi = profile.get("occupied_hz") or [0, 1e12]
        if ow and ow["width_hz"]:
            fits = lo <= ow["width_hz"] <= hi
            lvl("Modulation potentially present", "possible" if fits else "no",
                f"occupied width {ow['width_hz'] / 1e3:.0f} kHz centred {ow['offset_hz'] / 1e3:+.1f} kHz from the target "
                f"(expected {lo / 1e3:.0f}-{hi / 1e3:.0f} kHz for {profile.get('signal')}); spectrum shape alone cannot "
                "prove modulation")
        else:
            lvl("Modulation potentially present", "no", "nothing 3 dB above the floor near the target")
    best = max(decodes, key=lambda d: ((d.get("cadu_frames") or 0), (d.get("sat") or {}).get("viterbi_sync_fraction") or 0),
               default=None)
    if best is None:
        for name in ("Synchronization achieved", "Valid frames decoded", "Image data recovered"):
            lvl(name, "not tested", "no decode attempt yet")
    else:
        s = best.get("sat") or {}
        vf = s.get("viterbi_sync_fraction")
        lvl("Synchronization achieved", NOT_REPORTED if vf is None else ("yes" if vf > 0 else "no"),
            NOT_REPORTED if vf is None else f"Viterbi SYNCED in {vf * 100:.0f}% of {s['viterbi_reports']} reports"
            + (f", best BER {s['viterbi_ber_min']}" if s.get("viterbi_ber_min") is not None else ""))
        frames = best.get("cadu_frames") or 0
        lvl("Valid frames decoded", "yes" if frames > 0 else "no",
            f"{frames} CADU frames" + (f"; deframer SYNCED in {s['deframer_sync_fraction'] * 100:.0f}% of reports"
                                       if s.get("deframer_sync_fraction") is not None else ""))
        lines = s.get("image_lines_max")
        imgs = best.get("images") or 0
        lvl("Image data recovered", "yes" if (lines or 0) > 0 and imgs > 0 else "no",
            (f"{lines} image lines (channels {s.get('channels_with_lines')})" if lines is not None else NOT_REPORTED + " (lines)")
            + f"; {imgs} image(s) with real content")
    order = ["RF energy detected", "Modulation potentially present", "Synchronization achieved", "Valid frames decoded",
             "Image data recovered"]
    reached = [lv["level"] for lv in levels if lv["status"] in ("yes", "possible")]
    top = max(reached, key=order.index) if reached else None
    notes = []
    if best is not None and (best.get("cadu_frames") or 0) == 0 and any(lv["level"] == "RF energy detected" and lv["status"] == "yes" for lv in levels):
        notes.append("RF energy was present but the decoder never produced frames. A failed decode does not prove "
                     "that no satellite signal was there: frequency, Doppler, timing, level or IQ handling can all prevent lock.")
    return {"levels": levels, "highest": top, "notes": notes, "profile": profile}


# ==================================================================== two recordings of one pass
def _parse_t(v: Any) -> dt.datetime | None:
    if not v:
        return None
    t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _series(files: list[dict[str, Any]], target: int, chbw: float, t0: dt.datetime, t1: dt.datetime,
            step_s: float, block_s: float, nfft: int = 2048) -> dict[str, Any]:
    """Channel contrast (dB over this recording's own floor), offset and width over [t0, t1] on a fixed grid."""
    win = np.hanning(nfft)
    times = np.arange(0.0, (t1 - t0).total_seconds(), step_s)
    contrast = np.full(len(times), np.nan)
    offset = np.full(len(times), np.nan)
    mean_acc = None
    n_acc = 0
    for f in files:
        start = _parse_t(f.get("actual_start") or f.get("record_start"))
        rate, fmt, center = int(f["sample_rate_hz"]), f["format"], int(f["center_frequency_hz"])
        dtype, comps = DTYPES[fmt]
        bps = np.dtype(dtype).itemsize * comps
        total = pathlib.Path(f["path"]).stat().st_size // bps
        fr = center + (np.arange(nfft) - nfft // 2) * rate / nfft
        rbw = rate / nfft
        ch = np.abs(fr - target) <= chbw / 2
        ref = (np.abs(fr - target) > chbw) & (np.abs(fr - center) > 3 * rbw) & (np.abs(fr - center) < 0.45 * rate)
        if not ch.any() or not ref.any():
            continue
        n = int(block_s * rate)
        with open(f["path"], "rb") as fh:
            for i, tt in enumerate(times):
                at = (t0 + dt.timedelta(seconds=float(tt)) - start).total_seconds()
                a = int(at * rate)
                if a < 0 or a + n > total:
                    continue
                fh.seek(a * bps)
                x = to_complex(fh.read(n * bps), fmt)
                hop = nfft // 2
                k = (len(x) - nfft) // hop + 1
                if k < 1:
                    continue
                idx = np.arange(k)[:, None] * hop + np.arange(nfft)[None, :]
                p = (np.abs(np.fft.fftshift(np.fft.fft(x[idx] * win, axis=1), axes=1)) ** 2).mean(axis=0)
                floor = float(np.median(p[ref]))
                pc = float(p[ch].sum())
                contrast[i] = 10 * np.log10(pc / (floor * ch.sum()))
                if contrast[i] > 3:
                    offset[i] = float(np.sum(fr[ch] * p[ch]) / pc) - target
                pp = p / floor
                if mean_acc is None:
                    mean_acc, mean_f = np.zeros(nfft), fr
                if np.array_equal(fr, mean_f):
                    mean_acc += pp
                    n_acc += 1
    spec = None
    if mean_acc is not None and n_acc:
        rel = 10 * np.log10(mean_acc / n_acc)
        near = np.abs(mean_f - target) <= 3 * chbw
        spec = {"freqs_hz": [int(v) for v in mean_f[near]], "db_over_floor": [round(float(v), 2) for v in rel[near]]}
    good = contrast[np.isfinite(contrast)]
    return {"t_s": [round(float(v), 1) for v in times], "contrast_db": [None if not np.isfinite(v) else round(float(v), 2) for v in contrast],
            "offset_hz": [None if not np.isfinite(v) else round(float(v)) for v in offset],
            "contrast_median_db": round(float(np.median(good)), 2) if good.size else None,
            "contrast_p90_db": round(float(np.percentile(good, 90)), 2) if good.size else None,
            "seconds_above_3db": round(float(np.sum(good > 3) * step_s), 1) if good.size else 0.0,
            "spectrum_relative": spec}


def sat_compare(inputs: list[dict[str, Any]], p: dict[str, Any], out: pathlib.Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    by: dict[str, list[dict[str, Any]]] = {}
    for i in inputs:
        by.setdefault(i["recording_id"], []).append(i)
    ids = list(p.get("recordings") or by)
    if len(ids) != 2 or any(r not in by for r in ids):
        raise ValueError("satellite comparison needs exactly two recordings with raw IQ")
    target = int(p["target_frequency_hz"])
    chbw = float(p.get("channel_bandwidth_hz") or 150_000)
    spans = {}
    for r in ids:
        starts = [_parse_t(f.get("actual_start") or f.get("record_start")) for f in by[r]]
        ends = []
        for f, s in zip(by[r], starts):
            dtype, comps = DTYPES[f["format"]]
            ends.append(s + dt.timedelta(seconds=pathlib.Path(f["path"]).stat().st_size / (np.dtype(dtype).itemsize * comps)
                                          / int(f["sample_rate_hz"])))
        spans[r] = (min(starts), max(ends))
    t0, t1 = max(s for s, _ in spans.values()), min(e for _, e in spans.values())
    if t1 <= t0:
        raise ValueError("the two recordings do not overlap in time; they cannot be from the same pass")
    dur = (t1 - t0).total_seconds()
    step = max(2.0, dur / 300)
    series = {r: _series(by[r], target, chbw, t0, t1, step, float(p.get("block_s") or 0.5)) for r in ids}
    meta = {r: {"receiver_id": by[r][0]["receiver_id"], "format": by[r][0]["format"], "sample_rate_hz": by[r][0]["sample_rate_hz"],
                "gain": by[r][0].get("gain"), "files": len(by[r]), "start": spans[r][0].isoformat(), "end": spans[r][1].isoformat()}
            for r in ids}
    data = {"version": 1, "recordings": ids, "meta": meta, "overlap": {"start": t0.isoformat(), "end": t1.isoformat(),
            "seconds": round(dur, 1)}, "target_frequency_hz": target, "channel_bandwidth_hz": chbw, "step_s": step,
            "series": series, "method": __doc__.split("\n\n")[-1].strip()}
    (out / "sat_compare.json").write_text(json.dumps(data, separators=(",", ":")))
    summary = {r: {k: series[r][k] for k in ("contrast_median_db", "contrast_p90_db", "seconds_above_3db")} for r in ids}
    return {"overlap_s": round(dur, 1), "per_recording": summary}, [
        {"kind": "diagnostics", "path": str(out / "sat_compare.json"), "label": "satellite pass comparison"}]
