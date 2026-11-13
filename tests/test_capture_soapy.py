"""SoapySDR read loop: a read that returns 0 samples is not an error (an Airspy pass died on one at 2.76 GB)."""

import datetime as dt
import sys
import types

import pytest

from aobs.scheduling import capture


class _Res:
    def __init__(self, ret: int) -> None:
        self.ret = ret


class _FakeDevice:
    """Alternates empty reads, timeouts and an overflow with real data."""

    def __init__(self, _args: str) -> None:
        self.rate = self.freq = 0.0
        self.script = [0, 64, -1, 0, 64, -4, 64]

    def getHardwareInfo(self): return {}
    def setSampleRate(self, _d, _c, rate): self.rate = rate
    def setFrequency(self, _d, _c, freq): self.freq = freq
    def setBandwidth(self, *_): pass
    def setGainMode(self, *_): pass
    def setGain(self, *_): pass
    def getGain(self, *_): return 0.0
    def getSampleRate(self, *_): return self.rate
    def getFrequency(self, *_): return self.freq
    def getBandwidth(self, *_): return 0.0
    def getGainMode(self, *_): return False
    def setupStream(self, *_): return object()
    def activateStream(self, _s): pass
    def deactivateStream(self, _s): pass
    def closeStream(self, _s): pass

    def readStream(self, _s, bufs, n, timeoutUs=0):
        ret = self.script.pop(0) if self.script else 0
        if ret > 0:
            bufs[0][: 2 * ret] = 1
        return _Res(ret)


def test_zero_sample_reads_do_not_abort_the_recording(tmp_path, monkeypatch):
    soapy = types.ModuleType("SoapySDR")
    soapy.Device, soapy.SOAPY_SDR_CF32, soapy.SOAPY_SDR_CS16, soapy.SOAPY_SDR_RX = _FakeDevice, "CF32", "CS16", 0
    monkeypatch.setitem(sys.modules, "SoapySDR", soapy)
    now = capture.utc_now()
    run = {"radio": {"sample_rate_hz": 1000, "center_frequency_hz": 137_900_000},
           "format": "cs16", "soapy_driver": "airspy", "serial": "x"}
    out = capture.capture_soapy(run, tmp_path / "rec.cs16.part", now, now + dt.timedelta(seconds=0.3))
    assert out["samples"] == 3 * 64
    assert out["bytes"] == 3 * 64 * 4
    assert out["overflows"] == 1


def test_a_receiver_that_stops_streaming_ends_the_recording_with_a_clear_error(tmp_path, monkeypatch):
    soapy = types.ModuleType("SoapySDR")
    soapy.Device, soapy.SOAPY_SDR_CF32, soapy.SOAPY_SDR_CS16, soapy.SOAPY_SDR_RX = _FakeDevice, "CF32", "CS16", 0
    monkeypatch.setitem(sys.modules, "SoapySDR", soapy)
    monkeypatch.setattr(capture, "STALL_S", 0.2)
    now = capture.utc_now()
    run = {"radio": {"sample_rate_hz": 1000, "center_frequency_hz": 137_900_000},
           "format": "cs16", "soapy_driver": "airspy", "serial": "x"}
    with pytest.raises(RuntimeError, match="stopped streaming"):    # the script runs out, then only 0-sample reads
        capture.capture_soapy(run, tmp_path / "rec.cs16.part", now, now + dt.timedelta(seconds=30))
