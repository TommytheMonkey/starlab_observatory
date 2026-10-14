"""SDRplay inventory without opening a device.

An RSP powers up in its bootloader: vendor 1df7 on USB but no serial string. The
SDRplay API service loads the firmware the first time a client enumerates it, and
the device re-enumerates with its serial. A missing sysfs serial is therefore not
proof the receiver is gone; the API's device list (SoapySDRUtil --find, which
never selects or streams the device) settles which serial it is.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
from dataclasses import dataclass

from .rtlsdr import SYSFS_USB, _read

SDRPLAY_VENDOR = "1df7"


@dataclass(frozen=True)
class UsbSdrplay:
    product_id: str
    serial: str                      # "" while the bootloader runs (no firmware yet)
    sysfs_path: str
    devnum: str                      # changes on every (re-)enumeration


@dataclass(frozen=True)
class ApiFind:
    ok: bool                         # the API answered (an empty answer is still an answer)
    serials: frozenset[str]
    detail: str


def list_sdrplay_devices(sysfs: pathlib.Path = SYSFS_USB) -> list[UsbSdrplay]:
    return [UsbSdrplay(_read(e / "idProduct"), _read(e / "serial"), str(e), _read(e / "devnum"))
            for e in sorted(sysfs.glob("*")) if _read(e / "idVendor") == SDRPLAY_VENDOR]


def api_find(timeout: float = 10) -> ApiFind:
    """Serials the SDRplay API service lists. Exit 1 with 'No devices found' is a valid empty list."""
    try:
        r = subprocess.run(["SoapySDRUtil", "--find=driver=sdrplay"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ApiFind(False, frozenset(), f"SoapySDRUtil failed: {exc}")
    txt = (r.stdout + r.stderr).strip()
    serials = frozenset(re.findall(r"^\s*serial\s*=\s*(\S+)\s*$", txt, re.M))
    if serials or "No devices found" in txt:
        return ApiFind(True, serials, f"SoapySDRPlay3 lists {', '.join(sorted(serials)) or 'no device'}")
    return ApiFind(False, frozenset(), f"SoapySDRUtil exit {r.returncode}: {txt[-160:]}")
