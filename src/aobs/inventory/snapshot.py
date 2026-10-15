"""One read-only hardware snapshot, taken the same way on this computer and on remote hosts.

SCRIPT is a POSIX shell script. Starlab runs it with `sh -s` here and over SSH on a remote host (the
script goes on stdin, so nothing is installed there). It reads sysfs and /proc only:

  * the USB device list (vendor, product, serial strings the kernel read at enumeration, bus and
    device numbers, and which kernel driver claims each interface: `usbfs` means a libusb program
    has the device open),
  * which processes hold a /dev/bus/usb node open (readlink of /proc/<pid>/fd/*), and how many
    processes it was not allowed to inspect,
  * which SDR tools are installed (`command -v`) and the SoapySDR modules (`SoapySDRUtil --info`,
    which loads modules and opens no device).

It never opens a device node, so it can run while recordings are streaming. parse() turns its output
into a Snapshot; inventory.discovery then classifies the devices exactly as it does for this computer.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
from typing import Any

from ..hardware.rtlsdr import Holder
from .discovery import UsbDevice, parse_soapy_info

VERSION = "1"

TOOLS = ("rtl_test", "rtl_eeprom", "rtl_sdr", "rtl_biast", "airspy_info", "airspy_rx", "hackrf_info", "SoapySDRUtil",
         "iio_info", "LimeUtil", "uhd_find_devices", "sdrpp", "bwrap", "timeout")

SCRIPT = r"""
# Starlab hardware snapshot: reads sysfs and /proc only. Opens no device. Changes nothing.
SYSFS=${1:-/sys/bus/usb/devices}
PROC=${2:-/proc}
clean() { tr -d '\t\r\n' 2>/dev/null; }
printf 'starlab-snapshot\t%s\n' "__VERSION__"
printf 'hostname\t%s\n' "$(uname -n | clean)"
printf 'kernel\t%s\n' "$(uname -r | clean)"
for d in "$SYSFS"/*; do
  n=${d##*/}
  case $n in *:*) continue ;; esac
  [ -r "$d/idVendor" ] || continue
  printf 'dev\tpath=%s' "$n"
  for a in idVendor idProduct manufacturer product serial bcdDevice busnum devnum; do
    printf '\t%s=%s' "$a" "$(cat "$d/$a" 2>/dev/null | clean)"
  done
  drv=""
  for i in "$d/$n":*; do
    [ -L "$i/driver" ] || continue
    t=$(readlink "$i/driver"); drv="$drv ${t##*/}"
  done
  printf '\tifdrivers=%s\n' "$drv"
done
for t in __TOOLS__; do
  p=$(command -v "$t" 2>/dev/null) && printf 'tool\t%s\t%s\n' "$t" "$p"
done
unreadable=0
for p in "$PROC"/[0-9]*; do
  [ -d "$p" ] || continue
  if ! ls "$p/fd" >/dev/null 2>&1; then unreadable=$((unreadable + 1)); continue; fi
  for f in "$p"/fd/*; do
    l=$(readlink "$f" 2>/dev/null) || continue
    case $l in
      /dev/bus/usb/*) printf 'holder\t%s\t%s\t%s\n' "$l" "${p##*/}" "$(cat "$p/comm" 2>/dev/null | clean)" ;;
    esac
  done
done
printf 'unreadable\t%s\n' "$unreadable"
if command -v SoapySDRUtil >/dev/null 2>&1; then
  if command -v timeout >/dev/null 2>&1; then i=$(timeout 20 SoapySDRUtil --info 2>&1); else i=$(SoapySDRUtil --info 2>&1); fi
  printf 'soapy\t%s\n' "$(printf '%s' "$i" | grep -i 'available factories' | clean)"
