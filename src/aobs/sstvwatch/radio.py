"""The watch's IQ source: one SoapySDR device opened while the lease is held, retuned for each dwell.

Same settings path as the broker's own Soapy capture (scheduling.capture._record_soapy): rate and frequency are read
back and checked; a receiver without gain elements (the Airspy HF+) runs on its AGC; overflows are counted; no
samples for STALL_S means the receiver went away. Tests and the development environment never open a device
(inventory.hosts.hardware_blocked); they pass a source of their own.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

import numpy as np

STALL_S = 5.0
BLOCK = 65_536


class RadioError(RuntimeError):
    pass


class Source(Protocol):
    rate: int

    def tune(self, center_hz: int) -> None: ...
    def read(self) -> np.ndarray: ...        # complex64 block; raises RadioError when the stream dies
    def close(self) -> None: ...


class SoapySource:
    def __init__(self, driver: str, serial: str, rate: int, gain_db: float | None = None):
        from ..inventory.hosts import hardware_blocked
        why = hardware_blocked()
        if why:
            raise RadioError(f"not opening {driver} {serial}: {why}")
        import SoapySDR
        from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
        self._rx = SOAPY_SDR_RX
        args = f"driver={driver}" + (f",serial={serial}" if serial else "")   # as the broker's captures open it
        try:
            self.sdr = SoapySDR.Device(args)
        except Exception as exc:  # noqa: BLE001
            raise RadioError(f"cannot open {args}: {exc}") from None
        self.sdr.setSampleRate(SOAPY_SDR_RX, 0, rate)
        got = self.sdr.getSampleRate(SOAPY_SDR_RX, 0)
        if abs(got - rate) > 1:
            raise RadioError(f"{args} runs at {got} S/s, not {rate}")
        self.rate = int(rate)
        has_gains = bool(self.sdr.listGains(SOAPY_SDR_RX, 0))
        self.sdr.setGainMode(SOAPY_SDR_RX, 0, not has_gains or gain_db is None)
        if has_gains and gain_db is not None:
            self.sdr.setGain(SOAPY_SDR_RX, 0, float(gain_db))
        self.buf = np.empty(BLOCK, np.complex64)
        self.stream = self.sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        self.active = False
        self.overflows = 0

    def tune(self, center_hz: int) -> None:
        self.sdr.setFrequency(self._rx, 0, float(center_hz))
        got = self.sdr.getFrequency(self._rx, 0)
        if abs(got - center_hz) > 1000:
            raise RadioError(f"tuned {got:.0f} Hz, not {center_hz} Hz")
        if not self.active:
            self.sdr.activateStream(self.stream)
            self.active = True

    def read(self) -> np.ndarray:
        last = time.monotonic()
        while True:
            res = self.sdr.readStream(self.stream, [self.buf], BLOCK, timeoutUs=1_000_000)
            if res.ret > 0:
                return self.buf[: res.ret].copy()
            if res.ret == -4:
                self.overflows += 1
                last = time.monotonic()
            elif res.ret not in (0, -1):
                raise RadioError(f"SoapySDR stream error {res.ret}")
            elif time.monotonic() - last > STALL_S:
                raise RadioError(f"no samples for {STALL_S:.0f} s: the receiver stopped streaming")

    def close(self) -> None:
        try:
            if self.active:
                self.sdr.deactivateStream(self.stream)
            self.sdr.closeStream(self.stream)
        except Exception:  # noqa: BLE001 - closing a device that went away
            pass
        self.sdr = None  # type: ignore[assignment]


def open_source(w: Any) -> Source:
    return SoapySource(w.driver, w.serial, w.sample_rate_hz, w.gain_db)
