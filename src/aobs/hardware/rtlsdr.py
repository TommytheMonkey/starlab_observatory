"""RTL-SDR inventory without claiming devices.

Presence comes from sysfs only (no USB I/O). The librtlsdr index map uses
rtlsdr_get_device_usb_strings, which opens the device for descriptor reads but
never claims the interface - the same call dump1090 and the recorder's
--check-install already make while other receivers are streaming.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import pathlib
from dataclasses import dataclass

RTL_IDS = {("0bda", "2838"), ("0bda", "2832")}
SYSFS_USB = pathlib.Path("/sys/bus/usb/devices")


@dataclass(frozen=True)
class UsbRtl:
    serial: str
    product: str
    manufacturer: str
    busnum: int
    devnum: int
    sysfs_path: str

    @property
    def dev_node(self) -> str:
        return f"/dev/bus/usb/{self.busnum:03d}/{self.devnum:03d}"


def _read(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def list_rtl_devices(sysfs: pathlib.Path = SYSFS_USB) -> list[UsbRtl]:
    devices = []
    for entry in sorted(sysfs.glob("*")):
        if (_read(entry / "idVendor"), _read(entry / "idProduct")) not in RTL_IDS:
            continue
        try:
            devices.append(UsbRtl(
                serial=_read(entry / "serial"),
                product=_read(entry / "product"),
                manufacturer=_read(entry / "manufacturer"),
                busnum=int(_read(entry / "busnum")),
                devnum=int(_read(entry / "devnum")),
                sysfs_path=str(entry),
            ))
        except ValueError:
            continue
    return devices


_INDEX_CACHE: tuple[tuple[tuple[str, int, int], ...], list[str]] | None = None


def librtlsdr_index_map() -> list[str]:
    """Serials in librtlsdr index order (what `-d N` / --device-index N resolves to).

    Cached against the sysfs device set (serial, bus, devnum): enumeration order can
    only change when devices come or go, so steady state performs no USB I/O at all."""
    global _INDEX_CACHE
    key = tuple(sorted((d.serial, d.busnum, d.devnum) for d in list_rtl_devices()))
    if _INDEX_CACHE is not None and _INDEX_CACHE[0] == key:
        return list(_INDEX_CACHE[1])
    serials = _query_index_map()
    _INDEX_CACHE = (key, serials)
    return list(serials)


def _query_index_map() -> list[str]:
    name = ctypes.util.find_library("rtlsdr")
    if not name:
        raise RuntimeError("librtlsdr not found")
    lib = ctypes.CDLL(name)
    lib.rtlsdr_get_device_count.restype = ctypes.c_uint32
    lib.rtlsdr_get_device_usb_strings.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    lib.rtlsdr_get_device_usb_strings.restype = ctypes.c_int
    serials = []
    for index in range(lib.rtlsdr_get_device_count()):
        bufs = [ctypes.create_string_buffer(256) for _ in range(3)]
        if lib.rtlsdr_get_device_usb_strings(index, *bufs) != 0:
            serials.append("")
        else:
            serials.append(bufs[2].value.decode("utf-8", errors="replace"))
    return serials


@dataclass(frozen=True)
class Holder:
    pid: int
    comm: str
    cmdline: str


def usb_holders(dev_node: str, proc: pathlib.Path = pathlib.Path("/proc")) -> tuple[list[Holder], int]:
    """Processes with dev_node open. Returns (holders, unreadable_process_count).

    Processes of other users (e.g. root dump1090) cannot be inspected without
    privileges; the count of such processes is reported so callers never treat
    "no visible holder" as proof the device is free.
    """
    holders: list[Holder] = []
    unreadable = 0
    target = os.path.realpath(dev_node)
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            fds = list((pid_dir / "fd").iterdir())
        except PermissionError:
            unreadable += 1
            continue
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(fd) == target:
                    holders.append(Holder(
                        pid=int(pid_dir.name),
                        comm=_read(pid_dir / "comm"),
                        cmdline=_read(pid_dir / "cmdline").replace("\x00", " ").strip(),
                    ))
                    break
            except OSError:
                continue
    return holders, unreadable
