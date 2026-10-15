"""Ask the installed SoapySDR driver what a device can do (`SoapySDRUtil --probe`).

This opens the device, so it is never part of passive discovery: the caller checks first that
nothing is using the device and that Starlab has nothing booked on it (inventory.service).
The probe streams nothing and changes no setting; it reads the driver's capability lists.

Capabilities recorded here are exactly what the driver reported, with the time and the
command, so a later reader can tell measured facts from assumptions.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Callable

from .drivers import SOAPY_FORMATS

PROBE_TIMEOUT_S = 30
_NUM = r"-?\d+(?:\.\d+)?(?:e[-+]?\d+)?"


def _ranges(text: str, scale: float) -> tuple[list[float], list[list[float]]]:
    """'10, 2.5' or '[0.1, 10]' or '[0, 15, 1], 20' -> (discrete values, [[lo, hi(, step)]])."""
    values, ranges = [], []
    for m in re.finditer(rf"\[({_NUM}),\s*({_NUM})(?:,\s*({_NUM}))?\]|({_NUM})", text):
        if m.group(4) is not None:
            values.append(float(m.group(4)) * scale)
        else:
            r = [float(m.group(1)) * scale, float(m.group(2)) * scale]
            if m.group(3) is not None:
                r.append(float(m.group(3)) * scale)
            if r[0] == r[1]:
                values.append(r[0])
            else:
                ranges.append(r)
    return values, ranges


def parse_probe(text: str) -> dict[str, Any]:
    """Parse SoapySDRUtil --probe output for RX channel 0. Fields the driver did not print are absent."""
    out: dict[str, Any] = {"identification": {}, "settings": []}
    section = ""
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        h = re.match(r"^-- (.+)$", line)
        if h:
            section = h.group(1).strip()
            continue
        s = line.strip()
        if not s or s.startswith("---"):
            continue
        if section == "Device identification":
            if "=" in s:
                k, v = s.split("=", 1)
                out["identification"][k.strip()] = v.strip()
        elif section == "Peripheral summary":
            m = re.match(r"Channels:\s*(\d+)\s*Rx", s)
            if m:
                out["rx_channels"] = int(m.group(1))
            m = re.match(r"\*\s*(.+?)\s+-\s+(.*)$", s)
            if m and i + 1 < len(lines):
                meta = dict(re.findall(r"(\w+)=([^,\]]*)", lines[i + 1]))
                if "key" in meta:
                    out["settings"].append({"key": meta["key"], "name": m.group(1), "description": m.group(2),
                                            "type": meta.get("type"), "default": meta.get("default")})
        elif section == "RX Channel 0":
            if s.startswith("Supports AGC:"):
                out["agc"] = s.split(":", 1)[1].strip().upper() == "YES"
            elif s.startswith("Stream formats:"):
                out["stream_formats"] = [x.strip() for x in s.split(":", 1)[1].split(",") if x.strip()]
            elif s.startswith("Native format:"):
                out["native_format"] = s.split(":", 1)[1].strip().split()[0]
            elif s.startswith("Antennas:"):
                out["antennas"] = [x.strip() for x in s.split(":", 1)[1].split(",") if x.strip()]
            elif s.startswith("Full gain range:"):
                _, r = _ranges(s.split(":", 1)[1], 1)
                if r:
                    out["gain_range_db"] = r[0][:2]
            elif re.match(r"^\S+ gain range:", s):
                name = s.split(" gain range:")[0]
                vals, r = _ranges(s.split(":", 1)[1], 1)
                if r:
                    out.setdefault("gains", {})[name] = {"min": r[0][0], "max": r[0][1],
                                                         "step": r[0][2] if len(r[0]) > 2 else None}
                elif vals:
                    out.setdefault("gains", {})[name] = {"min": vals[0], "max": vals[0], "step": None}
            elif s.startswith("Full freq range:"):
                unit = 1e6 if "MHz" in s else 1e3 if "kHz" in s else 1e9 if "GHz" in s else 1
                vals, r = _ranges(s.split(":", 1)[1], unit)
                if r:
                    out["frequency_hz"] = [int(min(x[0] for x in r)), int(max(x[1] for x in r))]
            elif s.startswith("Sample rates:"):
                vals, r = _ranges(s.split(":", 1)[1], 1e6 if "MSps" in s else 1e3 if "kSps" in s else 1)
                out["sample_rates_hz"] = sorted(int(round(v)) for v in vals)
                out["sample_rate_ranges_hz"] = [[int(round(x[0])), int(round(x[1]))] for x in r]
            elif s.startswith("Filter bandwidths:"):
                vals, r = _ranges(s.split(":", 1)[1], 1e6 if "MHz" in s else 1e3 if "kHz" in s else 1)
                out["bandwidths_hz"] = sorted(int(round(v)) for v in vals)
                out["bandwidth_ranges_hz"] = [[int(round(x[0])), int(round(x[1]))] for x in r]
    return out


def capabilities_from_probe(p: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Capabilities Starlab can use from a parsed probe, and the problems that stop it recording."""
    problems = []
    if not p.get("rx_channels"):
        problems.append("the driver reports no receive channel")
    formats = [SOAPY_FORMATS[f] for f in p.get("stream_formats", []) if f in SOAPY_FORMATS]
    if not formats:
        problems.append(f"none of the driver's stream formats ({', '.join(p.get('stream_formats', [])) or 'none'}) "
                        "is one Starlab writes (CS16, CF32)")
    if not p.get("frequency_hz"):
        problems.append("the driver reported no frequency range")
    if not (p.get("sample_rates_hz") or p.get("sample_rate_ranges_hz")):
        problems.append("the driver reported no sample rates")
    if not p.get("gains"):
        problems.append("the driver reported no gain elements")
    bias = next((s["key"] for s in p.get("settings", []) if s.get("type") == "bool" and "bias" in
                 (s["key"] + " " + s["name"]).lower()), None)
    caps = {"frequency_hz": p.get("frequency_hz"), "sample_rates_hz": p.get("sample_rates_hz", []),
            "sample_rate_ranges_hz": p.get("sample_rate_ranges_hz", []), "formats": formats,
            "native_format": p.get("native_format"), "gains": p.get("gains", {}), "agc": bool(p.get("agc")),
            "bandwidths_hz": p.get("bandwidths_hz", []), "bandwidth_ranges_hz": p.get("bandwidth_ranges_hz", []),
            "bias_tee_setting": bias, "antennas": p.get("antennas", []),
            "hardware": p["identification"].get("hardware") or p["identification"].get("driver"),
            "identification": p["identification"]}
    return caps, problems


