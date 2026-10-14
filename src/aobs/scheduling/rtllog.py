"""Parse rtl_sdr start-up output into tuner initialization diagnostics.

Observed on this host (librtlsdr 0.6.0-131-gaed0, the RTL-SDR Blog fork in /usr/local/lib,
which both /usr/bin/rtl_sdr and /usr/local/bin/rtl_sdr load): every R820T open prints

    Found Rafael Micro R820T tuner
    Enabled direct sampling mode, input 2
    Sampling at 2400000 S/s.
    [R82XX] PLL not locked!
    Disabled direct sampling mode
    Tuned to 137500000 Hz.

in healthy and failed recordings alike. The library briefly enables direct sampling while
nothing is tuned and the PLL warning comes from that state; the final tune clears both. So a
PLL warning *before* "Tuned to" is a transient start-up artifact. Only a warning after the
final tune, or a missing tune, is evidence of a tuning failure.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

_DEV = re.compile(r"^\s*(\d+):\s+(.*?),\s+(.*?),\s+SN:\s*(\S*)")
_ERRORS = ("Failed to open", "No matching devices", "No supported devices", "usb_claim_interface", "Failed to submit",
           "cb transfer status", "Lost at least", "WARNING: Failed", "Failed to set", "usb_open error", "Error")


def parse(text: str, expected_serial: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"version": 1, "devices": [], "selected_index": None, "selected_serial": None, "device_name": None,
                         "tuner": None, "requested": {}, "reported_sample_rate_hz": None, "tuned_frequency_hz": None,
                         "reported_gain_db": None, "gain_mode_reported": None, "direct_sampling_events": [],
                         "direct_sampling_final": None, "pll_warnings": 0, "pll_warnings_after_tune": 0,
                         "errors": [], "evidence": []}
    tuned = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("exec:"):
            d["requested"] = _requested(s[5:])
        elif (m := _DEV.match(line)) and not d["selected_index"]:
            d["devices"].append({"index": int(m[1]), "vendor": m[2], "product": m[3], "serial": m[4]})
        elif s.startswith("Using device"):
            m = re.match(r"Using device (\d+):\s*(.*)", s)
            if m:
                d["selected_index"], d["device_name"] = int(m[1]), m[2]
        elif s.startswith("Found ") and s.endswith(" tuner"):
            d["tuner"] = s[6:-6]
        elif s.startswith("Enabled direct sampling mode"):
            d["direct_sampling_events"].append(s)
            d["direct_sampling_final"] = "enabled" + (f" (input {m[1]})" if (m := re.search(r"input (\d)", s)) else "")
        elif s.startswith(("Disabled direct sampling mode", "Direct sampling mode disabled")):
            d["direct_sampling_events"].append(s)
            d["direct_sampling_final"] = "disabled"
        elif "PLL not locked" in s or "no valid PLL" in s:
            d["pll_warnings"] += 1
            if tuned:
                d["pll_warnings_after_tune"] += 1
        elif s.startswith("Sampling at"):
            m = re.search(r"(\d+)\s*S/s", s)
            d["reported_sample_rate_hz"] = int(m[1]) if m else None
        elif s.startswith("Tuned to"):
            m = re.search(r"(\d+)\s*Hz", s)
            d["tuned_frequency_hz"] = int(m[1]) if m else None
            tuned = True
        elif s.startswith("Tuner gain set to automatic"):
            d["gain_mode_reported"] = "auto"
        elif s.startswith("Tuner gain set to"):
            m = re.search(r"(-?[\d.]+)\s*dB", s)
            d["reported_gain_db"] = float(m[1]) if m else None
            d["gain_mode_reported"] = "manual"
        elif any(e in s for e in _ERRORS):
            d["errors"].append(s[:200])
    idx = d["selected_index"]
    sel = next((x for x in d["devices"] if x["index"] == idx), None)
    d["selected_serial"] = sel["serial"] if sel else None
    return _assess(d, expected_serial)


def _requested(cmd: str) -> dict[str, Any]:
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return {}
    out: dict[str, Any] = {"executable": argv[0] if argv else None}
    names = {"-d": "device", "-f": "frequency_hz", "-s": "sample_rate_hz", "-g": "gain_db", "-p": "ppm", "-D": "direct_sampling"}
    for i, a in enumerate(argv[:-1]):
        if a in names:
            v = argv[i + 1]
            try:
                out[names[a]] = float(v) if a == "-g" else (int(float(v)) if a in ("-f", "-s", "-p", "-D") else v)
            except ValueError:
                out[names[a]] = v
    return out


def _assess(d: dict[str, Any], expected_serial: str | None) -> dict[str, Any]:
    ev, req = d["evidence"], d["requested"]
    status = "ok"
    if expected_serial and d["selected_serial"] and d["selected_serial"] != expected_serial:
        ev.append(f"opened serial {d['selected_serial']}, expected {expected_serial}")
        status = "wrong_device"
    elif d["selected_index"] is None:
        status = "open_failed" if d["errors"] or d["devices"] or req else "no_log"
        ev.append("rtl_sdr never reported which device it opened" if status == "open_failed" else "no rtl_sdr output")
    elif d["tuned_frequency_hz"] is None:
        status = "tuning_failure_suspected"
        ev.append("rtl_sdr never reported a successful tune")
    elif d["pll_warnings_after_tune"]:
        status = "tuning_failure_suspected"
        ev.append(f"{d['pll_warnings_after_tune']} PLL-not-locked warning(s) after the final tune")
    elif d["pll_warnings"]:
        status = "ok_transient_pll_warning"
        ev.append(f"{d['pll_warnings']} PLL warning(s) during start-up only, before the final tune "
                  "(normal for this librtlsdr build; not a fault)")
    if req.get("frequency_hz") and d["tuned_frequency_hz"] and req["frequency_hz"] != d["tuned_frequency_hz"]:
        ev.append(f"tuned {d['tuned_frequency_hz']} Hz, requested {req['frequency_hz']} Hz")
    if req.get("sample_rate_hz") and d["reported_sample_rate_hz"] and req["sample_rate_hz"] != d["reported_sample_rate_hz"]:
        ev.append(f"sampling at {d['reported_sample_rate_hz']} S/s, requested {req['sample_rate_hz']}")
    if req.get("gain_db") not in (None, 0.0) and d["reported_gain_db"] is not None \
            and abs(req["gain_db"] - d["reported_gain_db"]) > 0.05:
        ev.append(f"tuner gain {d['reported_gain_db']:g} dB, requested {req['gain_db']:g} dB (nearest supported step)")
    if d["direct_sampling_final"] not in (None, "disabled") and not req.get("direct_sampling"):
        ev.append(f"direct sampling left {d['direct_sampling_final']} although it was not requested")
        if status in ("ok", "ok_transient_pll_warning"):
            status = "tuning_failure_suspected"
    d["status"] = status
    d["fault"] = status in ("wrong_device", "open_failed", "tuning_failure_suspected")
    return d


def persistent_tuning_failure(history: list[dict[str, Any]], n: int = 3) -> bool:
    """True only when the last `n` parsed logs of one receiver all suspect a tuning failure."""
    recent = [h for h in history if h and h.get("status") not in (None, "no_log")][-n:]
    return len(recent) == n and all(h["status"] == "tuning_failure_suspected" for h in recent)
