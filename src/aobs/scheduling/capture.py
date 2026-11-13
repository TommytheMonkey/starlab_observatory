"""One recording, run as its own process: `python -m aobs.scheduling.capture <run.json>`.

The broker starts this in a new session, so a broker restart never interrupts a
recording; the outcome is written to <raw>.result.json for whoever is watching.
RTL-SDR captures run rtl_sdr with -d <librtlsdr index>, looked up from the serial just before the start (rtl_sdr
reads a -d value made of digits, such as 00000001, as an index before trying it as a serial), and the recording
fails unless rtl_sdr's own "Using device N" names the receiver's serial. SDRplay and other SoapySDR receivers
(Airspy, ...) open SoapySDR by driver and serial. Settings are applied exactly as given
and read back; a setting the driver rejects fails the capture instead of being skipped.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
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


RTL_WARMUP_S = 4.5      # rtl_sdr takes 1.5-1.85 s from exec to its first sample (31 passes, Nov 2026); then 2 s of
                        # pre-roll are checked for a replayed buffer (PRETEST_S), so the check ends before the start


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


REPEAT_SNIPPET = 4096        # bytes compared: receiver noise never repeats this much byte for byte
REPEAT_OFFSET = 4 << 20      # where the snippet is taken (a few MB in, past the USB start-up) ...
REPEAT_WINDOW = 8 << 20      # ... and how far after it an exact copy is searched for
REPEAT_MIN_VALUES = 16       # a snippet with fewer distinct bytes is too uniform to prove anything (flat check's job)


def find_repeat(data: bytes, offset: int, snippet: int = REPEAT_SNIPPET, window: int = REPEAT_WINDOW) -> int | None:
    """Distance in bytes to the first exact copy of data[offset:offset + snippet] in the `window` bytes after it, or
    None. A stuck RTL-SDR can replay one USB buffer instead of fresh ADC data: the ISS pass on OBS-R07 (2026-11-12)
    repeated every 3,760 bytes (1.84 ms) for all 11 minutes while level, spread and code counts all looked healthy. Defined
    here, not in iqhealth, because this file runs on its own on remote hosts; iqhealth imports it."""
    snip = data[offset:offset + snippet]
    if len(snip) < snippet or len(set(snip)) < REPEAT_MIN_VALUES:
        return None
    at = data.find(snip, offset + 1, offset + snippet + window)
    return at - offset if at >= 0 else None


def repeat_offset(size: int) -> int:
    """REPEAT_OFFSET, or a quarter of the way into a shorter file (a 2 s test capture), on a sample boundary."""
    return min(REPEAT_OFFSET, size // 4) & ~1


PRETEST_S = 2.0         # the first 2 s of the pass's own stream (pre-roll) are checked for a replayed buffer
PRETEST_GRACE_S = 3.0   # if those bytes have not arrived this long after they should have, the check is inconclusive
PRETEST_SKIP_CODE = "repeating_buffer"


def pretest(part: pathlib.Path, rate: int, start: dt.datetime) -> dict[str, Any]:
    """find_repeat on the first PRETEST_S of the recording itself, while rtl_sdr keeps streaming. Checking the pass's
    own stream needs no second device open (a dongle can come back stuck from any open) and costs no time: the
    recorder streams this pre-roll anyway. {"repeat": True} skips the pass; an inconclusive check lets it record,
    and the recording's own checks (iqhealth) apply."""
    need = int(rate * PRETEST_S) * 2
    with part.open("rb") as fh:
        data = fh.read(need)
    off = repeat_offset(len(data))
    now = utc_now()
    out: dict[str, Any] = {"ran": True, "seconds": PRETEST_S, "bytes": len(data), "offset_bytes": off,
                           "at": now.isoformat(), "before_start_s": round((start - now).total_seconds(), 2),
                           "repeat": False, "conclusive": False}
    if off == 0 or len(data) < off + 2 * REPEAT_SNIPPET:
        out["note"] = f"only {len(data)} bytes arrived: inconclusive"
        return out
    out["conclusive"] = True
    period = find_repeat(data, off)
    if period is not None:
        out.update(repeat=True, period_bytes=period, period_ms=round(period / 2 / rate * 1000, 3))
    return out


