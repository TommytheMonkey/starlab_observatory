"""Leases on receivers that are not RTL-SDRs (2026-11-24: the SSTV watch's lease on OBS-R10, an Airspy HF+, failed
every time with a bare "lease setup failed": the broker looked for "OBS-R10" among RTL-SDR EEPROM serials only)."""

from __future__ import annotations

import io
import json
import logging
import pathlib
import urllib.error
from typing import Any

import pytest

from aobs.config import Config
from aobs.coordination.broker import Broker, BrokerError
from test_broker import Clock, FakeHw
from test_usbports import add_device

HFPLUS_SERIAL = "3B528080C04446C7"


class SerialHw(FakeHw):
    """Records which receiver and serial the broker asks about; can fail like the old RTL-only lookup."""

    def __init__(self, fail: str | None = None) -> None:
        super().__init__()
        self.asked: list[tuple[str, str | None]] = []
        self.fail = fail

    def device_busy(self, receiver: str, serial: str | None = None) -> list[str]:
        self.asked.append((receiver, serial))
        if self.fail:
            raise BrokerError(self.fail, 503)
        return []


@pytest.fixture
def broker(tmp_config: Config) -> tuple[Broker, SerialHw]:
    tmp_config.raw["antenna_switch"].update(receiver="OBS-R09", ports=[{"port": 2, "code": 1, "antenna": "DISCONE"}])
    tmp_config.raw["broker"] = {"receivers": ["OBS-R09", "OBS-R10"]}
    hw = SerialHw()
    b = Broker(tmp_config, hw, Clock())
    b.receiver_serial = lambda rid: {"OBS-R10": HFPLUS_SERIAL}.get(rid)
    return b, hw


def test_a_receiver_without_a_switch_port_leases_by_its_hardware_serial(broker):
    b, hw = broker
    lease = b.lease("sstv-watch", "SSTV watch", 120, "OBS-R10")          # no port: the switch is not touched
    assert hw.asked == [("OBS-R10", HFPLUS_SERIAL)]
    assert hw.calls == [] and lease["port"] is None and lease["prior_port"] is None
    b.release(lease["token"])
    assert hw.calls == []                                              # nothing to restore either


def test_a_failed_lease_setup_logs_and_returns_why(broker, caplog):
    b, hw = broker
    hw.fail = "OBS-R10 is not on USB (looked for serial 3B528080C04446C7)"
    with caplog.at_level(logging.INFO, logger="aobs.broker"), pytest.raises(BrokerError, match="not on USB") as e:
        b.lease("sstv-watch", "SSTV watch", 120, "OBS-R10")
    assert e.value.status == 503
    released = [r for r in caplog.records if r.getMessage() == "lease released"]
    reason = getattr(released[-1], "reason", None) or json.dumps(released[-1].__dict__, default=str)
    assert "lease setup failed: OBS-R10 is not on USB (looked for serial 3B528080C04446C7)" in reason
    assert "OBS-R10" not in b._leases                                  # nothing left half-granted


# ------------------------------------------------------------------ the live USB lookup
@pytest.fixture
def sysfs(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    from aobs.hardware import rtlsdr, usbports
    root = tmp_path / "usb"
    add_device(root, "usb3", "1d6b", "0002", maxchild=10)
    add_device(root, "3-10", "0bda", "5411", product="USB2 hub", maxchild=4)
    add_device(root, "3-10.2", "03eb", "800c", serial=f"AIRSPYHF SN:{HFPLUS_SERIAL}", product="AIRSPY HF+", devnum=60)
    add_device(root, "3-10.3", "0bda", "2838", serial="OBS-R09", product="RTL2838UHIDIR", devnum=61)
    monkeypatch.setattr(usbports, "SYSFS_USB", root)
    real = rtlsdr.list_rtl_devices
    monkeypatch.setattr(rtlsdr, "list_rtl_devices", lambda sysfs=root: real(sysfs))
    return root


def live(tmp_config: Config) -> Any:
    from aobs.coordination.hardware import LiveHardware
    hw = LiveHardware.__new__(LiveHardware)
    hw.config, hw.adsb_unit = tmp_config, "observatory-adsb.service"
    return hw


def test_the_live_lookup_finds_an_airspy_hf_plus_by_its_serial_without_the_usb_prefix(sysfs, tmp_config, monkeypatch):
    from aobs.hardware import rtlsdr
    seen: list[str] = []
    monkeypatch.setattr(rtlsdr, "usb_holders", lambda node: (seen.append(node), ([], 0))[1])
    hw = live(tmp_config)
    assert hw.device_busy("OBS-R10", HFPLUS_SERIAL) == []
    assert seen == ["/dev/bus/usb/003/060"]
    assert hw.device_busy("OBS-R09") == [] and seen[-1] == "/dev/bus/usb/003/061"     # RTL-SDRs as before
    with pytest.raises(BrokerError, match=r"OBS-R10 is not on USB \(looked for serial OBS-R10\)"):
        hw.device_busy("OBS-R10")                     # the old call: no serial for a non-RTL receiver


def test_the_lease_client_shows_the_brokers_reason(monkeypatch):
    from aobs.piratewatch.lease import BrokerClient, LeaseError

    def refuse(req: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {},
                                     io.BytesIO(b'{"error": "OBS-R10 is not on USB (looked for serial OBS-R10)"}'))
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    with pytest.raises(LeaseError, match=r"HTTP 503 OBS-R10 is not on USB") as e:
        BrokerClient("http://127.0.0.1:1", "t").call("POST", "/v1/lease", {})
    assert e.value.status == 503
