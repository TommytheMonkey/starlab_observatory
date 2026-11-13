"""RTL-SDR IQ health: is a finished CU8 recording real receiver output, or a stuck dongle?

Background (OBS-R02, 2026-10-13): the capture process exited normally and wrote full-size
files, but the samples sat on two or three ADC codes around 127.5 (~0.5 LSB spread,
-45.1 dBFS) at both 8.7 dB and 38.6 dB of gain. A USB reconnect restored normal noise
(9.4 LSB at 8.7 dB, 17.8 LSB at 20.7 dB). rtl_sdr's own output was identical in both states,
so only the samples show the failure.

CU8 is unsigned 8-bit interleaved I, Q; the zero level is 127.5, so a sample's value is
(code - 127.5) / 128 and complex full scale is 0 dBFS.

A low level alone never marks a receiver defective. "suspicious_flat" needs a combination:
ADC codes barely moving (spread under 1 LSB) AND only a handful of distinct codes, with
repeated identical samples as supporting evidence, at a gain where a working R820T always
shows its own noise. A second recording at a different gain with the same level confirms
that the gain setting has no effect.

"repeating_buffer" (OBS-R07, 2026-11-12): an 11-minute ISS pass had normal level, spread and code counts, but its
bytes repeated exactly every 3,760 bytes (1.84 ms) from start to end: the dongle replayed one stale buffer instead of
fresh ADC data. Receiver noise never repeats 4 KB byte for byte, so one exact copy of a 4 KB snippet in the 8 MB after
it (capture.find_repeat) is proof. A dongle can also drop and come back stuck part-way through, so three points are
checked: a few MB in, the middle and near the end. Only a physical power cycle has cleared it. The recorder runs the
same check on the first 2 s of each RTL-SDR pass's stream (capture.pretest) and skips the pass when it repeats.

Classes, most severe first: capture_failure, repeating_buffer, suspicious_flat, possible_overload, weak_signal, healthy.
"""

from __future__ import annotations

import pathlib
from typing import Any

import numpy as np

from .capture import REPEAT_SNIPPET, REPEAT_WINDOW, find_repeat, repeat_offset

ZERO = 127.5
FLAT_STD_LSB = 1.0          # spread below this on both I and Q ...
FLAT_MAX_CODES = 8          # ... with at most this many distinct codes per component = a flat block
FLAT_BLOCK_FRACTION = 0.10  # this share of flat blocks makes the recording suspicious
FLAT_MIN_GAIN_DB = 5.0      # below this a quiet front end can be legitimately near-flat: report weak, not flat
WEAK_STD_LSB = 3.0          # under ~3 LSB the ADC is under-driven (weak RF or low gain); usable but poor
CLIP_FRACTION = 0.001       # >0.1 % of samples at code 0 or 255
HOT_STD_LSB = 45.0          # noise spread this wide means the ADC is near full scale
TRUNCATED = 0.90            # under 90 % of the expected bytes is a capture failure
NO_GAIN_RESPONSE_DB = 1.0   # two gains >= 6 dB apart whose levels differ by less than this

CLASSES = ("capture_failure", "repeating_buffer", "suspicious_flat", "possible_overload", "weak_signal", "healthy")
LABELS = {"healthy": "healthy", "weak_signal": "weak RF signal / under-driven ADC", "possible_overload": "possible overload",
          "suspicious_flat": "suspiciously flat IQ (possible receiver failure)", "capture_failure": "capture failure",
          "repeating_buffer": "repeating IQ buffer (dongle needs a power cycle)"}


BASIC_FULL_SCALE = {"cu8": 128.0, "cs8": 128.0, "cs16": 32768.0, "cf32": 1.0}
BASIC_LABELS = {"healthy": "basic check: normal", "weak_signal": "basic check: very low level",
                "possible_overload": "basic check: clipping", "no_data": "basic check: no varying samples"}


