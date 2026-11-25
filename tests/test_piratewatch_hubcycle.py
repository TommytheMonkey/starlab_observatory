"""Wedged-receiver recovery on a power-switchable hub: uhubctl cycles that one port instead of a USB reset.

Fake sysfs tree and a fake uhubctl runner (which re-enumerates devices in the tree); nothing touches USB."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any

import pytest

from aobs.hardware import usbports
from aobs.piratewatch.config import Settings
from aobs.piratewatch.daemon import NoData, Watch, read_control
from aobs.piratewatch.lease import LeaseKeeper
from aobs.piratewatch.radio import HubPorts
from aobs.piratewatch.store import Store
from aobs.piratewatch.transcribe import Transcriber, WhisperProcess

from test_piratewatch import FakeBroker
from test_usbports import UHUBCTL_3_8, add_device, make_sysfs, remove_device

UTC = dt.timezone.utc


class Rig:
    """The station's USB tree plus a uhubctl that power-cycles a port of it: the device drops off and comes back
    with a new device number (or with another serial, or not at all, or keeps its power)."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = make_sysfs(root)
        self.calls: list[list[str]] = []
        self.resets: list[str] = []
        self.listing_rc, self.listing_out = 0, UHUBCTL_3_8
        self.cycle_rc, self.cycle_out = 0, "Sent power off request\nSent power on request\n"
        self.comes_back_as: str | None = "same"      # "same" | None (gone) | another serial
        self.cuts_power = True

    def runner(self, argv: list[str], timeout: float) -> tuple[int, str]:
        self.calls.append(argv)
        if "-a" not in argv:
            return self.listing_rc, self.listing_out
        if self.cycle_rc:
            return self.cycle_rc, self.cycle_out
        hub, port = argv[argv.index("-l") + 1], argv[argv.index("-p") + 1]
        path = f"{hub}.{port}"
        if self.cuts_power and (self.root / path).exists():
            old = {k: (self.root / path / k).read_text().strip() for k in ("idVendor", "idProduct", "serial", "product")}
            remove_device(self.root, path)
            if self.comes_back_as is not None:
                serial = old["serial"] if self.comes_back_as == "same" else self.comes_back_as
                add_device(self.root, path, old["idVendor"], old["idProduct"], serial=serial, product=old["product"])
        return 0, self.cycle_out

    def where(self, serial: str) -> str | None:
        loc = usbports.locate(serial, self.root)
        return loc.path if loc.connected else None

    def reset(self, port: str) -> str:
        self.resets.append(port)
        return "/dev/bus/usb/003/041"

    @property
    def cycles(self) -> list[list[str]]:
        return [c for c in self.calls if "-a" in c]


@pytest.fixture
def make(tmp_path: pathlib.Path) -> Any:
    def build(receiver: str = "OBS-R05", ttl_room: float | None = None, **kw: Any) -> Any:
        rig = Rig(tmp_path / "sys")
        s = Settings(receiver=receiver, data_dir=str(tmp_path / "d"), tool_prefix=str(tmp_path), usb_reenumerate_s=0.2,
                     **kw)
        fb = FakeBroker()
        fb.ttl_room = ttl_room
        keeper = LeaseKeeper(fb, s.receiver, s.lease_owner, None, 120, renew_every_s=3600)  # type: ignore[arg-type]
        alerts: list[tuple[str, dict]] = []
        clock = {"now": dt.datetime(2026, 11, 14, 15, 0, tzinfo=UTC)}
        hub = HubPorts(s, sysfs=rig.root, uhubctl=usbports.Uhubctl(s.uhubctl, rig.runner))
        w = Watch(s, keeper=keeper, device_index=lambda _s: 0, holders=lambda _s: [], bias_forced=lambda _i: False,
                  bias_off=lambda _s: None, usb_port=rig.where, usb_reset=rig.reset, hub=hub,
                  alert=lambda text, data: alerts.append((text, data)), clock=lambda: clock["now"],
                  transcriber=Transcriber(s, Store(tmp_path / "x.sqlite3"), worker=WhisperProcess(s, ["true"])))
        w.wait = lambda _s: None

        def no_data(_index: int) -> Any:
            raise NoData(f"rtl_power gave no data for 19 s ({s.receiver} dropped off USB?)")
        w.sweep = no_data
        return w, rig, fb, alerts, clock
    return build


