"""Identify: ask the vendor tools installed on a host what a discovered device is and what it can do.

Every step here opens a device (or, for SDRplay, asks the SDRplay API service for its device list),
so identification is an explicit action, never part of discovery, and inventory.service only runs it
when the device is visibly free and nothing is booked on it. Steps run through a host (hosts.LocalHost
or hosts.SshHost), so identification is the same on this computer and on remote hosts.

Only what a tool printed is reported, each value with the tool it came from. Nothing is filled in
from tables: if no installed tool reports a frequency range, there is no frequency range.

Tool notes that shape the steps:

  * rtl_test / rtl_eeprom take -d <index or serial>, but a serial made of digits (the common default
    "00000001") is read as an index, which could open a different device. So the device list is read
    first with a device string that matches nothing (each device's USB strings are read, none is
    claimed), the index of the one serial is taken from it, and the output is checked to show that index
    was used for that serial.
  * airspy_info and hackrf_info open every Airspy or HackRF on the host in turn, so the service runs
    them only when all of those are free.
  * uhd_find_devices is never run: on a B200-series USRP it can load firmware into the device.
"""

from __future__ import annotations

import re
from typing import Any, Callable

NO_MATCH = "starlab-list-only-no-such-device"
TIMEOUT_S = 30


# ------------------------------------------------------------------ parsers (mocked output in tests)
def parse_rtl_list(text: str) -> list[dict[str, Any]]:
    """'  0:  Realtek, RTL2838UHIDIR, SN: 00000001' lines (rtl_test, rtl_eeprom, rtl_sdr all print them)."""
    out = []
    for m in re.finditer(r"^\s*(\d+):\s+(.*?),\s+(.*?),\s+SN:\s*(.*?)\s*$", text, re.M):
        out.append({"index": int(m.group(1)), "manufacturer": m.group(2), "product": m.group(3),
                    "serial": m.group(4)})
    return out