fi
printf 'end\n'
""".replace("__VERSION__", VERSION).replace("__TOOLS__", " ".join(TOOLS))


class SnapshotError(RuntimeError):
    def __init__(self, message: str, kind: str | None = None):
        super().__init__(message)
        self.kind = kind


@dataclasses.dataclass
class Snapshot:
    host: str                                   # Starlab host id ("local" or a registered host)
    hostname: str = ""                          # what the host calls itself (uname -n)
    kernel: str = ""
    devices: list[UsbDevice] = dataclasses.field(default_factory=list)
    holders: dict[str, list[Holder]] = dataclasses.field(default_factory=dict)
    unreadable: int = 0
    tools: dict[str, str] = dataclasses.field(default_factory=dict)
    soapy_modules: frozenset[str] | None = None
    complete: bool = False                      # the script ran to the end

    def which(self, tool: str) -> str | None:
        return self.tools.get(tool)

    def holders_of(self, node: str, _proc: Any = None) -> tuple[list[Holder], int]:
        return list(self.holders.get(node, [])), self.unreadable

    def summary(self) -> dict[str, Any]:
        return {"hostname": self.hostname, "kernel": self.kernel, "usb_devices": len(self.devices),
                "tools": dict(sorted(self.tools.items())),
                "soapy_modules": sorted(self.soapy_modules) if self.soapy_modules is not None else None,
                "unreadable_processes": self.unreadable}


def parse(text: str, host: str) -> Snapshot:
    snap = Snapshot(host)
    lines = text.splitlines()
    if not lines or not lines[0].startswith("starlab-snapshot\t"):
        raise ValueError("not a Starlab hardware snapshot (the remote shell printed something else first: "
                         f"{(lines[0] if lines else 'nothing')[:120]!r})")
    for line in lines[1:]:
        parts = line.split("\t")
        kind = parts[0]
        if kind == "hostname" and len(parts) > 1:
            snap.hostname = parts[1]
        elif kind == "kernel" and len(parts) > 1:
            snap.kernel = parts[1]
        elif kind == "dev":
            f = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
            try:
                bus, dev = int(f.get("busnum", "")), int(f.get("devnum", ""))
            except ValueError:
                continue
            snap.devices.append(UsbDevice(f.get("path", ""), f.get("idVendor", ""), f.get("idProduct", ""),
                                          f.get("manufacturer", ""), f.get("product", ""), f.get("serial", ""),
                                          f.get("bcdDevice", ""), bus, dev,
                                          tuple(sorted(set(f.get("ifdrivers", "").split())))))
        elif kind == "tool" and len(parts) > 2:
            snap.tools[parts[1]] = parts[2]
        elif kind == "holder" and len(parts) > 2 and parts[2].isdigit():
            snap.holders.setdefault(parts[1], []).append(Holder(int(parts[2]), parts[3] if len(parts) > 3 else "", ""))
        elif kind == "unreadable" and len(parts) > 1 and parts[1].isdigit():
            snap.unreadable = int(parts[1])
        elif kind == "soapy":
            snap.soapy_modules = parse_soapy_info(parts[1] if len(parts) > 1 else "")
        elif kind == "end":
            snap.complete = True
    if not snap.complete:
        raise ValueError("the hardware snapshot was cut off before the end")
    return snap


def script_args(sysfs: pathlib.Path | str | None = None, proc: pathlib.Path | str | None = None) -> list[str]:
    """Arguments for `sh -s --`: only tests point the script at another sysfs or /proc."""
    return [str(sysfs or "/sys/bus/usb/devices"), str(proc or "/proc")]


_SAFE = re.compile(r"^[A-Za-z0-9_./:-]+$")


def take(host: Any, sysfs: pathlib.Path | str | None = None, proc: pathlib.Path | str | None = None,
         timeout: float = 60) -> Snapshot:
    """Run SCRIPT on a host (inventory.hosts.LocalHost or SshHost) and parse what it printed."""
    args = script_args(sysfs, proc)
    if not all(_SAFE.match(a) for a in args):
        raise ValueError("unexpected characters in the snapshot paths")
    r = host.run(["sh", "-s", "--", *args], input=SCRIPT, timeout=timeout)
    if r.returncode != 0 and not r.stdout.startswith("starlab-snapshot"):
        raise SnapshotError(r.error or f"the snapshot script failed (exit {r.returncode}): {r.stderr.strip()[-300:]}",
                            r.error_kind or "snapshot_failed")
    try:
        return parse(r.stdout, host.id)
    except ValueError as exc:
        raise SnapshotError(str(exc), "snapshot_failed") from exc
