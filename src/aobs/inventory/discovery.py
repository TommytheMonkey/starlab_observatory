"""Passive hardware discovery: what is on USB, what it is, and whether something is using it.

Nothing here opens a device. Identity comes from sysfs (vendor, product, serial strings the
kernel already read at enumeration); "in use" comes from sysfs too (a libusb program that is
streaming has claimed an interface, which the kernel shows as the `usbfs` interface driver) and
from /proc (processes holding the device node). Discovery can therefore run while recordings
are in progress without disturbing them. Opening a device to ask its driver for capabilities is
a separate, explicit step (probe.py) with its own safety checks.
"""

from __future__ import annotations

import functools
import os
import pathlib
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..hardware.rtlsdr import SYSFS_USB, _read, usb_holders
from .drivers import DRIVERS, USB_MODELS, UsbModel, canonical_serial

SDR_VENDORS = {"0bda", "1df7", "1d50", "03eb", "0456", "0403", "2500"}


@dataclass(frozen=True)
class UsbDevice:
    usb_path: str                    # sysfs port path, e.g. "5-4" (stable while it stays in the same port)
    vid: str
    pid: str
    manufacturer: str
    product: str
    usb_serial: str
    release: str                     # bcdDevice: the USB device release number the firmware reports
    busnum: int
    devnum: int
    interface_drivers: tuple[str, ...] = ()

    @property
    def dev_node(self) -> str:
        return f"/dev/bus/usb/{self.busnum:03d}/{self.devnum:03d}"

    @property
    def model(self) -> UsbModel | None:
        return USB_MODELS.get((self.vid, self.pid))

    @property
    def serial(self) -> str:
        return canonical_serial(self.model, self.usb_serial)


def scan_usb(sysfs: pathlib.Path = SYSFS_USB) -> list[UsbDevice]:
    """Known receiver models, plus anything else from an SDR vendor that we do not recognize."""
    out = []
    for e in sorted(sysfs.glob("*")):
        if ":" in e.name:
            continue                 # interfaces, not devices
        vid, pid = _read(e / "idVendor"), _read(e / "idProduct")
        if (vid, pid) not in USB_MODELS and not (vid == "1df7"):
            continue
        try:
            busnum, devnum = int(_read(e / "busnum")), int(_read(e / "devnum"))
        except ValueError:
            continue
        drivers = tuple(sorted({os.path.basename(os.readlink(i / "driver")) for i in e.glob(f"{e.name}:*")
                                if (i / "driver").is_symlink()}))
        out.append(UsbDevice(e.name, vid, pid, _read(e / "manufacturer"), _read(e / "product"), _read(e / "serial"),
                             _read(e / "bcdDevice"), busnum, devnum, drivers))
    return out


def find_device(serial: str, sysfs: pathlib.Path = SYSFS_USB, usb_path: str | None = None) -> UsbDevice | None:
    """By USB port when one is given (receivers without a usable serial), never falling back to another port;
    otherwise by serial."""
    for d in scan_usb(sysfs):
        if usb_path:
            if d.usb_path == usb_path:
                return d
        elif serial and d.serial == serial:
            return d
    return None


def receiver_like(devices: Iterable[UsbDevice]) -> list[UsbDevice]:
    """The filter scan_usb applies, for device lists read some other way (snapshot.py)."""
    return [d for d in devices if (d.vid, d.pid) in USB_MODELS or d.vid == "1df7"]