def _pretest_logged(part: pathlib.Path, rate: int, start: dt.datetime) -> dict[str, Any]:
    pre = pretest(part, rate, start)
    print("pre-pass check: " + (f"REPEATING every {pre['period_bytes']} bytes ({pre['period_ms']} ms)" if pre["repeat"]
                                else pre.get("note") or "ok, no repeat")
          + f" ({pre['before_start_s']:+.2f} s before the start)", flush=True)
    return pre


def _keep_pretest(part: pathlib.Path, pre: dict[str, Any]) -> None:
    """A repeating pre-roll is evidence: kept (cut to the bytes checked) as <raw>.pretest, never as a recording."""
    ev = part.with_name(part.name.removesuffix(".part") + ".pretest")
    with part.open("r+b") as fh:
        fh.truncate(pre["bytes"])
    part.rename(ev)
    pre["evidence_file"] = str(ev)


DEVICE_CONFIRM_S = 2.0   # rtl_sdr prints "Using device N" before it streams; allow this long after the first sample
RTL_LIST_LINE = re.compile(r"^\s*(\d+):\s+.*\bSN:\s*(\S*)\s*$")
RTL_USING_LINE = re.compile(r"^Using device (\d+):")


def rtl_index(serial: str) -> int:
    """librtlsdr index of the one connected RTL-SDR with this serial. Self-contained (ctypes), because a remote
    host runs this file on its own."""
    import ctypes
    import ctypes.util
    name = ctypes.util.find_library("rtlsdr")
    if not name:
        raise RuntimeError("librtlsdr not found, so the receiver's device index cannot be looked up")
    L = ctypes.CDLL(name)
    L.rtlsdr_get_device_count.restype = ctypes.c_uint32
    L.rtlsdr_get_device_usb_strings.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    serials = []
    for i in range(int(L.rtlsdr_get_device_count())):
        m, p, s = (ctypes.create_string_buffer(256) for _ in range(3))
        ok = L.rtlsdr_get_device_usb_strings(ctypes.c_uint32(i), m, p, s) == 0
        serials.append(s.value.decode(errors="replace") if ok else "")
    hits = [i for i, s in enumerate(serials) if s == serial]
    if len(hits) != 1:
        raise RuntimeError(f"{len(hits)} connected RTL-SDRs have serial {serial} (connected: "
                           f"{', '.join(s or '?' for s in serials) or 'none'}); not recording")
    return hits[0]


class RtlDeviceCheck:
    """Reads rtl_sdr's output (copied to the log) for its device list and "Using device N", and decides whether
    the device it opened is the one asked for: index N, listed with the expected serial."""

    def __init__(self, serial: str, index: int):
        self.serial, self.index = serial, index
        self.listed: dict[int, str] = {}
        self.using: int | None = None

    def feed(self, line: str) -> None:
        m = RTL_LIST_LINE.match(line)
        if m:
            self.listed[int(m.group(1))] = m.group(2)
        m = RTL_USING_LINE.match(line)
        if m and self.using is None:
            self.using = int(m.group(1))

    def problem(self) -> str | None:
        """None when confirmed; otherwise why not (also while rtl_sdr has not said which device it opened)."""
        if self.using is None:
            return "rtl_sdr did not say which device it opened"
        if self.using != self.index:
            return f"rtl_sdr opened device {self.using}, not device {self.index} ({self.serial})"
        got = self.listed.get(self.using)
        if got != self.serial:
            return (f"rtl_sdr opened device {self.using}, which it lists as serial {got or 'unknown'}, not "
                    f"{self.serial}; the device order changed or another librtlsdr enumerates differently")
        return None


def _pump(stream: Any, check: RtlDeviceCheck) -> None:
    for raw in iter(stream.readline, b""):
        line = raw.decode(errors="replace")
        sys.stdout.write(line)
        sys.stdout.flush()
        check.feed(line.rstrip("\n"))