def logged(w: Watch) -> list[dict]:
    try:
        return [json.loads(line) for line in w.reset_log_path().read_text().splitlines()]
    except OSError:
        return []


def three(w: Watch) -> None:
    for _ in range(3):
        w.cycle()


def test_on_a_switchable_hub_port_uhubctl_cycles_only_that_port(make: Any) -> None:
    w, rig, fb, alerts, _ = make()
    devnum_before = usbports.locate("OBS-R05", rig.root).devnum
    w.cycle()
    w.cycle()
    assert rig.calls == []                                       # nothing before the third no-data failure
    w.cycle()
    assert rig.cycles == [["uhubctl", "-l", "3-8", "-p", "1", "-a", "cycle", "-d", "3"]]
    assert ["uhubctl", "-l", "3-8"] in rig.calls                 # asked uhubctl about that hub first
    assert rig.resets == []                                      # no USB reset
    loc = usbports.locate("OBS-R05", rig.root)
    assert loc.path == "3-8.1" and loc.devnum != devnum_before   # re-enumerated
    for other in ("OBS-R01", "OBS-R07", "009a"):                 # the rest of the hub untouched
        assert usbports.locate(other, rig.root).connected
    [e] = logged(w)
    assert e["method"] == "uhubctl" and e["ok"] is True and e["port"] == "3-8.1" and e["hub"] == "3-8"
    assert e["hub_port"] == 1 and e["hub_id"] == "0bda:5411" and e["back_on_port"] == "3-8.1" and e["delay_s"] == 3
    assert e["reset_this_hour"] == 1 and e["no_data_in_a_row"] == 3
    assert w._no_data == 0 and alerts == [] and not read_control(w.s).get("usb_halted")
    assert ("DELETE", "/v1/lease/tok") in fb.calls


def test_a_receiver_on_the_nested_hub_cycles_the_nested_hubs_port(make: Any) -> None:
    w, rig, *_ = make(receiver="OBS-R07", hub_cycle_delay_s=4.5)
    three(w)
    assert rig.cycles == [["uhubctl", "-l", "3-8.4", "-p", "4", "-a", "cycle", "-d", "4.5"]]
    assert usbports.locate("OBS-R05", rig.root).connected and usbports.locate("OBS-R01", rig.root).connected


def test_off_the_switchable_hub_the_usb_reset_is_used(make: Any) -> None:
    w, rig, _, alerts, _ = make(receiver="RSP1B-0001")           # on the ASM107x hub (174c:2074): never cycled
    three(w)
    assert rig.calls == [] and rig.resets == ["3-10.1"]
    [e] = logged(w)
    assert e["method"] == "usbdevfs_reset" and e["ok"] is True and e["port"] == "3-10.1" and "uhubctl_error" not in e
    assert alerts == []


def test_a_root_hub_port_gets_the_usb_reset(make: Any) -> None:
    w, rig, *_ = make(receiver="OBS-R02")
    three(w)
    assert rig.calls == [] and rig.resets == ["3-5"]


def test_a_configured_unswitchable_port_gets_the_usb_reset(make: Any) -> None:
    w, rig, *_ = make(hub_unswitchable_ports=["0bda:5411:1"])
    three(w)
    assert rig.cycles == [] and rig.resets == ["3-8.1"]


def test_no_uhubctl_listing_means_no_cycle(make: Any) -> None:
    w, rig, *_ = make()
    rig.listing_rc, rig.listing_out = 1, "No compatible devices detected!\n"
    three(w)
    assert rig.cycles == [] and rig.resets == ["3-8.1"]
    assert logged(w)[0]["method"] == "usbdevfs_reset"


def test_power_cycling_can_be_turned_off(make: Any) -> None:
    w, rig, *_ = make(hub_power_cycle=False)
    three(w)
    assert rig.calls == [] and rig.resets == ["3-8.1"]