def parse_rtl_test(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {"devices": parse_rtl_list(text)}
    m = re.search(r"^Using device (\d+):\s*(.*)$", text, re.M)
    if m:
        out["using"], out["device_name"] = int(m.group(1)), m.group(2).strip()
    m = re.search(r"^Found (.+?) tuner\s*$", text, re.M)
    if m:
        out["tuner"] = m.group(1).strip()
    m = re.search(r"^Supported gain values \((\d+)\):\s*(.*)$", text, re.M)
    if m:
        out["gains_db"] = [float(x) for x in m.group(2).split()]
    m = re.search(r"^(RTL-SDR Blog V\d.*?) Detected", text, re.M)
    if m:
        out["variant"] = m.group(1)
    return out


def parse_rtl_eeprom(text: str) -> dict[str, Any]:
    keys = {"Vendor ID": "vendor_id", "Product ID": "product_id", "Manufacturer": "manufacturer", "Product": "product",
            "Serial number": "serial", "Serial number enabled": "serial_enabled", "IR endpoint enabled": "ir_enabled",
            "Remote wakeup enabled": "remote_wakeup", "Bias Tee always on": "bias_tee_always_on"}
    out: dict[str, Any] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        if k.strip() in keys:
            out[keys[k.strip()]] = v.strip()
    return out


def parse_airspy_info(text: str) -> list[dict[str, Any]]:
    boards: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    rates = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Found AirSpy board"):
            cur = {"sample_rates_hz": []}
            boards.append(cur)
            rates = False
            continue
        if cur is None:
            continue
        if s.startswith("Board ID Number:"):
            cur["board_id"] = s.split(":", 1)[1].strip()
        elif s.startswith("Firmware Version:"):
            cur["firmware"] = s.split(":", 1)[1].strip()
        elif s.startswith("Part ID Number:"):
            cur["part_id"] = s.split(":", 1)[1].strip()
        elif s.startswith("Serial Number:"):
            cur["serial"] = s.split(":", 1)[1].strip().upper().removeprefix("0X")
        elif s.startswith("Supported sample rates"):
            rates = True
        elif rates and (m := re.match(r"^([\d.]+)\s*MSPS$", s)):
            cur["sample_rates_hz"].append(int(round(float(m.group(1)) * 1e6)))
        elif "failed" in s.lower():
            cur["error"] = s
    return boards


def parse_hackrf_info(text: str) -> list[dict[str, Any]]:
    boards: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    keys = {"Serial number": "serial", "Board ID Number": "board_id", "Firmware Version": "firmware",
            "Part ID Number": "part_id", "Hardware Revision": "hardware_revision",
            "Hardware supported by installed firmware": "firmware_supports"}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Index:"):
            cur = {"index": int(s.split(":", 1)[1].strip() or -1)}
            boards.append(cur)
            continue
        if cur is None or ":" not in s:
            continue
        k, v = s.split(":", 1)
        if k.strip() in keys:
            cur[keys[k.strip()]] = v.strip()
    for b in boards:
        if "serial" in b:
            b["serial"] = b["serial"].lower()
    return boards


def parse_soapy_find(text: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for line in text.splitlines():
        if line.startswith("Found device"):
            out.append({})
        elif out and (m := re.match(r"^\s+(\w+)\s*=\s*(.*?)\s*$", line)):
            out[-1][m.group(1)] = m.group(2)
    return out


def parse_iio_scan(text: str) -> list[dict[str, str]]:
    out = []
    for m in re.finditer(r"^\s*\d+:\s+([0-9a-f]{4}:[0-9a-f]{4})\s+\((.*?)\),\s+serial=(\S+)\s+\[(.*?)\]", text, re.M):
        out.append({"usb_id": m.group(1), "description": m.group(2), "serial": m.group(3), "uri": m.group(4)})
    return out


def parse_lime_find(text: str) -> list[dict[str, str]]:
    out = []
    for m in re.finditer(r"^\s*\*\s*\[(.*?)\]\s*$", text, re.M):
        parts = [p.strip() for p in m.group(1).split(",")]
        d = {"name": parts[0]}
        d.update(dict(p.split("=", 1) for p in parts[1:] if "=" in p))
        out.append(d)
    return out


# ------------------------------------------------------------------ identification
def family(dev: dict[str, Any]) -> str:
    """Which tool family identifies this device: rtlsdr | airspy | hackrf | sdrplay | plutosdr | lime | soapy."""
    if dev.get("driver") in ("rtlsdr", "airspy", "sdrplay"):
        return str(dev["driver"])
    return {"hackrf": "hackrf", "plutosdr": "plutosdr", "lime": "lime"}.get(dev.get("soapy_driver") or "", "soapy")


# Tools that open every device of their family on the host (the service checks all are free).
OPENS_ALL = {"airspy": "airspy_info", "hackrf": "hackrf_info"}


def _step(tool: str, cmd: list[str], r: Any, opens: str) -> dict[str, Any]:
    text = (r.stdout or "") + (r.stderr or "")
    return {"tool": tool, "command": " ".join(cmd), "exit": r.returncode, "opens": opens,
            "error": r.error if getattr(r, "error", None) else None, "output": text[-4000:]}


def identify(run: Callable[..., Any], dev: dict[str, Any], tools: dict[str, str] | Callable[[str], Any],
             probe: Callable[[str, str], dict[str, Any]] | None, skip: dict[str, str] | None = None) -> dict[str, Any]:
    """Run the identification steps for one discovered device on its host.

    run(argv, input=None, timeout=...) -> RunResult; dev = Discovered.as_dict(); tools = the host's installed tools;
    probe(soapy_driver, serial) = a SoapySDR probe on that host (or None); skip = {tool: reason} the service decided
    must not run (for example airspy_info while another Airspy is recording)."""
    has = tools if callable(tools) else (lambda t: tools.get(t))
    skip = dict(skip or {})
    fam, serial = family(dev), dev.get("serial") or ""
    steps: list[dict[str, Any]] = []
    fields: dict[str, dict[str, Any]] = {}          # name -> {value, source}
    problems: list[str] = []

    def put(name: str, value: Any, source: str) -> None:
        if value not in (None, "", [], {}):
            fields[name] = {"value": value, "source": source}

    def skipped(tool: str, why: str) -> None:
        steps.append({"tool": tool, "skipped": why})

    if fam == "rtlsdr":
        if not has("rtl_test"):
            skipped("rtl_test", "not installed on this host")
        else:
            cmd = ["rtl_test", "-d", NO_MATCH]
            r = run(cmd, timeout=TIMEOUT_S)
            steps.append(_step("rtl_test", cmd, r, "reads every RTL-SDR's USB strings; claims none"))
            listed = parse_rtl_list(r.stdout + r.stderr)
            idx = [d["index"] for d in listed if d["serial"] == serial]
            if r.error:
                problems.append(r.error)
            elif len(idx) != 1:
                problems.append(f"librtlsdr lists {len(idx)} devices with serial {serial!r}; not opening any")
            else:
                i = idx[0]
                cmd = ["rtl_test", "-d", str(i), "-t"]
                r = run(cmd, timeout=TIMEOUT_S)
                steps.append(_step("rtl_test", cmd, r, "opens this device (tuner check, no streaming)"))
                t = parse_rtl_test(r.stdout + r.stderr)
                again = [d for d in t["devices"] if d["index"] == i]
                if t.get("using") != i or not again or again[0]["serial"] != serial:
                    problems.append("rtl_test did not confirm it opened the device with this serial; its output is "
                                    "not used")
                else:
                    put("tuner", t.get("tuner"), "rtl_test")
                    put("variant", t.get("variant"), "rtl_test")
                    put("gain_values_db", t.get("gains_db"), "rtl_test")
                    if has("rtl_eeprom") and "rtl_eeprom" not in skip:
                        cmd = ["rtl_eeprom", "-d", str(i)]
                        r = run(cmd, timeout=TIMEOUT_S)
                        steps.append(_step("rtl_eeprom", cmd, r, "opens this device and reads its EEPROM (no write)"))
                        e = parse_rtl_eeprom(r.stdout + r.stderr)
                        if e.get("serial") == serial:
                            put("eeprom", e, "rtl_eeprom")
                            put("manufacturer", e.get("manufacturer"), "rtl_eeprom")
                            put("product", e.get("product"), "rtl_eeprom")
                        elif e:
                            problems.append(f"rtl_eeprom reported serial {e.get('serial')!r}, not {serial!r}; ignored")
                    elif not has("rtl_eeprom"):
                        skipped("rtl_eeprom", "not installed on this host")
        if has("rtl_biast"):
            put("bias_tee", "the installed driver has a bias-tee switch (rtl_biast); whether this receiver has a bias "
                "tee cannot be read from the device", "rtl_biast installed")
    elif fam in OPENS_ALL:
        tool = OPENS_ALL[fam]
        if not has(tool):
            skipped(tool, "not installed on this host")
        elif tool in skip:
            skipped(tool, skip[tool])
        else:
            cmd = [tool]
            r = run(cmd, timeout=TIMEOUT_S)
            steps.append(_step(tool, cmd, r, f"opens every {fam} device on the host in turn (no streaming)"))
            boards = (parse_airspy_info if fam == "airspy" else parse_hackrf_info)(r.stdout + r.stderr)
            want = serial.upper() if fam == "airspy" else serial.lower()
            mine = [b for b in boards if b.get("serial") == want or (fam == "hackrf" and want and
                                                                      b.get("serial", "").endswith(want))]
            if len(mine) == 1:
                b = mine[0]
                put("firmware", b.get("firmware"), tool)
                put("board", b.get("board_id"), tool)
                put("part_id", b.get("part_id"), tool)
                put("hardware_revision", b.get("hardware_revision"), tool)
                put("sample_rates_hz", b.get("sample_rates_hz"), tool)
                if b.get("error"):
                    problems.append(f"{tool}: {b['error']}")
            else:
                problems.append(f"{tool} did not report a board with serial {serial}")
    elif fam == "sdrplay":
        if not has("SoapySDRUtil"):
            skipped("SoapySDRUtil", "not installed on this host")
        else:
            cmd = ["SoapySDRUtil", "--find=driver=sdrplay"]
            r = run(cmd, timeout=TIMEOUT_S)
            steps.append(_step("SoapySDRUtil", cmd, r, "asks the SDRplay API service for its device list; selects no "
                               "device"))
            found = parse_soapy_find(r.stdout + r.stderr)
            mine = [d for d in found if d.get("serial") == serial] if serial else found
            if len(mine) == 1:
                put("api_label", mine[0].get("label"), "SDRplay API (SoapySDRPlay3)")
                m = re.search(r"\b(RSP\w+)\b", mine[0].get("label", ""))
                put("model", m.group(1) if m else None, "SDRplay API (SoapySDRPlay3)")
                put("serial", mine[0].get("serial"), "SDRplay API (SoapySDRPlay3)")
            else:
                problems.append(f"the SDRplay API lists {len(mine)} devices matching this one")
    elif fam == "plutosdr" and has("iio_info"):
        cmd = ["iio_info", "-s"]
        r = run(cmd, timeout=TIMEOUT_S)
        steps.append(_step("iio_info", cmd, r, "scans for IIO devices; streams nothing"))
        mine = [d for d in parse_iio_scan(r.stdout + r.stderr) if d["serial"] == serial]
        if mine:
            put("iio_uri", mine[0]["uri"], "iio_info")
            put("description", mine[0]["description"], "iio_info")
    elif fam == "lime" and has("LimeUtil"):
        cmd = ["LimeUtil", "--find"]
        r = run(cmd, timeout=TIMEOUT_S)
        steps.append(_step("LimeUtil", cmd, r, "lists Lime devices; streams nothing"))
        mine = [d for d in parse_lime_find(r.stdout + r.stderr) if d.get("serial", "").lower() == serial.lower()]
        if mine:
            put("model", mine[0].get("name"), "LimeUtil")
            put("connection", mine[0].get("media"), "LimeUtil")

    caps = None
    soapy = dev.get("soapy_driver") or ("rtlsdr" if fam == "rtlsdr" else None)
    if fam != "sdrplay" and soapy and probe is not None:
        if "SoapySDRUtil" in skip:
            skipped("SoapySDRUtil", skip["SoapySDRUtil"])
        elif not has("SoapySDRUtil"):
            skipped("SoapySDRUtil", "not installed on this host")
        else:
            p = probe(soapy, serial)
            steps.append({"tool": "SoapySDRUtil", "command": " ".join(p.get("command") or []),
                          "exit": 0 if p.get("ok") else 1, "opens": "opens this device (reads capabilities, no "
                          "streaming)", "error": p.get("error"), "output": (p.get("output") or "")[-4000:]})
            if p.get("capabilities"):
                caps = p["capabilities"]
                src = f"SoapySDR {soapy} module (--probe)"
                put("frequency_hz", caps.get("frequency_hz"), src)
                rates = caps.get("sample_rates_hz") or []
                put("sample_rates_hz", rates or caps.get("sample_rate_ranges_hz"), src)
                put("gain_controls", caps.get("gains"), src)
                put("antennas", caps.get("antennas"), src)
                if caps.get("bias_tee_setting"):
                    put("bias_tee", f"driver setting '{caps['bias_tee_setting']}'", src)
                ident = caps.get("identification") or {}
                for k in ("fw_version", "firmware", "fw"):
                    if ident.get(k):
                        put("firmware", ident[k], src)
            if p.get("problems"):
                problems += [f"SoapySDR: {x}" for x in p["problems"]]
            elif p.get("error"):
                problems.append(f"SoapySDR: {p['error']}")
    if not any("exit" in s for s in steps):
        problems.append("no identification tool for this device is installed on this host")
    return {"ok": bool(fields) and not any("did not confirm" in x for x in problems), "family": fam,
            "fields": fields, "steps": steps, "problems": problems, "capabilities": caps,
            "probe_ok": bool(caps) and not any(x.startswith("SoapySDR:") for x in problems)}