def _record_rtl(run: dict[str, Any], part: pathlib.Path, start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
    """rtl_sdr started early (so samples flow by `start`) with no sample limit (-n is a 32-bit byte
    count, ~15 min at 2.4 MS/s), and stopped at `end`. Coverage is measured from the first sample. The first
    PRETEST_S of the stream are checked for a replayed buffer as soon as they arrive (pretest); if they repeat,
    rtl_sdr is stopped at once and the pass is skipped (status "skipped", skip_code "repeating_buffer")."""
    r = run["radio"]
    gain = "0" if r["gain_mode"] == "auto" else str(r["gain_db"])  # rtl_sdr: -g 0 means tuner AGC
    _sleep_until(start - dt.timedelta(seconds=RTL_WARMUP_S))
    if STOP:
        raise RuntimeError("stopped before the recording started")
    index = rtl_index(run["serial"])
    cmd = [run.get("rtl_sdr_path", "/usr/bin/rtl_sdr"), "-d", str(index), "-f", str(r["center_frequency_hz"]),
           "-s", str(r["sample_rate_hz"]), "-g", gain, "-p", str(r.get("ppm_correction", 0)), str(part)]
    print(f"device index {index} = serial {run['serial']}", flush=True)
    print("exec:", " ".join(cmd), flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    check = RtlDeviceCheck(run["serial"], index)
    pump = threading.Thread(target=_pump, args=(proc.stdout, check), daemon=True)
    pump.start()
    first_sample = None
    ended_early = False
    wrong_device = None
    pre: dict[str, Any] | None = None
    need = int(r["sample_rate_hz"] * PRETEST_S) * 2
    while proc.poll() is None and not STOP and utc_now() < end:
        if first_sample is None and part.exists() and part.stat().st_size > 0:
            first_sample = utc_now()
        if pre is None and first_sample is not None and (
                part.stat().st_size >= need or (utc_now() - first_sample).total_seconds() > PRETEST_S + PRETEST_GRACE_S):
            pre = _pretest_logged(part, r["sample_rate_hz"], start)
            if pre["repeat"]:
                break
        why = check.problem() if check.using is not None or first_sample is not None else None
        if why and (check.using is not None or (utc_now() - first_sample).total_seconds() > DEVICE_CONFIRM_S):
            wrong_device = why
            break
        time.sleep(0.02)
    if proc.poll() is None:
        proc.terminate()                          # rtl_sdr flushes and closes the file on SIGTERM
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    elif wrong_device is None:
        ended_early = utc_now() < end
    pump.join(timeout=5)
    if wrong_device is None and (first_sample is not None or check.using is not None):
        wrong_device = check.problem()        # samples arrived but the device was never confirmed, or is wrong
    if wrong_device:
        print(f"wrong device: {wrong_device}", flush=True)
    if pre is None:                           # the stream ended before the check ran: check what arrived
        pre = _pretest_logged(part, r["sample_rate_hz"], start) if first_sample is not None and part.exists() else \
            {"ran": False, "repeat": False, "conclusive": False, "note": "no samples arrived"}
    skipped = bool(pre["repeat"]) and not wrong_device
    if skipped:
        _keep_pretest(part, pre)
    stopped = min(utc_now(), end)
    size = part.stat().st_size if part.exists() else 0
    began = first_sample or stopped
    expected = int(r["sample_rate_hz"] * (stopped - began).total_seconds()) * 2
    late_s = max(0.0, (began - start).total_seconds())
    if wrong_device:
        status = "failed"
    elif skipped:
        status = "skipped"
    elif STOP:
        status = "interrupted"
    elif ended_early or first_sample is None:
        status = "failed"
    else:
        status = "recorded" if size >= expected * 0.98 else "failed"
    error = None
    if wrong_device:
        error = f"wrong receiver: {wrong_device}; the file is not from {run['serial']}"
    elif skipped:
        error = (f"the first {PRETEST_S:g} s of samples repeat every {pre['period_bytes']} bytes ({pre['period_ms']} ms): "
                 f"{run['serial']} is replaying a stale buffer and needs a physical power cycle; the pass was not recorded")
    elif status != "recorded":
        error = f"rtl_sdr {'exited early ' if ended_early else ''}(exit {proc.returncode}), {size}/{expected} bytes"
    return {"status": status, "return_code": proc.returncode, "bytes": size, "expected_bytes": expected,
            "actual_start": began.isoformat(), "first_sample_late_s": round(late_s, 2), "command": cmd,
            "device": {"serial": run["serial"], "index": index, "opened": check.using,
                       "listed_serial": check.listed.get(check.using) if check.using is not None else None,
                       "confirmed": check.problem() is None},
            "warning": f"first sample {late_s:.1f} s after the scheduled start" if late_s > 0.5 else None,
            "error": error, "pretest": pre, **({"skip_code": PRETEST_SKIP_CODE} if skipped else {})}


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