def test_a_failing_uhubctl_falls_back_to_one_usb_reset(make: Any) -> None:
    w, rig, _, alerts, _ = make()
    rig.cycle_rc, rig.cycle_out = 1, "Permission denied. Try running as root!\n"
    three(w)
    assert len(rig.cycles) == 1 and rig.resets == ["3-8.1"]
    [e] = logged(w)                                              # one recovery, one entry, one unit of the budget
    assert e["method"] == "usbdevfs_reset" and e["fallback_from"] == "uhubctl" and "Permission denied" in e["uhubctl_error"]
    assert e["ok"] is True and e["hub"] == "3-8" and e["hub_port"] == 1 and alerts == []


def test_a_cycle_that_did_not_cut_power_falls_back_to_a_usb_reset(make: Any) -> None:
    w, rig, _, alerts, _ = make()
    rig.cuts_power = False                                       # the HUB7BC's rightmost port
    three(w)
    assert len(rig.cycles) == 1 and rig.resets == ["3-8.1"]
    [e] = logged(w)
    assert e["method"] == "usbdevfs_reset" and "power was not cut" in e["uhubctl_error"] and alerts == []


def test_three_recoveries_an_hour_across_both_methods_then_halt_and_alert(make: Any) -> None:
    w, rig, _, alerts, clock = make()
    start = clock["now"]
    w.reset_log_path().parent.mkdir(parents=True, exist_ok=True)
    w.log_reset({"at": (start - dt.timedelta(minutes=30)).isoformat(), "method": "usbdevfs_reset", "ok": True})
    w.log_reset({"at": (start - dt.timedelta(minutes=20)).isoformat(), "method": "uhubctl", "ok": True})
    three(w)
    assert len(rig.cycles) == 1 and alerts == []                 # the third of the hour
    clock["now"] = start + dt.timedelta(minutes=5)
    three(w)
    assert len(rig.cycles) == 1 and rig.resets == []             # no fourth, by either method
    [(text, data)] = alerts
    assert "Pirate watch stopped" in text and "after 3 USB resets in the last hour" in text
    assert [e.get("method") for e in data["resets_last_hour"]] == ["usbdevfs_reset", "uhubctl", "uhubctl"]
    assert read_control(w.s)["usb_halted"] and w.state == "blocked"


def test_another_serial_coming_back_on_the_port_is_a_failed_recovery(make: Any) -> None:
    w, rig, _, alerts, _ = make()
    rig.comes_back_as = "OBS-R99"
    three(w)
    assert len(rig.cycles) == 1 and rig.resets == []
    [e] = logged(w)
    assert e["method"] == "uhubctl" and e["ok"] is False and e["found_on_port"] == "OBS-R99"
    [(text, data)] = alerts
    assert "OBS-R99 came back on 3-8.1 instead of OBS-R05" in text and data["usb_halted"]
    assert read_control(w.s)["usb_halted"]


def test_a_receiver_that_does_not_come_back_after_a_cycle_halts_and_alerts(make: Any) -> None:
    w, rig, _, alerts, _ = make()
    rig.comes_back_as = None
    three(w)
    [(text, _)] = alerts
    assert "did not come back on USB" in text and "power cycle of hub 3-8 (0bda:5411) port 1" in text
    assert logged(w)[0]["ok"] is False


def test_a_duplicate_serial_after_the_cycle_halts_and_alerts(make: Any) -> None:
    w, rig, _, alerts, _ = make()
    real = rig.runner

    def runner(argv: list[str], timeout: float) -> tuple[int, str]:
        out = real(argv, timeout)
        if "-a" in argv:                                         # a second OBS-R05 shows up during the cycle
            add_device(rig.root, "3-8.4.1", "0bda", "2838", serial="OBS-R05")
        return out
    w.hub.uhubctl.runner = runner
    three(w)
    assert len(rig.cycles) == 1 and rig.resets == []
    [(text, _)] = alerts
    assert "OBS-R05 came back on more than one port (3-8.1, 3-8.4.1)" in text
    assert logged(w)[0]["found_status"] == "duplicate"