def basic(path: str | pathlib.Path, fmt: str, expected_bytes: int | None = None, n_blocks: int = 32,
          block_samples: int = 1 << 17) -> dict[str, Any]:
    """A simple, format-agnostic check for recordings the RTL-SDR CU8 check does not cover (SDRplay, Airspy,
    simulated, other formats): level, clipping, all-zero or frozen samples, DC offset. Informational only: it
    never fails a recording and never raises an alert. Reads evenly spaced blocks (a few MB), not the file."""
    p = pathlib.Path(path)
    out: dict[str, Any] = {"version": 1, "basic": True, "applicable": True, "file": str(p), "format": fmt,
                           "evidence": [], "issues": [], "alert": False}
    dtypes = {"cu8": (np.uint8, 2), "cs8": (np.int8, 2), "cs16": (np.int16, 4), "cf32": (np.float32, 8)}
    if fmt not in dtypes or not p.is_file():
        out.update(applicable=False, status=None, healthy=None,
                   note="no basic check for this format" if fmt in () or fmt not in dtypes else "the file is not here")
        return out
    dtype, bps = dtypes[fmt]
    size = p.stat().st_size
    total = size // bps
    out.update(bytes=size, samples=total)
    if expected_bytes and size < 0.98 * expected_bytes:
        out["issues"].append(f"short: {size / expected_bytes:.1%} of the expected bytes")
    fs = BASIC_FULL_SCALE[fmt]
    vals = []
    with p.open("rb") as fh:
        for start in _blocks(total, n_blocks, block_samples):
            fh.seek(start * bps)
            raw = np.frombuffer(fh.read(block_samples * bps), dtype=dtype).astype(np.float64)
            vals.append(raw - 127.5 if fmt == "cu8" else raw)
    x = np.concatenate(vals) if vals else np.zeros(0)
    if x.size < 2:
        return _basic_final(out, "no_data", "the file holds no samples")
    iq = x[0::2] + 1j * x[1::2]
    rms = float(np.sqrt(np.mean(np.abs(iq) ** 2)))
    out["rms_dbfs"] = round(20 * np.log10(max(rms, 1e-12) / fs), 2)
    out["dc_offset_db"] = round(20 * np.log10(max(abs(np.mean(iq)), 1e-12) / max(rms, 1e-12)), 1)
    out["clipped_fraction"] = round(float(np.mean(np.abs(x) >= fs * (0.999 if fmt == "cf32" else (fs - 1) / fs))), 6)
    out["zero_fraction"] = round(float(np.mean(x == 0)), 4)
    out["distinct_values"] = int(min(np.unique(x[: 1 << 16]).size, 1 << 16))
    out["evidence"].append(f"RMS {out['rms_dbfs']:.1f} dBFS, clipping {out['clipped_fraction']:.4%}, "
                           f"DC {out['dc_offset_db']:.1f} dB relative to RMS, {out['distinct_values']} distinct values")
    if out["zero_fraction"] > 0.99 or out["distinct_values"] <= 2:
        return _basic_final(out, "no_data", "the samples are all zero or frozen: no receiver data was recorded")
    if out["clipped_fraction"] > CLIP_FRACTION:
        return _basic_final(out, "possible_overload", f"{out['clipped_fraction']:.2%} of samples at full scale")
    if out["rms_dbfs"] < -70:
        return _basic_final(out, "weak_signal", "very low level; check the gain and the RF path")
    return _basic_final(out, "healthy", "samples vary normally; no clipping")


def _basic_final(o: dict[str, Any], status: str, summary: str) -> dict[str, Any]:
    o.update(status=status, label=BASIC_LABELS[status], summary=summary, healthy=status == "healthy")
    return o


def _blocks(size_samples: int, n_blocks: int, block: int) -> list[int]:
    """Evenly spaced block starts (in samples), skipping the first 0.25 s worth of USB start-up where possible."""
    if size_samples <= block:
        return [0] if size_samples else []
    lo = min(size_samples - block, 600_000)
    hi = size_samples - block
    if hi <= lo:
        lo = 0
    return sorted({int(x) for x in np.linspace(lo, hi, max(1, n_blocks))})


