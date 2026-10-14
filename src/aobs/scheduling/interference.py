"""Interference Investigation: turn a signal test's measurements (testmetrics.rf_test_metrics output) into
anomalies and grounded follow-up suggestions.

Rules of evidence:
  * Every anomaly comes from a number in the test and says which (`evidence`).
  * `confidence` is about whether the anomaly is real as described, not about its cause.
  * `explanations` are possibilities, never conclusions; nothing identifies a transmitter.
  * Analog overload is only ever "possible": its indicators are indirect, and the absence of digital
    clipping does not rule it out.
  * A suggestion is only made when a measurement motivates it, and says which.
"""

from __future__ import annotations

from typing import Any

import numpy as np

PRESETS = {
    "general": "General interference survey",
    "strong_carrier": "Strong carrier investigation",
    "fm_overload": "FM broadcast overload",
    "satellite": "Satellite reception interference",
    "lna_overload": "LNA overload investigation",
    "antenna_noise": "Antenna noise comparison",
    "custom": "Custom investigation",
}
SEV = ("info", "low", "medium", "high")


def _a(kind: str, title: str, severity: str, confidence: str, evidence: str, explanations: list[str],
       freq: Any = None, f_range: Any = None, times: Any = None, power: Any = None, persistence: Any = None) -> dict[str, Any]:
    return {"kind": kind, "title": title, "severity": severity, "confidence": confidence, "evidence": evidence,
            "explanations": explanations, "frequency_hz": freq, "frequency_range_hz": f_range, "time_s": times,
            "relative_power_db": power, "persistence": persistence}


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    out, i = [], 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j + 1 < len(mask) and mask[j + 1]:
                j += 1
            out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def diagnose(m: dict[str, Any], preset: str = "general") -> dict[str, Any]:
    s, iq = m["summary"], m.get("iq") or {}
    rbw = float(s.get("rbw_hz") or 1000)
    ch = s.get("channel") or {}
    tgt, chbw = ch.get("target_frequency_hz"), ch.get("bandwidth_hz") or 25_000
    center = (m.get("recording") or {}).get("center_frequency_hz")
    rate = (m.get("recording") or {}).get("sample_rate_hz") or 1
    out: list[dict[str, Any]] = []

    # ---- narrowband carriers and persistent spurs
    sigs = m.get("signals") or []
    narrow = [x for x in sigs if x["bandwidth_hz"] <= 3 * rbw and not x.get("note")]
    for x in narrow:
        near = tgt is not None and abs(x["frequency_hz"] - tgt) <= 5 * chbw
        inside = tgt is not None and abs(x["frequency_hz"] - tgt) <= chbw / 2
        over = x["peak_over_floor_db"]
        sev = "high" if inside and over >= 15 else "medium" if near or over >= 30 else "low"
        conf = "high" if x["duty_cycle"] >= 0.9 and over >= 15 else "medium"
        kind = "persistent_spur" if x["duty_cycle"] >= 0.9 else "narrowband"
        out.append(_a(kind, ("Persistent narrow carrier" if kind == "persistent_spur" else "Narrowband signal")
                      + (" inside the target channel" if inside else " near the target" if near else ""),
                      sev, conf, f"{over} dB over the floor, {x['bandwidth_hz']} Hz wide, present "
                      f"{x['duty_cycle'] * 100:.0f}% of the time",
                      ["a local transmitter or one of its harmonics", "a spur generated inside the receiver",
                       "a clock or switching power supply harmonic nearby"],
                      freq=x["frequency_hz"], times=[x["start_s"], x["end_s"]], power=over, persistence=x["duty_cycle"]))
    freqs = sorted(x["frequency_hz"] for x in narrow if x["duty_cycle"] >= 0.9)
    if len(freqs) >= 4:
        d = np.diff(freqs)
        step = float(np.median(d))
        if step > 0 and np.mean(np.abs(d - step) <= max(2 * rbw, 0.02 * step)) >= 0.75:
            out.append(_a("spur_comb", f"Regular comb of {len(freqs)} carriers every {step / 1e3:.1f} kHz", "medium",
                          "medium", f"{len(freqs)} persistent narrow carriers with near-equal spacing",
                          ["harmonics of a clock or switching supply (in the receiver, a USB hub, or nearby equipment)"],
                          f_range=[freqs[0], freqs[-1]]))

    # ---- transients
    for x in sigs:
        if x["duty_cycle"] < 0.2 and not x.get("note"):
            out.append(_a("transient", "Transient signal", "low", "medium" if x["peak_over_floor_db"] >= 15 else "low",
                          f"{x['peak_over_floor_db']} dB over the floor for {x['duty_cycle'] * 100:.0f}% of the capture",
                          ["intermittent traffic (voice, data, telemetry)", "a burst of local interference"],
                          freq=x["frequency_hz"], times=[x["start_s"], x["end_s"]], power=x["peak_over_floor_db"],
                          persistence=x["duty_cycle"]))
    for ev in m.get("transients") or []:
        out.append(_a("transient_band", "Burst across the band", "medium" if ev["over_median_db"] >= 10 else "low",
                      "high", f"band power {ev['over_median_db']} dB above its median",
                      ["a strong nearby transmission starting and stopping", "impulsive noise (motors, switching, static)"],
                      times=[ev["start_s"], ev["end_s"]], power=ev["over_median_db"]))

    # ---- broadband elevation and frequency-dependent noise
    sp = m.get("spectrum") or {}
    if sp.get("floor_dbfs"):
        fl = np.array([v if v is not None else np.nan for v in sp["floor_dbfs"]], float)
        f = np.array(sp["freqs_hz"], float)
        med = float(np.nanmedian(fl))
        edge = max(1, len(fl) // 20)
        core = np.zeros(len(fl), bool)
        core[edge:-edge] = True                       # the outer 5% is the anti-alias roll-off
        for i, j in _runs((fl > med + 3) & core):
            if (j - i + 1) >= 0.05 * len(fl):
                ex = float(np.nanmax(fl[i:j + 1]) - med)
                out.append(_a("broadband", "Raised noise over a wide range", "medium" if ex >= 6 else "low", "medium",
                              f"noise floor up to {ex:.1f} dB above the band median over {(f[j] - f[i]) / 1e3:.0f} kHz",
                              ["a wide transmission in that range (digital/data)", "broadband noise from nearby electronics",
                               "the shape of a filter or amplifier in the path (compare a terminated input)"],
                              f_range=[int(f[i]), int(f[j])], power=round(ex, 1)))
        v = s.get("noise_floor_variation_db")
        if v is not None and v > 3:
            lo, hi = int(np.nanargmin(fl[core]) + edge), int(np.nanargmax(fl[core]) + edge)
            out.append(_a("frequency_dependent_noise", "Noise floor varies across the band", "low", "high",
                          f"interquartile spread {v} dB; lowest near {f[lo] / 1e6:.4f} MHz, highest near {f[hi] / 1e6:.4f} MHz",
                          ["filter or amplifier response across the band", "band activity raising parts of the floor"],
                          f_range=[int(f[0]), int(f[-1])], power=v))
    if (s.get("broadband_noise_rises") or 0) > 0:
        t = (m.get("time") or {})
        tt = np.array(t.get("t_s") or [], float)
        fl_t = np.array([x if x is not None else np.nan for x in (t.get("floor_dbfs") or [])], float)
        if tt.size and fl_t.size:
            med = float(np.nanmedian(fl_t))
            for i, j in _runs(fl_t > med + 3)[:10]:
                out.append(_a("broadband_burst", "Noise floor rose briefly", "low", "medium",
                              f"floor {float(np.nanmax(fl_t[i:j + 1]) - med):.1f} dB above its median",
                              ["impulsive or switching noise", "a wideband transmission"], times=[float(tt[i]), float(tt[j])]))

    # ---- asymmetry
    asym = s.get("spectral_asymmetry_db")
    if asym is not None and abs(asym) >= 3:
        out.append(_a("asymmetry", "Upper and lower half of the band differ", "low", "high",
                      f"{'upper' if asym > 0 else 'lower'} half has {abs(asym):.1f} dB more average power",
                      ["signals concentrated on one side", "I/Q imbalance images", "a filter edge inside the band"], power=asym))

    # ---- digital clipping
    clip = s.get("clipped_fraction") or 0
    if clip > 1e-5:
        out.append(_a("clipping", "Digital clipping", "high" if clip > 1e-3 else "medium" if clip > 1e-4 else "low", "high",
                      f"{clip * 100:.4f}% of samples at full scale",
                      ["receiver gain too high for the strongest signal present", "a very strong nearby transmitter"],
                      power=round(clip * 100, 5)))

    # ---- possible analog overload (indirect indicators only)
    ind = []
    if m.get("intermod_hypotheses"):
        ind.append(f"{len(m['intermod_hypotheses'])} peak(s) at 2·f1 − f2 of stronger peaks")
    for fnd in s.get("findings") or []:
        if fnd.get("finding") in ("intermodulation_or_spur_comb", "broadband_overload", "excessive_gain") and \
                fnd.get("status") in ("present", "suspected"):
            ind.append(f"{fnd['finding'].replace('_', ' ')}: {fnd['detail']}")
    pk = s.get("peak_sample_fraction_of_full_scale") or 0
    if pk >= 0.9:
        ind.append(f"peak sample at {pk * 100:.0f}% of full scale")
    strong = [x for x in sigs if x["peak_over_floor_db"] >= 40]
    if strong:
        ind.append(f"{len(strong)} signal(s) 40 dB or more above the floor")
    if ind:
        out.append(_a("analog_overload", "Possible analog overload", "medium", "medium" if len(ind) >= 2 else "low",
                      "; ".join(ind),
                      ["the LNA or tuner is compressed by a strong signal (in or outside this band)",
                       "these patterns can also come from real signals; overload is not proven"]))

    # ---- DC offset and I/Q imbalance
    dci, dcq = iq.get("dc_offset_i") or 0, iq.get("dc_offset_q") or 0
    dc_spike = next((f for f in s.get("findings") or [] if f.get("finding") == "dc_spike" and f.get("status") == "present"), None)
    if max(abs(dci), abs(dcq)) > 0.02 or dc_spike:
        out.append(_a("dc_offset", "DC offset / spike at the tuned center", "low", "high",
                      f"I mean {dci}, Q mean {dcq}" + (f"; {dc_spike['detail']}" if dc_spike else ""),
                      ["normal for zero-IF receivers; tests tune 1/8 of the sample rate away from the target to avoid it"],
                      freq=center))
    imb, cor = iq.get("iq_amplitude_imbalance_db"), iq.get("iq_correlation")
    if (imb is not None and abs(imb) > 0.5) or (cor is not None and abs(cor) > 0.05):
        out.append(_a("iq_imbalance", "I/Q imbalance", "medium" if (abs(imb or 0) > 1.5 or abs(cor or 0) > 0.15) else "low",
                      "high", f"amplitude imbalance {imb} dB, I/Q correlation {cor}",
                      ["receiver I/Q path mismatch; produces mirror images of strong signals across the center"]))

    out.sort(key=lambda a: (-SEV.index(a["severity"]), a["kind"]))
    return {"preset": preset, "preset_label": PRESETS.get(preset, preset), "anomalies": out,
            "no_overload_indicators": not ind, "suggestions": suggestions(out, m, preset),
            "notes": ["Absence of digital clipping does not rule out analog overload."] if not clip else []}


def suggestions(anoms: list[dict[str, Any]], m: dict[str, Any], preset: str) -> list[dict[str, Any]]:
    s = m["summary"]
    kinds = {a["kind"] for a in anoms}
    out = []

    def add(test: str, title: str, because: str) -> None:
        if not any(x["test"] == test for x in out):
            out.append({"test": test, "title": title, "because": because})
    if kinds & {"persistent_spur", "narrowband", "spur_comb", "broadband"} or preset == "antenna_noise":
        n = sum(1 for a in anoms if a["kind"] in ("persistent_spur", "narrowband", "spur_comb", "broadband"))
        add("termination", "Compare against a 50-Ω terminated input",
            f"{n} narrowband/broadband anomaly(ies) were measured; signals that remain with the antenna removed "
            "point inside the receiver or its cabling" if n else "this preset compares antenna noise with the receiver's own")
    if kinds & {"clipping", "analog_overload"} or preset == "lna_overload":
        why = next((a["evidence"] for a in anoms if a["kind"] in ("clipping", "analog_overload")), "this preset looks for overload")
        add("gain_sweep", "Run a gain sweep", f"{why}; a sweep shows where clipping starts and where target contrast is best")
    if preset == "fm_overload" or any(a["kind"] == "analog_overload" for a in anoms):
        add("scan", "Wideband scan 80–160 MHz",
            "strong signals outside this capture (e.g. FM broadcast) can overload the front end without appearing here")
    ch = s.get("channel") or {}
    if ch.get("measured") and ch.get("snr_db") is None:
        add("repeat", "Repeat when the target transmitter is active",
            f"the target channel held only {ch.get('power_over_noise_db')} dB over the noise, so there was nothing to measure")
    if not anoms:
        add("baseline", "Use this as a baseline", "no anomaly crossed the thresholds; compare a future change against it")
    return out


def compare_termination(a: dict[str, Any], b: dict[str, Any], tol_bins: float = 3) -> dict[str, Any]:
    """A = antenna connected, B = antenna removed (terminated or open). Which signals remain?"""
    rbw = float(a["summary"].get("rbw_hz") or 1000)
    remained, gone = [], []
    bsig = b.get("signals") or []
    for x in a.get("signals") or []:
        m = next((y for y in bsig if abs(y["frequency_hz"] - x["frequency_hz"]) <= tol_bins * rbw), None)
        row = {"frequency_hz": x["frequency_hz"], "connected_over_floor_db": x["peak_over_floor_db"],
               "removed_over_floor_db": m["peak_over_floor_db"] if m else None}
        (remained if m else gone).append(row)
    new = [{"frequency_hz": y["frequency_hz"], "removed_over_floor_db": y["peak_over_floor_db"]} for y in bsig
           if not any(abs(y["frequency_hz"] - x["frequency_hz"]) <= tol_bins * rbw for x in a.get("signals") or [])]
    fa, fb = a["summary"].get("noise_floor_dbfs_per_hz"), b["summary"].get("noise_floor_dbfs_per_hz")
    return {"remained": remained, "disappeared": gone, "only_without_antenna": new,
            "floor_change_db": round(fb - fa, 2) if fa is not None and fb is not None else None}