def test_a_duplicate_serial_is_never_cycled(make: Any) -> None:
    w, rig, *_ = make()
    add_device(rig.root, "3-8.4.1", "0bda", "2838", serial="OBS-R05")
    three(w)
    assert rig.calls == []


def test_an_upcoming_reservation_puts_the_cycle_off(make: Any) -> None:
    w, rig, fb, alerts, _ = make(ttl_room=60)                    # the broker caps the lease: a recording in ~90 s
    three(w)
    assert rig.cycles == [] and rig.resets == []                 # neither a cycle nor a reset
    assert logged(w) == [] and alerts == [] and not read_control(w.s).get("usb_halted")
    assert w.state == "waiting" and "put off" in w.detail and "starts in about 90 s" in w.detail
    assert ("POST", "/v1/lease/tok/renew") in fb.calls           # asked the broker for room first
    fb.ttl_room = None                                           # after the recording: recovered on the next failure
    w.cycle()
    assert len(rig.cycles) == 1


def test_a_refused_lease_extension_puts_the_cycle_off(make: Any) -> None:
    w, rig, fb, alerts, _ = make()
    from aobs.piratewatch.lease import LeaseError
    orig = fb.call

    def call(method: str, path: str, body: Any = None) -> Any:
        if path.endswith("/renew") and body and body["ttl_s"] > 120:
            raise LeaseError("OBS-R05 is reserved for a scheduled recording starting 2026-11-14T15:01:00Z", 409)
        return orig(method, path, body)
    fb.call = call
    three(w)
    assert rig.cycles == [] and rig.resets == [] and alerts == []
    assert "reserved for a scheduled recording" in w.detail


def test_a_lost_lease_puts_the_cycle_off(make: Any) -> None:
    w, rig, *_ = make()
    real = w.keeper.room

    def lose(seconds: float) -> float:
        w.keeper._lose("scheduled job j1 needs OBS-R05")
        return real(seconds)
    w.keeper.room = lose                                         # type: ignore[method-assign]
    three(w)
    assert rig.cycles == [] and rig.resets == []
    assert "j1" in w.detail


def test_the_status_counts_both_methods(make: Any) -> None:
    w, *_ = make()
    three(w)
    assert json.loads(w.s.path("status.json").read_text())["usb_resets_last_hour"] == 1


def test_default_hub_ports_never_run_uhubctl_in_tests(tmp_path: pathlib.Path) -> None:
    s = Settings(data_dir=str(tmp_path / "d"))
    hub = HubPorts(s, sysfs=make_sysfs(tmp_path / "sys"))
    ok, why = hub.switchable(hub.locate("OBS-R05"))
    assert not ok and "not running uhubctl" in why
    with pytest.raises(usbports.UhubctlError):
        hub.cycle("3-8", 1)


def test_a_stale_sysfs_entry_is_not_taken_for_the_receiver_coming_back(make: Any) -> None:
    """Live 2026-11-14 (uhubctl through libusb): the kernel kept the device in sysfs while the port was off, and it
    re-enumerated with a new device number only after power-on. Success needs that new enumeration."""
    w, rig, _, alerts, _ = make()
    rig.cuts_power = False                                       # the cycle itself leaves sysfs as it was...
    before = usbports.locate("OBS-R05", rig.root).devnum
    waits = {"n": 0, "seen_stale": 0}

    def wait(_s: float) -> None:
        if rig.cycles:
            waits["n"] += 1
            if usbports.locate("OBS-R05", rig.root).devnum == before:
                waits["seen_stale"] += 1
            if waits["n"] == 3:                                  # ...re-enumerated a moment after power-on
                remove_device(rig.root, "3-8.1")
                add_device(rig.root, "3-8.1", "0bda", "2838", serial="OBS-R05", product="NESDR SMArt v5")
    w.wait = wait
    three(w)
    assert len(rig.cycles) == 1 and rig.resets == [] and alerts == []
    [e] = logged(w)
    assert e["method"] == "uhubctl" and e["ok"] is True and e["back_on_port"] == "3-8.1"
    assert waits["seen_stale"] >= 2                              # it polled past the stale entry
    assert usbports.locate("OBS-R05", rig.root).devnum != before