def usage(dev: UsbDevice, proc: pathlib.Path = pathlib.Path("/proc"),
          holders: Callable[[str, pathlib.Path], tuple[list[Any], int]] = usb_holders, mine: bool = True
          ) -> dict[str, Any]:
    """Who is using the device. `free` is True only when nothing visible uses it."""
    claimed = "usbfs" in dev.interface_drivers
    try:
        found, unreadable = holders(dev.dev_node, proc)
    except OSError:
        found, unreadable = [], 0
    me = os.getpid() if mine else -1
    names = [f"{h.comm}[{h.pid}]" for h in found if h.pid != me]
    sdrplay_api = dev.vid == "1df7"
    if sdrplay_api:
        detail = "held by the SDRplay API service (normal); a recording is detected from its output files"
    elif claimed and names:
        detail = f"in use by {', '.join(names)}"
    elif claimed:
        detail = ("in use: a program has claimed its USB interface" +
                  (f" (not visible to this user; {unreadable} processes could not be inspected)" if unreadable else ""))
    elif names:
        detail = f"open in {', '.join(names)}"
    else:
        detail = "not in use"
    return {"free": not sdrplay_api and not claimed and not names, "claimed": claimed, "holders": names,
            "unreadable_processes": unreadable, "managed_by_api": sdrplay_api, "detail": detail}


