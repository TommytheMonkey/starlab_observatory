"""USB port lookup by serial and the power-switchability verdict, on a fake sysfs tree (no USB I/O, no uhubctl)."""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from aobs.hardware import usbports
from aobs.hardware.usbports import HubPolicy, PortLocation, Uhubctl, UhubctlError

# The station on 2026-11-14: Plugable USBC-HUB7BC (RTS5411, two USB2 tiers) on 3-8, the ASM107x hub on 3-10.
STATION = {
    "usb3": dict(vid="1d6b", pid="0002", product="xHCI Host Controller", maxchild=10),
    "3-8": dict(vid="0bda", pid="5411", product="USB2.1 Hub", maxchild=4),
    "3-8.1": dict(vid="0bda", pid="2838", serial="OBS-R05", product="NESDR SMArt v5"),
    "3-8.2": dict(vid="0bda", pid="2838", serial="OBS-R01", product="RTL2838UHIDIR"),
    "3-8.3": dict(vid="1d50", pid="605b", serial="009a", product="YARD Stick One"),
    "3-8.4": dict(vid="0bda", pid="5411", product="USB2.1 Hub", maxchild=4),
    "3-8.4.4": dict(vid="0bda", pid="2838", serial="OBS-R07", product="Blog V4L"),
    "3-10": dict(vid="174c", pid="2074", product="ASM107x", maxchild=4),
    "3-10.1": dict(vid="1df7", pid="3050", serial="RSP1B-0001", product="SDRplay RSP1B"),
    "3-10.2": dict(vid="1d50", pid="60a1", serial="AIRSPY-0002", product="AIRSPY"),
    "3-5": dict(vid="0bda", pid="2838", serial="OBS-R02", product="RTL2838UHIDIR"),
}

UHUBCTL_3_8 = """Current status for hub 3-8.4 [0bda:5411 Generic USB2.1 Hub, USB 2.10, 4 ports, ppps]
  Port 1: 0100 power
  Port 2: 0100 power
  Port 3: 0100 power
  Port 4: 0503 power highspeed enable connect [0bda:2838 RTLSDRBlog Blog V4L OBS-R07]
Current status for hub 3-8 [0bda:5411 Generic USB2.1 Hub, USB 2.10, 4 ports, ppps]
  Port 1: 0103 power enable connect [0bda:2838 Nooelec NESDR SMArt v5 OBS-R05]
  Port 2: 0103 power enable connect [0bda:2838 Realtek RTL2838UHIDIR OBS-R01]
  Port 3: 0103 power enable connect [1d50:605b Great Scott Gadgets YARD Stick One 009a]
  Port 4: 0503 power highspeed enable connect [0bda:5411 Generic USB2.1 Hub]
"""

_devnum = [10]


def add_device(root: pathlib.Path, name: str, vid: str, pid: str, serial: str = "", product: str = "",
               maxchild: int = 0, devnum: int | None = None) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if devnum is None:
        _devnum[0] += 1
        devnum = _devnum[0]
    files = {"idVendor": vid, "idProduct": pid, "product": product, "maxchild": str(maxchild), "devnum": str(devnum),
             "busnum": name.split("-")[0].removeprefix("usb"), "devpath": name.split("-", 1)[1] if "-" in name else "0"}
    if serial:
        files["serial"] = serial
    for k, v in files.items():
        (d / k).write_text(v + "\n")


def remove_device(root: pathlib.Path, name: str) -> None:
    d = root / name
    for f in d.iterdir():
        f.unlink()
    d.rmdir()


def make_sysfs(root: pathlib.Path, devices: dict[str, dict[str, Any]] | None = None) -> pathlib.Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, attrs in (STATION if devices is None else devices).items():
        add_device(root, name, **attrs)
    (root / "3-8:1.0").mkdir()                               # an interface: never a device
    (root / "3-8:1.0" / "bInterfaceClass").write_text("09\n")
    return root


@pytest.fixture
def sysfs(tmp_path: pathlib.Path) -> pathlib.Path:
    return make_sysfs(tmp_path / "sys")


def test_scan_lists_devices_not_interfaces_or_root_hubs(sysfs: pathlib.Path) -> None:
    names = [d.path for d in usbports.scan(sysfs)]
    assert "3-8.4.4" in names and "usb3" not in names and "3-8:1.0" not in names
    hubs = [d.path for d in usbports.scan(sysfs) if d.is_hub]
    assert hubs == ["3-10", "3-8", "3-8.4"]


