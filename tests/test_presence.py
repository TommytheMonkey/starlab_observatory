"""USB presence: RTL-SDR by sysfs serial; SDRplay also by the API's device list while its bootloader hides the serial."""

from __future__ import annotations

import datetime as dt
import pathlib
import subprocess
from typing import Any

import pytest

from aobs.hardware import sdrplay
from aobs.scheduling.presence import ABSENT, PRESENT, UNKNOWN, PresenceProbe
from aobs.scheduling.status import StatusService
from test_scheduler import env, rtl_job, submit  # noqa: F401
from test_testlab import lab  # noqa: F401

RSP = "240526D360"


class Usb:
    """A fake /sys/bus/usb/devices."""
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        root.mkdir(exist_ok=True)

    def plug(self, port: str, vendor: str, product: str, serial: str | None, devnum: int = 5) -> None:
        d = self.root / port
        d.mkdir(exist_ok=True)
        for name, value in (("idVendor", vendor), ("idProduct", product), ("devnum", str(devnum)), ("serial", serial)):
            if value is None:
                (d / name).unlink(missing_ok=True)
            else:
                (d / name).write_text(value + "\n")

    def unplug(self, port: str) -> None:
        for f in (self.root / port).iterdir():
            f.unlink()
        (self.root / port).rmdir()

    def rtl(self, port: str, serial: str) -> None:
        self.plug(port, "0bda", "2838", serial)

    def rsp_bootloader(self, devnum: int = 10) -> None:     # as at power-up: no serial string
        self.plug("3-10.2", "1df7", "3050", None, devnum)

    def rsp_firmware(self, devnum: int = 14) -> None:       # after the API loaded the firmware
        self.plug("3-10.2", "1df7", "3050", RSP, devnum)


class Api:
    def __init__(self) -> None:
        self.answer = sdrplay.ApiFind(True, frozenset({RSP}), f"SoapySDRPlay3 lists {RSP}")
        self.state = "active"
        self.calls = 0

    def find(self) -> sdrplay.ApiFind:
        self.calls += 1
        return self.answer


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def usb(tmp_path: pathlib.Path) -> Usb:
    u = Usb(tmp_path / "usb")
    for port, serial in (("3-5", "OBS-R01"), ("3-10.3", "OBS-R02"), ("3-3", "OBS-R05")):
        u.rtl(port, serial)
    return u


@pytest.fixture
def probe(env, usb):  # noqa: F811
    """The live probe wired into the scheduler exactly as in production, over fake USB and API."""
    api, mono = Api(), Mono()
    p = PresenceProbe(sysfs=usb.root, find=api.find, api_state=lambda: api.state,
                      quiet=env.s.sdrplay_recording_window, clock=mono)
    env.s._probe, env.s._present = p, p
    p.api, p.mono = api, mono
    return p


def rsp_job(name: str = "rsp", start: float = 60, end: float = 76) -> dict[str, Any]:
    job = rtl_job(name, rx="RSP1B", start=start, end=end, receiver__serial=RSP, receiver__driver="sdrplay",
                  recording__engine="soapysdr", recording__format="cs16", satdump__baseband_format="cs16",
                  radio__sample_rate_hz=2_000_000, satdump__sample_rate_hz=2_000_000, radio__bandwidth_hz=1_536_000,
                  radio__gain_db=None)
    job["radio"]["gain_elements"] = {"IFGR": 40, "RFGR": 4}
    return job


def rx(env, rid: str):  # noqa: F811
    return env.s.receivers[rid]


# ---------------------------------------------------------------- SDRplay


def test_sdrplay_with_serial_on_usb_needs_no_api_call(env, usb, probe):  # noqa: F811
    usb.rsp_firmware()
    assert env.s.presence(rx(env, "RSP1B")).state == PRESENT
    assert probe.api.calls == 0