def assess(path: str | pathlib.Path, fmt: str = "cu8", sample_rate_hz: int | None = None,
           expected_bytes: int | None = None, gain_db: float | None = None, gain_mode: str | None = None,
           n_blocks: int = 32, block_samples: int = 1 << 17) -> dict[str, Any]:
    """Measure a recording from evenly spaced blocks (reads ~8 MB, not the file). Never raises for bad data."""
    p = pathlib.Path(path)
    out: dict[str, Any] = {"version": 1, "file": str(p), "format": fmt, "requested_gain_db": gain_db,
                           "gain_mode": gain_mode, "evidence": [], "issues": []}
    if fmt != "cu8":
        out.update(applicable=False, status=None, note="IQ health checks are calibrated for RTL-SDR CU8 only")
        return out
    out["applicable"] = True
    if not p.is_file():
        return _final(out, "capture_failure", "the recording file does not exist")
    size = p.stat().st_size
    samples = size // 2
    out.update(bytes=size, samples=samples, whole_samples=size % 2 == 0, expected_bytes=expected_bytes,
               duration_s=round(samples / sample_rate_hz, 3) if sample_rate_hz else None)
    if size % 2:
        out["issues"].append("file ends with half a sample (odd byte count)")
    if expected_bytes:
        out["fraction_of_expected"] = round(size / expected_bytes, 4)
    if samples == 0:
        return _final(out, "capture_failure", "the recording is empty")
    starts = _blocks(samples, n_blocks, block_samples)
    hist_i = np.zeros(256, np.int64)
    hist_q = np.zeros(256, np.int64)
    acc = np.zeros(4)                  # sum I, sum Q, sum (I-z)^2+(Q-z)^2, n
    repeats = pairs = 0
    longest = 0
    per_block = []
    zero_blocks = 0
    with p.open("rb") as fh:
        for s in starts:
            fh.seek(s * 2)
            raw = np.frombuffer(fh.read(block_samples * 2), np.uint8)
            raw = raw[: len(raw) // 2 * 2]
            if len(raw) < 4:
                continue
            i, q = raw[0::2], raw[1::2]
            hist_i += np.bincount(i, minlength=256)
            hist_q += np.bincount(q, minlength=256)
            fi, fq = i.astype(np.float64), q.astype(np.float64)
            acc += (fi.sum(), fq.sum(), ((fi - ZERO) ** 2 + (fq - ZERO) ** 2).sum(), len(i))
            iq = i.astype(np.uint16) << 8 | q
            same = iq[1:] == iq[:-1]
            repeats += int(same.sum())
            pairs += len(same)
            longest = max(longest, _longest_run(same) + 1)
            si, sq = float(fi.std()), float(fq.std())
            ui, uq = int(np.unique(i).size), int(np.unique(q).size)
            if not raw.any():
                zero_blocks += 1
            per_block.append({"offset_s": round(s / sample_rate_hz, 2) if sample_rate_hz else s,
                              "std_i": round(si, 2), "std_q": round(sq, 2), "codes_i": ui, "codes_q": uq,
                              "flat": max(si, sq) < FLAT_STD_LSB and max(ui, uq) <= FLAT_MAX_CODES})
    n = acc[3]
    if not n:
        return _final(out, "capture_failure", "no samples could be read")
    mi, mq = acc[0] / n, acc[1] / n
    vals = np.arange(256, dtype=np.float64)
    std_i = float(np.sqrt(max(0.0, (hist_i * (vals - mi) ** 2).sum() / n)))
    std_q = float(np.sqrt(max(0.0, (hist_q * (vals - mq) ** 2).sum() / n)))
    power = acc[2] / n / 128.0 ** 2
    clipped = (hist_i[0] + hist_i[255] + hist_q[0] + hist_q[255]) / (2 * n)
    flat_blocks = sum(b["flat"] for b in per_block)
    m = {
        "sampled_samples": int(n), "sampled_blocks": len(per_block),
        "rms_dbfs": round(10 * np.log10(max(power, 1e-12)), 2),
        "ac_rms_dbfs": round(10 * np.log10(max((std_i ** 2 + std_q ** 2) / 128.0 ** 2, 1e-12)), 2),
        "i_std_lsb": round(std_i, 2), "q_std_lsb": round(std_q, 2),
        "sample_spread_lsb": round(max(std_i, std_q), 2),
        "i_unique_codes": int((hist_i > 0).sum()), "q_unique_codes": int((hist_q > 0).sum()),
        "i_range": [int(np.flatnonzero(hist_i)[0]), int(np.flatnonzero(hist_i)[-1])],
        "q_range": [int(np.flatnonzero(hist_q)[0]), int(np.flatnonzero(hist_q)[-1])],
        "dc_offset_i_lsb": round(mi - ZERO, 2), "dc_offset_q_lsb": round(mq - ZERO, 2),
        "clipped_fraction": round(float(clipped), 6),
        "repeated_sample_fraction": round(repeats / max(1, pairs), 4),
        "longest_constant_run": int(longest),
        "flat_block_fraction": round(flat_blocks / len(per_block), 3),
        "zero_block_fraction": round(zero_blocks / len(per_block), 3),
        "block_std_lsb": {"min": min(max(b["std_i"], b["std_q"]) for b in per_block),
                          "median": float(np.median([max(b["std_i"], b["std_q"]) for b in per_block])),
                          "max": max(max(b["std_i"], b["std_q"]) for b in per_block)},
    }
    out.update(m)
    out["flat_blocks_at_s"] = [b["offset_s"] for b in per_block if b["flat"]][:20]
    out["repeat"] = _repeat(p, size, sample_rate_hz)
    return _classify(out)


def repeat_points(size: int) -> list[int]:
    """Where the 4 KB snippets are taken: a few MB in, the middle, and one search window before the end. A file
    shorter than three windows gets fewer (one, a quarter of the way in, for a few seconds of samples)."""
    span = REPEAT_SNIPPET + REPEAT_WINDOW
    first = repeat_offset(size)
    last = max(first, size - span) & ~1
    return sorted({first, ((first + last) // 2) & ~1, last})


def _repeat(p: pathlib.Path, size: int, sample_rate_hz: int | None) -> dict[str, Any]:
    """find_repeat at each of repeat_points (reads up to ~25 MB). The earliest repeating point and its period are
    reported; the onset is known only to the spacing of the points."""
    span = REPEAT_SNIPPET + REPEAT_WINDOW
    points = []
    with p.open("rb") as fh:
        for off in repeat_points(size):
            fh.seek(off)
            data = fh.read(span)
            points.append({"offset_bytes": off, "offset_s": round(off / 2 / sample_rate_hz, 2) if sample_rate_hz else None,
                           "searched_bytes": max(0, len(data) - REPEAT_SNIPPET), "period_bytes": find_repeat(data, 0)})
    hit = next((x for x in points if x["period_bytes"]), None)
    r: dict[str, Any] = {"points": points, "period_bytes": hit and hit["period_bytes"],
                         "first_offset_bytes": hit and hit["offset_bytes"], "first_offset_s": hit and hit["offset_s"]}
    if hit:
        r["period_samples"] = hit["period_bytes"] / 2
        r["period_ms"] = round(hit["period_bytes"] / 2 / sample_rate_hz * 1000, 3) if sample_rate_hz else None
    return r


def _longest_run(same: np.ndarray) -> int:
    if not same.any():
        return 0
    d = np.diff(np.concatenate(([0], same.astype(np.int8), [0])))
    return int((np.flatnonzero(d == -1) - np.flatnonzero(d == 1)).max())


def _classify(o: dict[str, Any]) -> dict[str, Any]:
    ev = o["evidence"]
    exp = o.get("expected_bytes")
    if exp and o["bytes"] < exp * TRUNCATED:
        return _final(o, "capture_failure", f"truncated: {o['bytes']:,} of {exp:,} expected bytes "
                                            f"({o['bytes'] / exp:.0%})")
    if o["zero_block_fraction"] >= 0.5:
        return _final(o, "capture_failure", f"{o['zero_block_fraction']:.0%} of sampled blocks are all zero bytes "
                                            "(no ADC data was written)")
    if exp and o["bytes"] < exp * 0.98:
        o["issues"].append(f"short: {o['bytes'] / exp:.1%} of the expected bytes")
    rp = o.get("repeat") or {}
    if rp.get("period_bytes"):
        ms = f" ({rp['period_ms']:g} ms)" if rp.get("period_ms") else ""
        at = f"{rp['first_offset_s']:g} s" if rp.get("first_offset_s") is not None else f"byte {rp['first_offset_bytes']:,}"
        ev.append(f"the {REPEAT_SNIPPET} bytes at offset {rp['first_offset_bytes']:,} ({at}) repeat exactly "
                  f"{rp['period_bytes']:,} bytes later{ms}: the samples are a replayed buffer, not receiver output")
        clean = [x for x in rp["points"] if x["offset_bytes"] < rp["first_offset_bytes"]]
        if clean:
            ev.append(f"clean at {', '.join(_where(x) for x in clean)}: the receiver started replaying part-way through")
        return _final(o, "repeating_buffer", f"IQ repeats every {rp['period_bytes']:,} bytes{ms} {repeat_where(rp)}")
    spread = o["sample_spread_lsb"]
    codes = max(o["i_unique_codes"], o["q_unique_codes"])
    flat_share = o["flat_block_fraction"]
    if flat_share >= FLAT_BLOCK_FRACTION:
        ev.append(f"{flat_share:.0%} of sampled blocks have under {FLAT_STD_LSB:g} LSB spread on I and Q "
                  f"and at most {FLAT_MAX_CODES} distinct ADC codes")
        ev.append(f"overall spread {spread:.2f} LSB over {o['i_unique_codes']} I / {o['q_unique_codes']} Q codes; "
                  f"RMS {o['rms_dbfs']:.2f} dBFS")
        if o["repeated_sample_fraction"] >= 0.05:
            ev.append(f"{o['repeated_sample_fraction']:.1%} of consecutive samples are identical "
                      f"(healthy receiver noise: well under 1%)")
        g = o.get("requested_gain_db")
        if o.get("gain_mode") != "auto" and g is not None and g < FLAT_MIN_GAIN_DB:
            ev.append(f"requested gain {g:g} dB is very low, so a quiet front end could look like this; "
                      "not treated as a receiver failure")
            return _final(o, "weak_signal", "ADC barely moving at very low gain")
        if flat_share < 0.9:
            ev.append(f"flat only in part of the recording (blocks at {o['flat_blocks_at_s'][:6]} s): "
                      "the receiver may have stalled mid-recording")
        return _final(o, "suspicious_flat", "IQ samples show abnormally low variation")
    if o["clipped_fraction"] > CLIP_FRACTION or spread > HOT_STD_LSB:
        ev.append(f"clipped {o['clipped_fraction']:.3%} of samples, spread {spread:.1f} LSB")
        return _final(o, "possible_overload", "ADC at or near full scale")
    if spread < WEAK_STD_LSB:
        ev.append(f"spread {spread:.2f} LSB over {codes} codes: the ADC is under-driven but its samples vary normally")
        return _final(o, "weak_signal", "low but varying level; raise the gain or check the RF path")
    ev.append(f"spread {spread:.1f} LSB over {codes} codes, RMS {o['rms_dbfs']:.1f} dBFS, clipping "
              f"{o['clipped_fraction']:.4%}")
    return _final(o, "healthy", "normal receiver noise and signal statistics")


def repeat_where(rp: dict[str, Any]) -> str:
    """Where a recording repeats, as far as its checked points show: "at every checked point (...)" or "by 323.5 s in
    (clean at 2.05 s)"."""
    pts = rp.get("points") or []
    clean = [x for x in pts if x["offset_bytes"] < (rp.get("first_offset_bytes") or 0)]
    if not clean:
        return f"at every checked point ({', '.join(_where(x) for x in pts)})"
    first = next(x for x in pts if x["offset_bytes"] == rp["first_offset_bytes"])
    return f"by {_where(first)} in (clean at {', '.join(_where(x) for x in clean)})"


def _where(x: dict[str, Any]) -> str:
    return f"{x['offset_s']:g} s" if x.get("offset_s") is not None else f"byte {x['offset_bytes']:,}"


def _final(o: dict[str, Any], status: str, summary: str) -> dict[str, Any]:
    o["status"] = status
    o["label"] = LABELS[status]
    o["summary"] = summary
    o["healthy"] = status == "healthy"
    o["alert"] = status in ("suspicious_flat", "capture_failure", "repeating_buffer")
    return o


def gain_response(current: dict[str, Any], others: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Compare against other recent recordings of the same receiver. Each item: {recording_id,
    requested_gain_db, rms_dbfs, status}. Returns evidence when two gains >= 6 dB apart give the same level."""
    g, lvl = current.get("requested_gain_db"), current.get("rms_dbfs")
    if g is None or lvl is None:
        return None
    for o in others:
        og, ol = o.get("requested_gain_db"), o.get("rms_dbfs")
        if og is None or ol is None or abs(og - g) < 6:
            continue
        same = abs(ol - lvl) < NO_GAIN_RESPONSE_DB
        return {"compared_with": o.get("recording_id"), "other_gain_db": og, "other_rms_dbfs": ol,
                "gain_db": g, "rms_dbfs": lvl, "responds_to_gain": not same,
                "note": (f"no gain response: {lvl:.2f} dBFS at {g:g} dB vs {ol:.2f} dBFS at {og:g} dB"
                         if same else f"level follows gain: {lvl:.2f} dBFS at {g:g} dB vs {ol:.2f} dBFS at {og:g} dB")}
    return None


RECOVERY = ("Do not reset or power-cycle the receiver during a scheduled pass. When it is idle: "
            "(1) record ~30 s at two gains at least 10 dB apart (e.g. 8.7 and 38.6 dB) with /radio-schedule; "
            "(2) if the level and spread do not change, physically reconnect its USB cable (OBS-R02 on 2026-10-13 "
            "recovered this way) and repeat (1); (3) if it is still flat, try another USB port or cable, then "
            "replace the dongle. A flat ADC is a receiver/USB fault: antenna or LNA problems lower the level "
            "but leave normal noise.")


POWER_CYCLE = ("Physically power-cycle the dongle when it is idle: unplug its USB cable (or switch off its powered hub "
               "port), wait ~10 s, plug it back in, then record a short test. A software USB reset or restarting "
               "rtl_sdr does not clear a replayed buffer. If it comes back, try another USB port or cable, then replace "
               "the dongle.")


def alert_text(receiver_id: str, serial: str, recording_id: str, h: dict[str, Any], tuner: dict[str, Any] | None = None,
               next_job: str | None = None) -> str:
    title = {"suspicious_flat": "POSSIBLE RECEIVER FAILURE", "repeating_buffer": "DONGLE REPLAYING ONE BUFFER"}.get(
        h["status"], "CAPTURE FAILURE")
    lines = [f":warning: *WARNING: {receiver_id} — {title}*"]
    if h["status"] == "repeating_buffer":
        rp = h["repeat"]
        ms = f" ({rp['period_ms']:g} ms)" if rp.get("period_ms") else ""
        lines.append(f"The recording completed, but its IQ samples repeat exactly every {rp['period_bytes']:,} bytes{ms} "
                     f"{repeat_where(rp)}: where it repeats it holds no real signal. The receiver needs a physical "
                     "power cycle.")
    elif h["status"] == "suspicious_flat":
        lines.append("The recording completed successfully, but its IQ samples exhibit abnormally low variation.")
    else:
        lines.append(f"The recording is not usable: {h['summary']}.")
    g = h.get("requested_gain_db")
    lines += [f"• Receiver serial: {serial}", f"• Recording: {recording_id}",
              f"• Requested gain: {'auto' if h.get('gain_mode') == 'auto' else (f'{g:g} dB' if g is not None else 'n/a')}"
              + (f" (tuner reported {tuner['reported_gain_db']:g} dB)" if tuner and tuner.get("reported_gain_db") is not None else "")]
    if h.get("rms_dbfs") is not None:
        lines += [f"• Measured RMS: {h['rms_dbfs']:.2f} dBFS", f"• ADC sample spread: {h['sample_spread_lsb']:.2f} LSB "
                  f"(I {h['i_std_lsb']:.2f}, Q {h['q_std_lsb']:.2f})",
                  f"• Unique sample values: I {h['i_unique_codes']}, Q {h['q_unique_codes']} of 256",
                  f"• Repeated consecutive samples: {h['repeated_sample_fraction']:.1%}"]
    gr = h.get("gain_response")
    if gr:
        lines.append(f"• Gain response: {gr['note']} ({gr['compared_with']})")
    for e in h.get("evidence", [])[:4]:
        lines.append(f"  – {e}")
    if next_job:
        lines.append(f"• Next job on this receiver: {next_job}")
    lines.append(f"Recommended: {POWER_CYCLE if h['status'] == 'repeating_buffer' else RECOVERY}")
    return "\n".join(lines)


def from_pretest(pre: dict[str, Any] | None) -> dict[str, Any] | None:
    """A pass's pre-pass check (capture.pretest) as an IQ health record, so a skipped pass shows as the receiver's IQ
    status and a clean check clears it. None when the check did not run or was inconclusive."""
    if not pre or not pre.get("conclusive"):
        return None
    status = "repeating_buffer" if pre.get("repeat") else "healthy"
    o: dict[str, Any] = {"version": 1, "applicable": True, "source": "pretest", "at": pre.get("at"),
                         "repeat": {"period_bytes": pre.get("period_bytes"), "period_ms": pre.get("period_ms"),
                                    "first_offset_bytes": pre.get("offset_bytes") if pre.get("repeat") else None,
                                    "first_offset_s": None}, "evidence": []}
    return _final(o, status, f"pre-pass check: repeats every {pre['period_bytes']:,} bytes ({pre.get('period_ms')} ms)"
                  if pre.get("repeat") else f"pre-pass check: no repeat in the first {pre.get('seconds', 2):g} s")


def pretest_alert_text(receiver_id: str, serial: str, job_id: str, job_name: str, pretest: dict[str, Any],
                       next_job: str | None = None) -> str:
    """A pass skipped because the first ~2 s of its stream repeated (capture.pretest)."""
    lines = [f":rotating_light: *{receiver_id} — PASS SKIPPED: DONGLE NEEDS A PHYSICAL POWER CYCLE*",
             f"The first {pretest.get('seconds', 2):g} s of samples for {job_name} ({job_id}) repeat exactly every "
             f"{pretest['period_bytes']:,} bytes ({pretest.get('period_ms')} ms): {serial} is replaying a stale buffer "
             "instead of streaming samples, so the pass was stopped and not recorded.",
             f"• Receiver serial: {serial}"]
    if pretest.get("evidence_file"):
        lines.append(f"• Samples kept: {pretest['evidence_file']}")
    if next_job:
        lines.append(f"• Next job on this receiver: {next_job} (it will be skipped too until the dongle is power-cycled)")
    lines.append("Further passes skipped for this reason are not alerted one by one: a reminder follows every few "
                 "hours while it lasts, and a message when it recovers.")
    lines.append(f"Required: {POWER_CYCLE}")
    return "\n".join(lines)


FAULT_NAMES = {"repeating_buffer": "replaying one USB buffer"}


def _ago(since: Any, now: Any) -> str:
    h = (now - since).total_seconds() / 3600
    return f"{h:.1f} h" if h < 48 else f"{h / 24:.1f} days"


def fault_reminder_text(receiver_id: str, serial: str, fault: str, since: Any, now: Any, affected: int,
                        job: dict[str, Any], next_job: str | None = None) -> str:
    lines = [f":rotating_light: *REMINDER: {receiver_id} is still {FAULT_NAMES.get(fault, fault)}*",
             f"First seen {since.isoformat(timespec='minutes')} ({_ago(since, now)} ago); {affected} pass(es) skipped or "
             f"unusable since, most recently {job['job_name']} ({job['job_id']}).",
             f"• Receiver serial: {serial}"]
    if next_job:
        lines.append(f"• Next job on this receiver: {next_job}")
    lines.append(f"Required: {POWER_CYCLE}")
    return "\n".join(lines)


def fault_recovered_text(receiver_id: str, fault: str, since: Any, affected: int, job: dict[str, Any],
                         source: str) -> str:
    return (f":white_check_mark: *{receiver_id} — RECOVERED: no longer {FAULT_NAMES.get(fault, fault)}*\n"
            f"The {source} of {job['job_name']} ({job['job_id']}) is clean. The fault was first seen "
            f"{since.isoformat(timespec='minutes')}; {affected} pass(es) were skipped or unusable while it lasted.")