def run_probe(soapy_driver: str, serial: str, run: Callable[..., Any] = subprocess.run,
              timeout: float = PROBE_TIMEOUT_S) -> dict[str, Any]:
    """Probe one device by driver and serial. Returns {ok, capabilities, problems, error, command, output}."""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", serial) or not re.fullmatch(r"[a-z0-9_]{1,32}", soapy_driver):
        return {"ok": False, "error": "refusing to probe: unexpected characters in the driver or serial"}
    args = f"driver={soapy_driver},serial={serial}"
    cmd = ["SoapySDRUtil", f"--probe={args}"]
    try:
        r = run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "command": cmd, "error": f"the driver did not answer within {timeout:.0f} s"}
    except OSError as exc:
        return {"ok": False, "command": cmd, "error": f"could not run SoapySDRUtil: {exc}"}
    text = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or "-- RX Channel 0" not in text:
        tail = " ".join(text.strip().splitlines()[-3:])[-300:]
        return {"ok": False, "command": cmd, "error": f"the driver could not open the device (exit {r.returncode}): {tail}",
                "output": text[-4000:]}
    caps, problems = capabilities_from_probe(parse_probe(text))
    return {"ok": not problems, "command": cmd, "capabilities": caps, "problems": problems, "output": text[-8000:]}
