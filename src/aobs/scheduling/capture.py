"""One recording, run as its own process: `python -m aobs.scheduling.capture <run.json>`.

The broker starts this in a new session, so a broker restart never interrupts a
recording; the outcome is written to <raw>.result.json for whoever is watching.
RTL-SDR captures run rtl_sdr with -d <serial>; SDRplay captures open SoapySDR by
serial. Settings are applied exactly as given; a setting the driver rejects
fails the capture instead of being skipped.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import Any

STOP = False
STALL_S = 5.0          # no samples for this long mid-recording: the receiver is gone (e.g. dropped off USB)


def _stop(*_: Any) -> None:
    global STOP
    STOP = True


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def write_json(path: pathlib.Path, data: dict[str, Any]) -> None:
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


RTL_WARMUP_S = 3.0      # rtl_sdr needs ~1.5 s to open, tune and start streaming


def _sleep_until(t: dt.datetime) -> None:
    while not STOP and utc_now() < t:
        time.sleep(min(0.1, max(0.0, (t - utc_now()).total_seconds())))


def capture_rtl(run: dict[str, Any], part: pathlib.Path, start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
    """rtl_sdr started early (so samples flow by `start`) with no sample limit (-n is a 32-bit byte
    count, ~15 min at 2.4 MS/s), and stopped at `end`. Coverage is measured from the first sample."""
    r = run["radio"]
    gain = "0" if r["gain_mode"] == "auto" else str(r["gain_db"])  # rtl_sdr: -g 0 means tuner AGC
    cmd = [run.get("rtl_sdr_path", "/usr/bin/rtl_sdr"), "-d", run["serial"], "-f", str(r["center_frequency_hz"]),
           "-s", str(r["sample_rate_hz"]), "-g", gain, "-p", str(r.get("ppm_correction", 0)), str(part)]
    _sleep_until(start - dt.timedelta(seconds=RTL_WARMUP_S))
    if STOP:
        raise RuntimeError("stopped before the recording started")
    print("exec:", " ".join(cmd), flush=True)
    proc = subprocess.Popen(cmd, stdout=sys.stdout, stderr=subprocess.STDOUT)
    first_sample = None
    ended_early = False
    while proc.poll() is None and not STOP and utc_now() < end:
        if first_sample is None and part.exists() and part.stat().st_size > 0:
            first_sample = utc_now()
        time.sleep(0.02)
    if proc.poll() is None:
        proc.terminate()                          # rtl_sdr flushes and closes the file on SIGTERM
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    else:
        ended_early = utc_now() < end
    stopped = min(utc_now(), end)
    size = part.stat().st_size if part.exists() else 0
    began = first_sample or stopped
    expected = int(r["sample_rate_hz"] * (stopped - began).total_seconds()) * 2
    late_s = max(0.0, (began - start).total_seconds())
    if STOP:
        status = "interrupted"
    elif ended_early or first_sample is None:
        status = "failed"
    else:
        status = "recorded" if size >= expected * 0.98 else "failed"
    error = None
    if status != "recorded":
        error = f"rtl_sdr {'exited early ' if ended_early else ''}(exit {proc.returncode}), {size}/{expected} bytes"
    return {"status": status, "return_code": proc.returncode, "bytes": size, "expected_bytes": expected,
            "actual_start": began.isoformat(), "first_sample_late_s": round(late_s, 2), "command": cmd,
            "warning": f"first sample {late_s:.1f} s after the scheduled start" if late_s > 0.5 else None,
            "error": error}


def capture_sdrplay(run: dict[str, Any], part: pathlib.Path, start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
    import numpy as np
    import SoapySDR
    from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_CS16, SOAPY_SDR_RX

    r = run["radio"]
    fmt = run["format"]
    sdr = SoapySDR.Device(f"driver=sdrplay,serial={run['serial']}")
    applied: dict[str, Any] = {}
    sdr.writeSetting("biasT_ctrl", "true" if r["bias_tee"] else "false")
    applied["biasT_ctrl"] = sdr.readSetting("biasT_ctrl")
    if applied["biasT_ctrl"] != ("true" if r["bias_tee"] else "false"):
        raise RuntimeError(f"bias tee readback {applied['biasT_ctrl']!r} does not match the request")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, r["sample_rate_hz"])
    sdr.setFrequency(SOAPY_SDR_RX, 0, r["center_frequency_hz"])
    if r.get("ppm_correction"):
        sdr.setFrequencyCorrection(SOAPY_SDR_RX, 0, float(r["ppm_correction"]))
    sdr.setBandwidth(SOAPY_SDR_RX, 0, r["bandwidth_hz"])
    sdr.setGainMode(SOAPY_SDR_RX, 0, bool(r["agc"]))
    ge = r.get("gain_elements") or {}
    if not r["agc"]:
        sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", ge["IFGR"])
    sdr.setGain(SOAPY_SDR_RX, 0, "RFGR", ge["RFGR"])
    if r.get("dc_offset_correction") is not None:          # None: driver default (automatic jobs)
        sdr.setDCOffsetMode(SOAPY_SDR_RX, 0, bool(r["dc_offset_correction"]))
    # SoapySDRPlay3 exposes IQ correction and the notch filters as driver settings, not Soapy modes.
    wanted = dict(r.get("device_settings") or {})
    if r.get("iq_correction") is not None:
        wanted["iqcorr_ctrl"] = bool(r["iq_correction"])
    for key, val in wanted.items():
        sdr.writeSetting(key, "true" if val else "false")
        got = sdr.readSetting(key)
        applied[key] = got
        if got != ("true" if val else "false"):
            raise RuntimeError(f"driver setting {key} reads back {got!r}, not {val}")
    applied.update(sample_rate=sdr.getSampleRate(SOAPY_SDR_RX, 0), frequency=sdr.getFrequency(SOAPY_SDR_RX, 0),
                   bandwidth=sdr.getBandwidth(SOAPY_SDR_RX, 0), agc=sdr.getGainMode(SOAPY_SDR_RX, 0),
                   IFGR=sdr.getGain(SOAPY_SDR_RX, 0, "IFGR"), RFGR=sdr.getGain(SOAPY_SDR_RX, 0, "RFGR"),
                   dc_offset_mode=sdr.getDCOffsetMode(SOAPY_SDR_RX, 0))
    print("applied:", json.dumps(applied), flush=True)
    if abs(applied["sample_rate"] - r["sample_rate_hz"]) > 1 or abs(applied["frequency"] - r["center_frequency_hz"]) > 1:
        raise RuntimeError(f"driver did not accept rate/frequency exactly: {applied}")
    soapy_fmt, buf = (SOAPY_SDR_CS16, np.empty(2 * 65536, np.int16)) if fmt == "cs16" else \
        (SOAPY_SDR_CF32, np.empty(65536, np.complex64))
    stream = sdr.setupStream(SOAPY_SDR_RX, soapy_fmt)
    samples = overflows = 0
    try:
        # Activate ahead of time (activation takes ~0.25 s) and discard samples until `start`.
        _sleep_until(start - dt.timedelta(seconds=1.5))
        sdr.activateStream(stream)
        while not STOP and utc_now() < start:
            sdr.readStream(stream, [buf], 65536, timeoutUs=200_000)
        started = utc_now()
        with part.open("xb") as out:
            last_data = time.monotonic()
            while not STOP and utc_now() < end:
                res = sdr.readStream(stream, [buf], 65536, timeoutUs=1_000_000)
                if res.ret > 0:
                    out.write(buf[: res.ret * (2 if fmt == "cs16" else 1)].tobytes())
                    samples += res.ret
                    last_data = time.monotonic()
                elif res.ret == -4:          # SOAPY_SDR_OVERFLOW: samples dropped, keep going but count it
                    overflows += 1
                    last_data = time.monotonic()
                elif res.ret not in (0, -1):    # 0 = nothing this read, -1 = timeout: keep reading
                    raise RuntimeError(f"SoapySDR stream error {res.ret}")
                elif time.monotonic() - last_data > STALL_S:
                    raise RuntimeError(f"no samples for {STALL_S:.0f} s: the receiver stopped streaming "
                                       "(look for a USB disconnect in the kernel log)")
    finally:
        try:
            sdr.deactivateStream(stream)
            sdr.closeStream(stream)
        except Exception:  # noqa: BLE001
            pass
    seconds = (end - started).total_seconds()
    expected = int(r["sample_rate_hz"] * seconds) * (4 if fmt == "cs16" else 8)
    size = part.stat().st_size
    status = "interrupted" if STOP else ("recorded" if size >= expected * 0.98 else "failed")
    return {"status": status, "bytes": size, "expected_bytes": expected, "samples": samples, "overflows": overflows,
            "actual_start": started.isoformat(), "applied_settings": applied,
            "error": None if status == "recorded" else f"{size}/{expected} bytes ({overflows} overflows)"}


def main(argv: list[str]) -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    run_path = pathlib.Path(argv[1])
    run = json.loads(run_path.read_text())
    raw = pathlib.Path(run["raw"])
    result_path = pathlib.Path(run["result"])
    part = raw.with_name(raw.name + ".part")
    start = dt.datetime.fromisoformat(run["record_start"])
    end = dt.datetime.fromisoformat(run["record_end"])
    result: dict[str, Any] = {"job_id": run["job_id"], "pid": os.getpid()}
    try:
        raw.parent.mkdir(parents=True, exist_ok=True)
        if raw.exists():
            raise RuntimeError(f"{raw} already exists; refusing to overwrite")
        if run.get("steps"):              # wideband scan / gain sweep: one open receiver, many steps
            from .stepcapture import capture as capture_steps
            result.update(capture_steps(run, part, start, end, lambda: STOP))
        else:
            fn = capture_rtl if run["driver"] == "rtlsdr" else capture_sdrplay
            result.update(fn(run, part, start, end))
        if result["status"] == "recorded":
            part.rename(raw)
            result["file"] = str(raw)
        else:
            result["file"] = str(part) if part.exists() else None
    except Exception as exc:  # noqa: BLE001 - the outcome must always be written
        result.update(status="failed", error=f"{type(exc).__name__}: {exc}",
                      bytes=part.stat().st_size if part.exists() else 0, file=str(part) if part.exists() else None)
    result["actual_end"] = utc_now().isoformat()
    write_json(result_path, result)
    print("result:", json.dumps({k: result.get(k) for k in ("status", "bytes", "expected_bytes", "error")}), flush=True)
    return 0 if result["status"] == "recorded" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
