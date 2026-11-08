"""One recording, run as its own process: `python -m aobs.scheduling.capture <run.json>`.

The broker starts this in a new session, so a broker restart never interrupts a
recording; the outcome is written to <raw>.result.json for whoever is watching.
RTL-SDR captures run rtl_sdr with -d <serial>; SDRplay and other SoapySDR receivers
(Airspy, ...) open SoapySDR by driver and serial. Settings are applied exactly as given
and read back; a setting the driver rejects fails the capture instead of being skipped.
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


BIAS_OFF_TRIES = 5      # rtl_sdr may need a moment to let go of the device before the bias tee can be switched off


def _bias_off(serial: str) -> dict[str, Any]:
    from .stepcapture import set_rtl_bias_tee
    err = ""
    for i in range(BIAS_OFF_TRIES):
        try:
            return {**set_rtl_bias_tee(serial, False), "tries": i + 1}
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            time.sleep(1.0)
    return {"on": None, "error": err}


def capture_rtl(run: dict[str, Any], part: pathlib.Path, start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
    """rtl_sdr has no per-serial bias tee option, so when Starlab controls the bias tee (run["bias_tee_control"]) it
    is switched through librtlsdr before rtl_sdr opens the device (the setting outlasts the close), and switched off
    again after rtl_sdr exits, however the recording ended. Off is also set explicitly when the job wants it off, so
    a tee left on by a killed recorder does not carry over."""
    if not run.get("bias_tee_control"):
        return _record_rtl(run, part, start, end)
    from .stepcapture import set_rtl_bias_tee
    want = bool(run["radio"].get("bias_tee"))
    bias: dict[str, Any] = {"requested": want, "applied": None,
                            "method": "librtlsdr rtlsdr_set_bias_tee by serial, before rtl_sdr opens the device"}
    warn = []
    try:
        bias["set"] = set_rtl_bias_tee(run["serial"], want)
        bias["applied"] = want
        print(f"bias tee {'ON' if want else 'off'} (librtlsdr, {run['serial']})", flush=True)
    except Exception as exc:  # noqa: BLE001 - record anyway; the result says the tee could not be switched
        bias["error"] = str(exc)
        warn.append(f"could not switch the bias tee {'on' if want else 'off'}: {exc}")
        print("bias tee:", exc, flush=True)
    result: dict[str, Any] | None = None
    try:
        result = _record_rtl(run, part, start, end)
    finally:
        if want or bias.get("error"):
            bias["after"] = _bias_off(run["serial"])
            print("bias tee off after the recording:", bias["after"].get("error") or "ok", flush=True)
            if bias["after"].get("error"):
                warn.append(f"the bias tee could not be switched off after the recording ({bias['after']['error']}); "
                            "it may still be on")
        if result is not None:
            result["bias_tee"] = bias
            if warn:
                result["warning"] = "; ".join([w for w in [result.get("warning")] if w] + warn)
    return result


def _record_rtl(run: dict[str, Any], part: pathlib.Path, start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
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
    import SoapySDR

    r = run["radio"]
    sdr = SoapySDR.Device(f"driver=sdrplay,serial={run['serial']}")
    applied: dict[str, Any] = {}
    sdr.writeSetting("biasT_ctrl", "true" if r["bias_tee"] else "false")
    applied["biasT_ctrl"] = sdr.readSetting("biasT_ctrl")
    if applied["biasT_ctrl"] != ("true" if r["bias_tee"] else "false"):
        raise RuntimeError(f"bias tee readback {applied['biasT_ctrl']!r} does not match the request")
    try:
        out = _record_sdrplay(sdr, run, part, start, end, applied)
    except BaseException:
        if run.get("bias_tee_control") and r["bias_tee"]:
            _soapy_bias_off(sdr, "biasT_ctrl")
        raise
    out["bias_tee"] = {"requested": bool(r["bias_tee"]), "applied": applied["biasT_ctrl"] == "true",
                       "setting": "biasT_ctrl", "readback": applied["biasT_ctrl"],
                       "method": "SoapySDRPlay3 writeSetting('biasT_ctrl') + readSetting",
                       **({"after": applied["biasT_ctrl_after"]} if "biasT_ctrl_after" in applied else {})}
    return out


def _record_sdrplay(sdr: Any, run: dict[str, Any], part: pathlib.Path, start: dt.datetime, end: dt.datetime,
                    applied: dict[str, Any]) -> dict[str, Any]:
    import numpy as np
    from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_CS16, SOAPY_SDR_RX

    r = run["radio"]
    fmt = run["format"]
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
        if run.get("bias_tee_control") and r["bias_tee"]:
            try:                                  # Starlab switched it on for this recording: off again
                sdr.writeSetting("biasT_ctrl", "false")
                applied["biasT_ctrl_after"] = sdr.readSetting("biasT_ctrl")
            except Exception as exc:  # noqa: BLE001
                applied["biasT_ctrl_after"] = f"could not switch off: {exc}"
    seconds = (end - started).total_seconds()
    expected = int(r["sample_rate_hz"] * seconds) * (4 if fmt == "cs16" else 8)
    size = part.stat().st_size
    status = "interrupted" if STOP else ("recorded" if size >= expected * 0.98 else "failed")
    return {"status": status, "bytes": size, "expected_bytes": expected, "samples": samples, "overflows": overflows,
            "actual_start": started.isoformat(), "applied_settings": applied,
            "error": None if status == "recorded" else f"{size}/{expected} bytes ({overflows} overflows)"}


def capture_soapy(run: dict[str, Any], part: pathlib.Path, start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
    """Generic SoapySDR receiver (Airspy, HackRF, ...): named gain elements, read back after setting. When Starlab
    switches the bias tee (run["bias_tee_control"] with the driver's setting, e.g. SoapyAirspy "biastee"), it is set
    and read back before anything else, and switched off again however the recording ended."""
    import SoapySDR

    r = run["radio"]
    sdr = SoapySDR.Device(f"driver={run['soapy_driver']},serial={run['serial']}")
    key = run.get("bias_tee_setting") if run.get("bias_tee_control") else None
    bias = _soapy_bias_on(sdr, key, bool(r.get("bias_tee"))) if key else None
    try:
        return _record_soapy(sdr, run, part, start, end, bias)
    finally:
        if key and r.get("bias_tee"):
            bias["after"] = _soapy_bias_off(sdr, key)
            print("bias tee off after the recording:", bias["after"], flush=True)


def _soapy_bias_on(sdr: Any, key: str, want: bool) -> dict[str, Any]:
    """Set a SoapySDR bias tee setting (SoapyAirspy "biastee" -> airspy_set_rf_bias) to the job's value and read it
    back; off is written explicitly too, so a tee left on by a killed recorder does not carry over."""
    sdr.writeSetting(key, "true" if want else "false")
    back = sdr.readSetting(key)
    print(f"bias tee {'ON' if want else 'off'} ({key} reads back {back!r})", flush=True)
    if back != ("true" if want else "false"):
        if want:
            _soapy_bias_off(sdr, key)
        raise RuntimeError(f"bias tee setting {key} reads back {back!r}, not {want}")
    return {"requested": want, "applied": want, "setting": key, "readback": back,
            "method": f"SoapySDR writeSetting('{key}') + readSetting"}


def _soapy_bias_off(sdr: Any, key: str) -> str:
    try:
        sdr.writeSetting(key, "false")
        return sdr.readSetting(key)
    except Exception as exc:  # noqa: BLE001
        return f"could not switch off: {exc}"


def _record_soapy(sdr: Any, run: dict[str, Any], part: pathlib.Path, start: dt.datetime, end: dt.datetime,
                  bias: dict[str, Any] | None) -> dict[str, Any]:
    import numpy as np
    from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_CS16, SOAPY_SDR_RX

    r, fmt = run["radio"], run["format"]
    hw = dict(sdr.getHardwareInfo())
    sdr.setSampleRate(SOAPY_SDR_RX, 0, r["sample_rate_hz"])
    sdr.setFrequency(SOAPY_SDR_RX, 0, r["center_frequency_hz"])
    if r.get("bandwidth_hz"):
        sdr.setBandwidth(SOAPY_SDR_RX, 0, r["bandwidth_hz"])
    sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    applied: dict[str, Any] = {"hardware_info": hw}
    if bias:
        applied[bias["setting"]] = bias["readback"]
    for name, value in (r.get("gain_elements") or {}).items():
        sdr.setGain(SOAPY_SDR_RX, 0, name, float(value))
        applied[name] = sdr.getGain(SOAPY_SDR_RX, 0, name)
        if abs(applied[name] - float(value)) > 0.51:
            raise RuntimeError(f"gain element {name} reads back {applied[name]}, not {value}")
    applied.update(sample_rate=sdr.getSampleRate(SOAPY_SDR_RX, 0), frequency=sdr.getFrequency(SOAPY_SDR_RX, 0),
                   bandwidth=sdr.getBandwidth(SOAPY_SDR_RX, 0), agc=sdr.getGainMode(SOAPY_SDR_RX, 0))
    print("applied:", json.dumps(applied, default=str), flush=True)
    if abs(applied["sample_rate"] - r["sample_rate_hz"]) > 1:
        raise RuntimeError(f"driver did not accept the sample rate exactly: {applied['sample_rate']}")
    if abs(applied["frequency"] - r["center_frequency_hz"]) > 1000:
        raise RuntimeError(f"driver tuned {applied['frequency']} Hz, not {r['center_frequency_hz']} Hz")
    soapy_fmt, buf, per = (SOAPY_SDR_CS16, np.empty(2 * 65536, np.int16), 2) if fmt == "cs16" else \
        (SOAPY_SDR_CF32, np.empty(65536, np.complex64), 1)
    stream = sdr.setupStream(SOAPY_SDR_RX, soapy_fmt)
    samples = overflows = 0
    try:
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
                    out.write(buf[: res.ret * per].tobytes())
                    samples += res.ret
                    last_data = time.monotonic()
                elif res.ret == -4:
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
    expected = int(r["sample_rate_hz"] * (end - started).total_seconds()) * (4 if fmt == "cs16" else 8)
    size = part.stat().st_size
    status = "interrupted" if STOP else ("recorded" if size >= expected * 0.98 else "failed")
    out = {"status": status, "bytes": size, "expected_bytes": expected, "samples": samples, "overflows": overflows,
           "actual_start": started.isoformat(), "applied_settings": applied,
           "error": None if status == "recorded" else f"{size}/{expected} bytes ({overflows} overflows)"}
    if bias:
        out["bias_tee"] = bias            # the dict is shared: capture_soapy adds "after" once the tee is off again
    return out


def capture_sim(run: dict[str, Any], part: pathlib.Path, start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
    """Simulation mode: synthetic CS16 noise plus a weak carrier at the target, written in real time.
    Opens no device. Every result is marked simulated, so nothing downstream can take it for a recording."""
    import numpy as np

    r = run["radio"]
    rate = int(r["sample_rate_hz"])
    gain = float((r.get("gain_elements") or {}).get("GAIN", 25))
    noise = 40.0 * 10 ** (gain / 40)                     # LSB; rises with the simulated gain
    offset = float((r.get("target_frequency_hz") or r["center_frequency_hz"]) - r["center_frequency_hz"])
    tone = noise * 0.08
    rng = np.random.default_rng()
    _sleep_until(start)
    if STOP:
        raise RuntimeError("stopped before the recording started")
    started = utc_now()
    written = 0
    with part.open("xb") as out:
        while not STOP:
            now = min(utc_now(), end)
            due = int((now - started).total_seconds() * rate) - written
            if due <= 0:
                if now >= end:
                    break
                time.sleep(0.05)
                continue
            n = min(due, 262_144)
            t = (np.arange(written, written + n)) / rate
            x = (rng.normal(0, noise, n) + 1j * rng.normal(0, noise, n) + tone * np.exp(2j * np.pi * offset * t))
            iq = np.empty(2 * n, np.int16)
            iq[0::2] = np.clip(x.real, -32767, 32767)
            iq[1::2] = np.clip(x.imag, -32767, 32767)
            out.write(iq.tobytes())
            written += n
    expected = int(rate * (end - started).total_seconds()) * 4
    size = part.stat().st_size
    status = "interrupted" if STOP else ("recorded" if size >= expected * 0.98 else "failed")
    return {"status": status, "bytes": size, "expected_bytes": expected, "samples": written, "overflows": 0,
            "actual_start": started.isoformat(), "simulated": True,
            "applied_settings": {"simulator": True, "sample_rate": rate, "noise_lsb": round(noise, 1)},
            "error": None if status == "recorded" else f"{size}/{expected} bytes"}


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
            fn = {"rtlsdr": capture_rtl, "sdrplay": capture_sdrplay, "sim": capture_sim}.get(run["driver"], capture_soapy)
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