def test_sdrplay_bootloader_is_identified_through_soapysdr(env, usb, probe, lab):  # noqa: F811
    """The reported bug: on USB since power-up, no serial in sysfs until the API loads the firmware."""
    usb.rsp_bootloader()
    p = env.s.presence(rx(env, "RSP1B"))
    assert p.state == PRESENT and "SDRplay API" in p.detail and probe.api.calls == 1
    caps = {r["receiver_id"]: r for r in env.s.capabilities()["receivers"]}
    assert caps["RSP1B"]["connected"] is True and caps["RSP1B"]["available_now"] is True
    st = StatusService(env.s).receiver(rx(env, "RSP1B"))
    assert st["connected"] is True and st["state"] == "IDLE" and st["presence"]["state"] == PRESENT
    av = lab.availability("RSP1B")
    assert av["available"], av["reasons"]
    assert not any("not on USB" in r for r in av["reasons"])
    assert submit(env, rsp_job())["results"][0]["ok"]
    assert probe.api.calls == 1                   # dashboard refreshes reuse the cached enumeration


def test_sdrplay_physically_disconnected(env, usb, probe, lab):  # noqa: F811
    p = env.s.presence(rx(env, "RSP1B"))
    assert p.state == ABSENT and probe.api.calls == 0      # no SDRplay vendor device at all: no need to ask
    assert StatusService(env.s).receiver(rx(env, "RSP1B"))["state"] == "UNAVAILABLE"
    assert f"RSP1B (serial {RSP}) is not on USB" in lab.availability("RSP1B")["reasons"][0]
    assert "not connected right now" in " ".join(submit(env, rsp_job())["results"][0]["errors"])


def test_api_lists_another_rsp_means_ours_is_absent(env, usb, probe):  # noqa: F811
    usb.rsp_bootloader()
    probe.api.answer = sdrplay.ApiFind(True, frozenset({"9999OTHER"}), "SoapySDRPlay3 lists 9999OTHER")
    assert env.s.presence(rx(env, "RSP1B")).state == ABSENT


def test_sdrplay_busy_recording_is_never_reported_disconnected(env, usb, probe):  # noqa: F811
    usb.rsp_firmware()
    jid = submit(env, rsp_job("pass"))["results"][0]["job_id"]
    env.clock.now = dt.datetime.fromisoformat(env.s.get(jid)["reservation_start"]) + dt.timedelta(seconds=90)
    env.s.transition(jid, "recording", "test")
    st = StatusService(env.s).receiver(rx(env, "RSP1B"))
    assert st["connected"] is True and st["state"] == "RECORDING"
    # the same window with the serial hidden: enumeration is deferred, never "absent"
    usb.rsp_bootloader()
    p = env.s.presence(rx(env, "RSP1B"))
    assert (p.state, p.reason) == (UNKNOWN, "discovery_deferred") and probe.api.calls == 0
    assert env.s._present(rx(env, "RSP1B")) is True
    st = StatusService(env.s).receiver(rx(env, "RSP1B"))
    assert st["connected"] is None and st["state"] == "RECORDING"


def test_sdrplay_api_service_down_is_unknown_not_absent(env, usb, probe, lab):  # noqa: F811
    usb.rsp_bootloader()
    probe.api.answer, probe.api.state = sdrplay.ApiFind(False, frozenset(), "SoapySDRUtil exit 1: api timeout"), "inactive"
    p = env.s.presence(rx(env, "RSP1B"))
    assert (p.state, p.reason) == (UNKNOWN, "api_unavailable") and "sdrplay.service is inactive" in p.detail
    assert env.s._present(rx(env, "RSP1B")) is True              # bookings are not rejected on a failed lookup
    st = StatusService(env.s).receiver(rx(env, "RSP1B"))
    assert st["connected"] is None and st["state"].startswith("UNKNOWN")
    reasons = lab.availability("RSP1B")["reasons"]
    assert any("cannot confirm RSP1B" in r and "sdrplay.service" in r for r in reasons)
    assert not any("is not on USB" in r for r in reasons)


def test_sdrplay_discovery_failure_is_retried_and_never_cached_for_long(env, usb, probe):  # noqa: F811
    usb.rsp_bootloader()
    probe.api.answer = sdrplay.ApiFind(False, frozenset(), "SoapySDRUtil failed: timed out after 10 seconds")
    p = env.s.presence(rx(env, "RSP1B"))
    assert (p.state, p.reason) == (UNKNOWN, "discovery_failed") and probe.api.calls == 1
    env.s.presence(rx(env, "RSP1B"))
    assert probe.api.calls == 1                                   # cached briefly ...
    probe.mono.t += probe.fail_ttl_s + 1
    probe.api.answer = sdrplay.ApiFind(True, frozenset({RSP}), "ok")
    assert env.s.presence(rx(env, "RSP1B")).state == PRESENT and probe.api.calls == 2   # ... then asked again