def test_locate_by_serial_gives_path_hub_and_port(sysfs: pathlib.Path) -> None:
    r07 = usbports.locate("OBS-R07", sysfs)
    assert r07.connected and r07.path == "3-8.4.4" and r07.hub == "3-8.4" and r07.port == 4
    assert r07.hub_id == "0bda:5411" and r07.hub_product == "USB2.1 Hub" and r07.usb_id == "0bda:2838"
    r05 = usbports.locate("OBS-R05", sysfs)
    assert (r05.path, r05.hub, r05.port) == ("3-8.1", "3-8", 1) and not r05.root_hub
    rsp = usbports.locate("RSP1B-0001", sysfs)
    assert (rsp.hub, rsp.port, rsp.hub_id) == ("3-10", 1, "174c:2074")


def test_locate_on_a_root_hub_port(sysfs: pathlib.Path) -> None:
    r02 = usbports.locate("OBS-R02", sysfs)
    assert (r02.path, r02.hub, r02.port, r02.root_hub, r02.hub_id) == ("3-5", "usb3", 5, True, "1d6b:0002")


def test_locate_follows_a_moved_cable(sysfs: pathlib.Path) -> None:
    remove_device(sysfs, "3-8.4.4")
    add_device(sysfs, "3-8.4.2", "0bda", "2838", serial="OBS-R07")
    assert usbports.locate("OBS-R07", sysfs).path == "3-8.4.2"


def test_missing_and_duplicate_serials(sysfs: pathlib.Path) -> None:
    assert usbports.locate("OBS-R99", sysfs).status == "not_connected"
    add_device(sysfs, "3-8.4.1", "0bda", "2838", serial="OBS-R01")
    dup = usbports.locate("OBS-R01", sysfs)
    assert dup.status == "duplicate" and dup.duplicates == ("3-8.2", "3-8.4.1") and dup.path is None
    assert usbports.duplicate_serials(usbports.scan(sysfs)) == {"OBS-R01": ["3-8.2", "3-8.4.1"]}


def test_locate_by_path_for_a_receiver_without_serial(sysfs: pathlib.Path) -> None:
    loc = usbports.locate("", sysfs, path="3-8.3")
    assert loc.connected and loc.hub == "3-8" and loc.port == 3


def test_serial_at(sysfs: pathlib.Path) -> None:
    assert usbports.serial_at("3-8.1", sysfs) == "OBS-R05"
    assert usbports.serial_at("3-8.4.3", sysfs) is None


def test_a_missing_sysfs_is_empty(tmp_path: pathlib.Path) -> None:
    assert usbports.scan(tmp_path / "nope") == []
    assert usbports.locate("OBS-R05", tmp_path / "nope").status == "not_connected"


def test_parse_uhubctl_listing() -> None:
    hubs = usbports.parse_uhubctl(UHUBCTL_3_8)
    assert set(hubs) == {"3-8", "3-8.4"}
    h = hubs["3-8"]
    assert h.usb_id == "0bda:5411" and h.ppps is True and sorted(h.ports) == [1, 2, 3, 4]
    assert h.ports[1].status == "0103" and h.ports[1].flags == ["power", "enable", "connect"]
    assert "OBS-R05" in h.ports[1].attached and h.ports[1].powered
    assert hubs["3-8.4"].ports[4].flags == ["power", "highspeed", "enable", "connect"]
    g = usbports.parse_uhubctl("Current status for hub 1-1 [05e3:0608 USB2.0 Hub, USB 2.00, 4 ports, ganged]\n"
                               "  Port 1: 0100 power\n")
    assert g["1-1"].ppps is False
    assert usbports.parse_uhubctl("No compatible devices detected!\n") == {}