@functools.lru_cache(maxsize=1)
def _soapy_modules_cached(bucket: int) -> frozenset[str] | None:
    try:
        r = subprocess.run(["SoapySDRUtil", "--info"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_soapy_info(r.stdout + r.stderr)


def soapy_modules() -> frozenset[str] | None:
    """SoapySDR modules installed on this host (`SoapySDRUtil --info`; loads modules, opens no device)."""
    return _soapy_modules_cached(int(time.time() // 300))


def parse_soapy_info(text: str) -> frozenset[str] | None:
    m = re.search(r"Available factories\.*\s*(.+)", text)
    if not m:
        return None
    return frozenset(x.strip().lower() for x in m.group(1).split(",") if x.strip())


@dataclass
class Support:
    status: str                      # supported | driver_missing | unsupported | not_a_receiver | unknown_model
    detail: str
    driver: str | None = None
    soapy: str | None = None


def support(dev: UsbDevice, modules: frozenset[str] | None, which: Callable[[str], str | None] = shutil.which) -> Support:
    m = dev.model
    if m is None:
        return Support("unknown_model", f"USB {dev.vid}:{dev.pid} ({dev.manufacturer} {dev.product}) is not a model "
                                        "Starlab knows")
    if m.kind != "sdr":
        return Support("not_a_receiver", f"{m.model} is {m.note or 'not an IQ receiver'}")
    if m.driver is None:
        return Support("unsupported", f"{m.model}: {m.note or 'no capture driver yet'}", None, m.soapy)
    drv = DRIVERS[m.driver]
    missing = [t for t in drv.tools if not which(t)]
    if missing:
        return Support("driver_missing", f"needs {', '.join(missing)} installed", m.driver, m.soapy)
    if m.soapy:
        if modules is None:
            return Support("driver_missing", "SoapySDR is not installed or SoapySDRUtil failed", m.driver, m.soapy)
        if m.soapy not in modules:
            return Support("driver_missing", f"the SoapySDR module for {m.soapy} is not installed "
                                             f"(SoapySDRUtil --info does not list it)", m.driver, m.soapy)
    return Support("supported", f"{drv.label}", m.driver, m.soapy)


@dataclass
class Discovered:
    device: UsbDevice
    support: Support
    usage: dict[str, Any]
    configured_as: str | None = None
    warnings: list[str] = field(default_factory=list)
    host: str = "local"

    def as_dict(self) -> dict[str, Any]:
        d, m = self.device, self.device.model
        return {"usb_path": d.usb_path, "usb_id": f"{d.vid}:{d.pid}", "vid": d.vid, "pid": d.pid,
                "simulated": self.support.driver == "sim",
                "manufacturer": d.manufacturer or (m.manufacturer if m else ""), "product": d.product,
                "model": m.model if m else f"{d.manufacturer} {d.product}".strip() or "unknown device",
                "kind": m.kind if m else "unknown", "serial": d.serial, "usb_serial": d.usb_serial,
                "usb_release": d.release, "driver": self.support.driver, "soapy_driver": self.support.soapy,
                "support": {"status": self.support.status, "detail": self.support.detail},
                "needs_probe": bool(self.support.driver and DRIVERS[self.support.driver].needs_probe),
                "usage": self.usage, "configured_as": self.configured_as, "host": self.host,
                "connection": "usb" if self.host == "local" else "usb, over ssh",
                "dev_node": d.dev_node if d.vid != "sim" else None, "warnings": self.warnings}


def simulated_devices() -> list[UsbDevice]:
    from .drivers import SIM_DEVICES
    return [UsbDevice(path, "sim", "0001", "Starlab simulator", "Simulated receiver", serial, "0000", 0, 0)
            for path, serial in SIM_DEVICES]


def discover(receivers: Iterable[Any] = (), sysfs: pathlib.Path = SYSFS_USB, modules: frozenset[str] | None = None,
             which: Callable[[str], str | None] = shutil.which, proc: pathlib.Path = pathlib.Path("/proc"),
             holders: Callable[[str, pathlib.Path], tuple[list[Any], int]] = usb_holders,
             use_modules: bool = True, simulation: bool = False) -> list[Discovered]:
    """Every receiver-like USB device, matched to configured receivers by hardware serial (or USB port).
    With simulation on, the simulated devices are listed too (never mixed up with USB hardware)."""
    devices = scan_usb(sysfs) + (simulated_devices() if simulation else [])
    if modules is None and use_modules:
        modules = soapy_modules()
    return classify(devices, receivers, modules, which, proc, holders)


def discover_snapshot(snap: Any, receivers: Iterable[Any] = (), simulation: bool = False) -> list[Discovered]:
    """The same classification for a snapshot.Snapshot, taken on this computer or on a remote host."""
    devices = receiver_like(snap.devices) + (simulated_devices() if simulation and snap.host == "local" else [])
    return classify(devices, [r for r in receivers if getattr(r, "host", "local") == snap.host], snap.soapy_modules,
                    snap.which, None, snap.holders_of, host=snap.host)


def classify(devices: list[UsbDevice], receivers: Iterable[Any], modules: frozenset[str] | None,
             which: Callable[[str], str | None], proc: pathlib.Path | None,
             holders: Callable[..., tuple[list[Any], int]], host: str = "local") -> list[Discovered]:
    rx = list(receivers)
    out = []
    serial_count: dict[str, int] = {}
    for d in devices:
        if d.serial:
            serial_count[d.serial] = serial_count.get(d.serial, 0) + 1
    for d in devices:
        sup = support(d, modules, which)
        item = Discovered(d, sup, usage(d, proc or pathlib.Path("/proc"), holders, mine=host == "local")
                          if d.vid != "sim" else
                          {"free": True, "claimed": False, "holders": [], "unreadable_processes": 0,
                           "managed_by_api": False, "detail": "simulated device: nothing to open"}, host=host)
        for r in rx:
            same_port = getattr(r, "identity", "serial") == "usb_path" and getattr(r, "usb_path", None) == d.usb_path
            if same_port or (d.serial and r.serial == d.serial and (sup.driver in (None, r.driver) or r.driver == "soapy")):
                item.configured_as = r.id
                break
        if not d.serial:
            if d.vid == "1df7":
                item.warnings.append("no serial yet: an SDRplay device shows its serial only after the SDRplay API "
                                     "service has loaded its firmware")
            else:
                item.warnings.append("this device reports no serial number; it can only be identified by the USB port "
                                     f"it is plugged into ({d.usb_path}), so keep it in that port")
        elif serial_count[d.serial] > 1:
            fix = " (give each a unique serial with rtl_eeprom -s)" if sup.driver == "rtlsdr" else ""
            item.warnings.append(f"{serial_count[d.serial]} devices report serial {d.serial}; Starlab cannot tell them "
                                 f"apart by serial{fix}")
        out.append(item)
    return out