def test_cached_enumeration_expires_and_follows_a_replug(env, usb, probe):  # noqa: F811
    usb.rsp_bootloader()
    probe.api.answer = sdrplay.ApiFind(True, frozenset(), "SoapySDRPlay3 lists no device")
    assert env.s.presence(rx(env, "RSP1B")).state == UNKNOWN
    probe.api.answer = sdrplay.ApiFind(True, frozenset({RSP}), "ok")
    usb.rsp_bootloader(devnum=11)                                 # replugged: a different USB device
    assert env.s.presence(rx(env, "RSP1B")).state == PRESENT and probe.api.calls == 2
    probe.mono.t += probe.ttl_s + 1
    env.s.presence(rx(env, "RSP1B"))
    assert probe.api.calls == 3


def test_firmware_load_re_enumeration_is_not_a_disconnect(env, usb, probe):  # noqa: F811
    usb.rsp_firmware()
    assert env.s.presence(rx(env, "RSP1B")).state == PRESENT
    usb.unplug("3-10.2")
    probe.mono.t += 1
    assert env.s.presence(rx(env, "RSP1B")).reason == "re_enumerating"
    probe.mono.t += 30
    assert env.s.presence(rx(env, "RSP1B")).state == ABSENT


# ---------------------------------------------------------------- RTL-SDR


def test_rtl_sdr_presence_is_sysfs_only(env, usb, probe):  # noqa: F811
    for rid in ("OBS-R01", "OBS-R02", "OBS-R05"):
        assert env.s.presence(rx(env, rid)).state == PRESENT
    usb.unplug("3-10.3")
    p = env.s.presence(rx(env, "OBS-R02"))
    assert p.state == ABSENT and p.connected is False and env.s._present(rx(env, "OBS-R02")) is False
    usb.rsp_bootloader()                                          # an SDRplay never stands in for a missing RTL
    assert env.s.presence(rx(env, "OBS-R02")).state == ABSENT and probe.api.calls == 0


def test_signal_path_availability_reports_presence(env, usb, probe, lab):  # noqa: F811
    usb.rsp_bootloader()
    for rid in ("OBS-R02", "RSP1B"):
        av = lab.availability(rid)
        assert av["available"] and av["presence"]["state"] == PRESENT
    usb.unplug("3-10.3")
    av = lab.availability("OBS-R02")
    assert not av["available"] and av["presence"]["state"] == ABSENT and "is not on USB" in av["reasons"][0]


def test_injected_present_callable_still_works(env):  # noqa: F811
    env.present.discard("240526D360")
    assert env.s.presence(rx(env, "RSP1B")).state == ABSENT
    env.present.add("240526D360")
    assert env.s.presence(rx(env, "RSP1B")).state == PRESENT


# ---------------------------------------------------------------- SoapySDRUtil parsing


@pytest.mark.parametrize("rc, out, ok, serials", [
    (0, "Found device 0\n  driver = sdrplay\n  label = SDRplay Dev0 RSP1B 240526D360\n  serial = 240526D360\n",
     True, {RSP}),
    (1, "No devices found! driver=sdrplay\n", True, set()),
    (1, "[ERROR] sdrplay_api_Open() Error: sdrplay_api_Fail\n", False, set()),
])
def test_api_find_parses_soapysdrutil(monkeypatch, rc, out, ok, serials):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, rc, out, ""))
    f = sdrplay.api_find()
    assert (f.ok, set(f.serials)) == (ok, serials)


def test_api_find_timeout_is_a_failure_not_an_empty_list(monkeypatch):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired("SoapySDRUtil", 10)
    monkeypatch.setattr(subprocess, "run", hang)
    f = sdrplay.api_find()
    assert not f.ok and "timed out" in f.detail.lower()


def test_sdrplay_sysfs_listing_reads_bootloader_and_firmware_states(usb):
    usb.rsp_bootloader()
    [d] = sdrplay.list_sdrplay_devices(usb.root)
    assert (d.product_id, d.serial, d.devnum) == ("3050", "", "10")
    usb.rsp_firmware()
    assert sdrplay.list_sdrplay_devices(usb.root)[0].serial == RSP