def test_policy_needs_an_allowlisted_hub_with_ppps_and_the_port_listed(sysfs: pathlib.Path) -> None:
    pol = HubPolicy()
    listing = usbports.parse_uhubctl(UHUBCTL_3_8)
    assert pol.check(usbports.locate("OBS-R05", sysfs), listing) == (True, "hub 3-8 (0bda:5411) port 1")
    assert pol.check(usbports.locate("OBS-R07", sysfs), listing)[0] is True
    ok, why = pol.check(usbports.locate("RSP1B-0001", sysfs), listing)          # the ASM107x hub: never
    assert not ok and "not in the power-switchable hub list" in why
    ok, why = pol.check(usbports.locate("OBS-R02", sysfs), listing)
    assert not ok and "root hub" in why
    ok, why = pol.check(usbports.locate("OBS-R99", sysfs), listing)
    assert not ok and why == "not connected"
    ok, why = pol.check(usbports.locate("OBS-R05", sysfs), None)              # no uhubctl output: conservative
    assert not ok and "unavailable" in why
    ok, why = pol.check(usbports.locate("OBS-R05", sysfs), {})
    assert not ok and "does not list hub 3-8" in why
    ganged = usbports.parse_uhubctl(UHUBCTL_3_8.replace("4 ports, ppps]\n  Port 1: 0103", "4 ports, ganged]\n  Port 1: 0103"))
    ok, why = pol.check(usbports.locate("OBS-R05", sysfs), ganged)
    assert not ok and "ganged" in why
    add_device(sysfs, "3-8.4.1", "0bda", "2838", serial="OBS-R05")
    ok, why = pol.check(usbports.locate("OBS-R05", sysfs), listing)
    assert not ok and "duplicate serial" in why


def test_unswitchable_ports_by_hub_id_or_location(sysfs: pathlib.Path) -> None:
    listing = usbports.parse_uhubctl(UHUBCTL_3_8)
    r07 = usbports.locate("OBS-R07", sysfs)
    for entry in ("0bda:5411:4", "3-8.4:4", "0BDA:5411:4"):
        ok, why = HubPolicy.from_settings(None, [entry]).check(r07, listing)
        assert not ok and "listed as not power-switchable" in why
    assert HubPolicy.from_settings(None, ["3-8:4"]).check(r07, listing)[0] is True       # another hub's port 4
    assert HubPolicy.from_settings([], []).check(r07, listing)[0] is False               # empty allowlist: none


def test_describe_flags_for_status_pages(sysfs: pathlib.Path) -> None:
    pol = HubPolicy()
    d = usbports.describe(usbports.locate("OBS-R05", sysfs), pol)
    assert d["switchable"] is True and d["flags"] == [] and d["path"] == "3-8.1"
    assert usbports.describe(usbports.locate("RSP1B-0001", sysfs), pol)["flags"] == ["port not power-switchable"]
    assert usbports.describe(usbports.locate("OBS-R99", sysfs), pol)["flags"] == ["not connected"]
    add_device(sysfs, "3-8.4.1", "0bda", "2838", serial="OBS-R05")
    assert usbports.describe(usbports.locate("OBS-R05", sysfs), pol)["flags"] == ["duplicate serial"]


def test_uhubctl_is_never_run_for_real_under_pytest() -> None:
    with pytest.raises(UhubctlError, match="not running uhubctl"):
        usbports.run_uhubctl(["uhubctl", "-l", "3-8"], 5)
    with pytest.raises(UhubctlError, match="not running uhubctl"):
        Uhubctl().cycle("3-8", 1, 3)


def test_uhubctl_commands_target_one_port() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout: float) -> tuple[int, str]:
        calls.append(argv)
        return (0, UHUBCTL_3_8) if "-a" not in argv else (0, "Sent power off request\nSent power on request\n")

    u = Uhubctl("uhubctl", runner)
    assert set(u.listing("3-8")) == {"3-8", "3-8.4"}
    u.cycle("3-8.4", 4, 3.0)
    assert calls == [["uhubctl", "-l", "3-8"], ["uhubctl", "-l", "3-8.4", "-p", "4", "-a", "cycle", "-d", "3"]]


def test_uhubctl_failures_raise() -> None:
    u = Uhubctl("uhubctl", lambda argv, t: (1, "No compatible devices detected!\nRun with -h for help\n"))
    with pytest.raises(UhubctlError, match="No compatible devices"):
        u.listing("3-8")
    u = Uhubctl("uhubctl", lambda argv, t: (1, "Permission denied. Try running as root\n"))
    with pytest.raises(UhubctlError, match="Permission denied"):
        u.cycle("3-8", 1, 3)


def test_port_location_dict_round_trips() -> None:
    d = PortLocation(serial="X", status="duplicate", duplicates=("3-1", "3-2")).as_dict()
    assert d["duplicates"] == ["3-1", "3-2"] and d["status"] == "duplicate"
