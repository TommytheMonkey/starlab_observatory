"""Stepped captures: one open receiver, many (frequency, gain) steps. Used by wideband scans and gain sweeps.

The receiver is opened once and retuned in place, so a 45-step scan does not pay 45 device opens:
  * RTL-SDR through librtlsdr itself (ctypes), the library rtl_sdr uses: set_center_freq / set_tuner_gain,
    reset_buffer, then a settle period of samples is read and discarded before the dwell is kept;
  * SDRplay through SoapySDR with the stream left running: setFrequency / setGain, then the settle
    period is read and discarded the same way.
Every kept sample goes into one raw file (the receiver's native format) and each step's position,
frequency, gain, timing and overflow count go into <raw>.steps.json, so the analysis is reproducible from
the raw IQ. Nothing here changes the bias tee except to hold it at the job's (validated) value.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import datetime as dt
import json
import pathlib
import time
from typing import Any, Callable, Protocol

import numpy as np

STALL_S = 5.0          # no samples for this long: the receiver is gone (e.g. dropped off USB)

RTL_LIB = ("/usr/local/lib/librtlsdr.so.0", "librtlsdr.so.0", "librtlsdr.so")
READ_CHUNK = 1 << 18                      # bytes per rtlsdr_read_sync call (multiple of 512)


class Device(Protocol):
    bytes_per_sample: int

    def tune(self, hz: int) -> int: ...
    def set_gain(self, gain: Any) -> Any: ...
    def read(self, n_samples: int) -> tuple[bytes, int]: ...      # (raw bytes, overflow events)
    def close(self) -> None: ...


class RtlDevice:
    """Minimal librtlsdr binding (only what stepped captures need)."""
    bytes_per_sample = 2

    def __init__(self, serial: str, rate: int, ppm: int = 0, bias_tee: bool = False, lib: str | None = None):
        self.lib = self._load(lib)
        L = self.lib
        L.rtlsdr_get_index_by_serial.argtypes = [ctypes.c_char_p]
        idx = L.rtlsdr_get_index_by_serial(serial.encode())
        if idx < 0:
            raise RuntimeError(f"no RTL-SDR with serial {serial} (librtlsdr returned {idx})")
        self.dev = ctypes.c_void_p()
        r = L.rtlsdr_open(ctypes.byref(self.dev), ctypes.c_uint32(idx))
        if r < 0:
            raise RuntimeError(f"rtlsdr_open({serial}) failed ({r}); the receiver is probably in use")
        self.applied: dict[str, Any] = {"serial": serial, "index": idx}
        try:
            self._ok(L.rtlsdr_set_sample_rate(self.dev, ctypes.c_uint32(rate)), "set_sample_rate")
            self.applied["sample_rate"] = int(L.rtlsdr_get_sample_rate(self.dev))
            if ppm:
                self._ok(L.rtlsdr_set_freq_correction(self.dev, ctypes.c_int(ppm)), "set_freq_correction")
            # hold the bias tee at the validated value (never turned on unless the receiver requires it)
            if hasattr(L, "rtlsdr_set_bias_tee"):
                L.rtlsdr_set_bias_tee(self.dev, ctypes.c_int(1 if bias_tee else 0))
            self._ok(L.rtlsdr_set_agc_mode(self.dev, ctypes.c_int(0)), "set_agc_mode")
        except Exception:
            self.close()
            raise

    @staticmethod
    def _load(path: str | None) -> Any:
        for p in ([path] if path else []) + list(RTL_LIB) + [ctypes.util.find_library("rtlsdr") or ""]:
            if not p:
                continue
            try:
                return ctypes.CDLL(p)
            except OSError:
                continue
        raise RuntimeError("librtlsdr not found")

    @staticmethod
    def _ok(r: int, what: str) -> None:
        if r < 0:
            raise RuntimeError(f"rtlsdr_{what} failed ({r})")

    def tune(self, hz: int) -> int:
        self._ok(self.lib.rtlsdr_set_center_freq(self.dev, ctypes.c_uint32(int(hz))), "set_center_freq")
        return int(self.lib.rtlsdr_get_center_freq(self.dev))

    def set_gain(self, gain: Any) -> Any:
        if gain in (None, "auto"):
            self._ok(self.lib.rtlsdr_set_tuner_gain_mode(self.dev, ctypes.c_int(0)), "set_tuner_gain_mode")
            return "auto"
        self._ok(self.lib.rtlsdr_set_tuner_gain_mode(self.dev, ctypes.c_int(1)), "set_tuner_gain_mode")
        self._ok(self.lib.rtlsdr_set_tuner_gain(self.dev, ctypes.c_int(int(round(float(gain) * 10)))), "set_tuner_gain")
        return self.lib.rtlsdr_get_tuner_gain(self.dev) / 10.0

    def read(self, n_samples: int) -> tuple[bytes, int]:
        self._ok(self.lib.rtlsdr_reset_buffer(self.dev), "reset_buffer")
        want = n_samples * 2
        out = bytearray()
        buf = ctypes.create_string_buffer(READ_CHUNK)
        got = ctypes.c_int(0)
        while len(out) < want:
            n = min(READ_CHUNK, ((want - len(out) + 511) // 512) * 512)
            self._ok(self.lib.rtlsdr_read_sync(self.dev, buf, ctypes.c_int(n), ctypes.byref(got)), "read_sync")
            if got.value <= 0:
                raise RuntimeError("rtlsdr_read_sync returned no data")
            out += buf.raw[:got.value]
        return bytes(out[:want]), 0

    def close(self) -> None:
        if getattr(self, "dev", None) is not None and self.dev.value:
            try:
                self.lib.rtlsdr_close(self.dev)
            finally:
                self.dev = ctypes.c_void_p()


class SoapyDevice:
    """SDRplay through SoapySDR with one running stream; retuning does not restart it."""

    def __init__(self, serial: str, radio: dict[str, Any], fmt: str):
        import SoapySDR
        from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_CS16, SOAPY_SDR_RX
        self.S, self.RX = SoapySDR, SOAPY_SDR_RX
        self.fmt = fmt
        self.bytes_per_sample = 4 if fmt == "cs16" else 8
        self.sdr = SoapySDR.Device(f"driver=sdrplay,serial={serial}")
        s = self.sdr
        s.writeSetting("biasT_ctrl", "true" if radio.get("bias_tee") else "false")
        if s.readSetting("biasT_ctrl") != ("true" if radio.get("bias_tee") else "false"):
            raise RuntimeError("bias tee readback does not match the request")
        s.setSampleRate(SOAPY_SDR_RX, 0, radio["sample_rate_hz"])
        s.setBandwidth(SOAPY_SDR_RX, 0, radio["bandwidth_hz"])
        s.setGainMode(SOAPY_SDR_RX, 0, bool(radio.get("agc")))
        for k, v in (radio.get("device_settings") or {}).items():
            s.writeSetting(k, "true" if v else "false")
        self.applied = {"sample_rate": s.getSampleRate(SOAPY_SDR_RX, 0), "bandwidth": s.getBandwidth(SOAPY_SDR_RX, 0)}
        self.stream = s.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16 if fmt == "cs16" else SOAPY_SDR_CF32)
        self.buf = np.empty(2 * 65536, np.int16) if fmt == "cs16" else np.empty(65536, np.complex64)
        self.active = False

    def tune(self, hz: int) -> int:
        self.sdr.setFrequency(self.RX, 0, int(hz))
        if not self.active:
            self.sdr.activateStream(self.stream)
            self.active = True
        return int(self.sdr.getFrequency(self.RX, 0))

    def set_gain(self, gain: Any) -> Any:
        g = gain or {}
        if g.get("IFGR") is not None and not self.sdr.getGainMode(self.RX, 0):
            self.sdr.setGain(self.RX, 0, "IFGR", int(g["IFGR"]))
        if g.get("RFGR") is not None:
            self.sdr.setGain(self.RX, 0, "RFGR", int(g["RFGR"]))
        return {"IFGR": self.sdr.getGain(self.RX, 0, "IFGR"), "RFGR": self.sdr.getGain(self.RX, 0, "RFGR")}

    def read(self, n_samples: int) -> tuple[bytes, int]:
        out, got, over = [], 0, 0
        last_data = time.monotonic()
        per = 2 if self.fmt == "cs16" else 1
        while got < n_samples:
            res = self.sdr.readStream(self.stream, [self.buf], min(65536, n_samples - got), timeoutUs=1_000_000)
            if res.ret > 0:
                out.append(self.buf[: res.ret * per].tobytes())
                got += res.ret
                last_data = time.monotonic()
            elif res.ret == -4:
                over += 1
                last_data = time.monotonic()
            elif res.ret not in (0, -1):    # 0 = nothing this read, -1 = timeout: keep reading
                raise RuntimeError(f"SoapySDR stream error {res.ret}")
            elif time.monotonic() - last_data > STALL_S:
                raise RuntimeError(f"no samples for {STALL_S:.0f} s: the receiver stopped streaming "
                                   "(look for a USB disconnect in the kernel log)")
        return b"".join(out), over

    def close(self) -> None:
        try:
            if self.active:
                self.sdr.deactivateStream(self.stream)
            self.sdr.closeStream(self.stream)
        except Exception:  # noqa: BLE001
            pass


def run_steps(dev: Device, rate: int, steps: list[dict[str, Any]], part: pathlib.Path, deadline: dt.datetime,
              stopped: Callable[[], bool], now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
              settle_s: float = 0.06, repeats: int = 1) -> dict[str, Any]:
    """Execute the steps (repeated `repeats` times) until done, stopped or the deadline. Raw IQ appended to
    `part`; returns the step index. The device is NOT closed here (the caller owns it)."""
    index: list[dict[str, Any]] = []
    offset = 0
    gain_now: Any = object()
    applied_gain: Any = None
    started = now()
    done = True
    with part.open("xb") as out:
        for sweep in range(repeats):
            for k, st in enumerate(steps):
                if stopped() or now() >= deadline:
                    done = False
                    break
                if st.get("gain") != gain_now:
                    applied_gain = dev.set_gain(st.get("gain"))
                    gain_now = st.get("gain")
                tuned = dev.tune(int(st["center_hz"]))
                settle = int(rate * float(st.get("settle_s", settle_s)))
                if settle:
                    dev.read(settle)                                      # discard: PLL/AGC settling
                t0 = now()
                n = int(rate * float(st["dwell_s"]))
                raw, over = dev.read(n)
                out.write(raw)
                index.append({"sweep": sweep, "step": k, "center_hz": int(st["center_hz"]), "tuned_hz": tuned,
                              "gain": st.get("gain"), "applied_gain": applied_gain, "offset_samples": offset,
                              "samples": len(raw) // dev.bytes_per_sample, "started_at": t0.isoformat(),
                              "overflows": over})
                offset += len(raw) // dev.bytes_per_sample
            if not done:
                break
    planned = len(steps) * repeats
    return {"steps": index, "planned_steps": planned, "completed_steps": len(index), "complete": len(index) == planned,
            "started_at": started.isoformat(), "finished_at": now().isoformat(), "sample_rate_hz": rate,
            "bytes_per_sample": dev.bytes_per_sample, "settle_s": settle_s, "repeats": repeats}


def capture(run: dict[str, Any], part: pathlib.Path, start: dt.datetime, end: dt.datetime,
            stopped: Callable[[], bool], open_device: Callable[[dict[str, Any]], Device] | None = None) -> dict[str, Any]:
    """The capture-process entry point for a stepped job (run["steps"])."""
    r, sw = run["radio"], run["steps"]
    while not stopped() and dt.datetime.now(dt.timezone.utc) < start - dt.timedelta(seconds=2):
        time.sleep(0.05)
    if stopped():
        raise RuntimeError("stopped before the recording started")
    if open_device is None:
        def open_device(run: dict[str, Any]) -> Device:
            if run["driver"] == "rtlsdr":
                return RtlDevice(run["serial"], int(r["sample_rate_hz"]), int(r.get("ppm_correction", 0)), bool(r.get("bias_tee")))
            return SoapyDevice(run["serial"], r, run["format"])
    dev = open_device(run)
    try:
        idx = run_steps(dev, int(r["sample_rate_hz"]), sw["steps"], part, end, stopped,
                        settle_s=float(sw.get("settle_s", 0.06)), repeats=int(sw.get("repeats", 1)))
    finally:
        dev.close()                             # always release the receiver, even after an error
    idx["applied"] = getattr(dev, "applied", None)
    pathlib.Path(str(part).removesuffix(".part") + ".steps.json").write_text(json.dumps(idx, indent=1))
    size = part.stat().st_size
    status = "interrupted" if stopped() else ("recorded" if idx["completed_steps"] > 0 else "failed")
    return {"status": status, "bytes": size, "expected_bytes": size, "steps_completed": idx["completed_steps"],
            "steps_planned": idx["planned_steps"], "actual_start": idx["started_at"],
            "warning": None if idx["complete"] else f"only {idx['completed_steps']} of {idx['planned_steps']} steps "
                                                    "before the reservation ended",
            "error": None if idx["completed_steps"] else "no step completed",
            "overflows": sum(s["overflows"] for s in idx["steps"])}
