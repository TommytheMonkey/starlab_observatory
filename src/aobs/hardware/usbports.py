"""Where a USB device is plugged in (by serial, looked up at runtime) and whether its hub port can be power-cycled.

Paths change whenever a cable moves, so nothing here is configured by path: a device is found by its serial in
/sys/bus/usb/devices (sysfs reads only, no USB I/O), and its parent hub and port come from the sysfs name
("3-8.4.4" = port 4 of the hub at "3-8.4"; "3-8" = port 8 of root hub usb3).

Per-port power switching (uhubctl) is allowed only when every check passes, otherwise the answer is "no":
  * the device is on USB exactly once (a duplicated serial is reported, never acted on);
  * its parent is an external hub (never a root hub) whose vid:pid is in the allowlist (default 0bda:5411, the
    Realtek RTS5411 USB2 hub of the Plugable USBC-HUB7BC);
  * the port is not listed in `unswitchable_ports`;
  * uhubctl itself lists that hub with per-port power switching ("ppps", not "ganged") and that port.

uhubctl does not mark the ports it cannot really switch. On the USBC-HUB7BC the rightmost port is not
power-switchable (a known limitation of that model: uhubctl "succeeds" but the port keeps power). Which sysfs port
that is depends on the hub tier, so it has to be configured once by hand in `unswitchable_ports`, as
"<hub vid:pid>:<port>" (every such hub) or "<hub location>:<port>" (that hub while it stays on that path).
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

SYSFS_USB = pathlib.Path("/sys/bus/usb/devices")
DEFAULT_SWITCHABLE_HUBS = ("0bda:5411",)
_DEVICE_NAME = re.compile(r"^\d+-\d+(\.\d+)*$")          # "3-8.4.4"; not "usb3" (root hub) nor "3-8:1.0" (interface)


class UhubctlError(RuntimeError):
    pass


def _read(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


@dataclass(frozen=True)
class UsbDevice:
    path: str                    # sysfs name, e.g. "3-8.4.4"
    vid: str
    pid: str
    serial: str
    product: str
    manufacturer: str
    devpath: str
    maxchild: int                # > 0 for a hub
    devnum: int = 0              # changes at every enumeration: a power cycle that really cut power gives a new one

    @property
    def usb_id(self) -> str:
        return f"{self.vid}:{self.pid}"

    @property
    def is_hub(self) -> bool:
        return self.maxchild > 0


def parent_of(path: str) -> tuple[str, int, bool]:
    """("3-8.4", 4, False) for "3-8.4.4"; ("usb3", 8, True) for "3-8" (a root hub port)."""
    bus, chain = path.split("-", 1)
    if "." in chain:
        hub, port = path.rsplit(".", 1)
        return hub, int(port), False
    return f"usb{bus}", int(chain), True


def scan(sysfs: pathlib.Path | None = None) -> list[UsbDevice]:
    """Every USB device (not interfaces, not root hubs) with its identity, from sysfs."""
    root = sysfs or SYSFS_USB
    out = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    for entry in entries:
        if not _DEVICE_NAME.match(entry.name):
            continue
        try:
            maxchild = int(_read(entry / "maxchild") or 0)
        except ValueError:
            maxchild = 0
        try:
            devnum = int(_read(entry / "devnum") or 0)
        except ValueError:
            devnum = 0
        out.append(UsbDevice(path=entry.name, vid=_read(entry / "idVendor"), pid=_read(entry / "idProduct"),
                             serial=_read(entry / "serial"), product=_read(entry / "product"),
                             manufacturer=_read(entry / "manufacturer"), devpath=_read(entry / "devpath"),
                             maxchild=maxchild, devnum=devnum))
    return out


def serial_matches(d: UsbDevice, serial: str) -> bool:
    """The USB serial string, or the hardware serial Starlab names the receiver by ("AIRSPY SN:637862DC..." is
    Airspy 637862DC...)."""
    if not serial or not d.serial:
        return False
    if d.serial == serial:
        return True
    from ..inventory.drivers import USB_MODELS, canonical_serial
    return canonical_serial(USB_MODELS.get((d.vid, d.pid)), d.serial) == serial


def duplicate_serials(devices: Iterable[UsbDevice]) -> dict[str, list[str]]:
    """serial -> its sysfs paths, for every non-empty serial seen more than once."""
    seen: dict[str, list[str]] = {}
    for d in devices:
        if d.serial:
            seen.setdefault(d.serial, []).append(d.path)
    return {s: p for s, p in seen.items() if len(p) > 1}


@dataclass(frozen=True)
class PortLocation:
    """Where one serial is on USB right now. status: connected | not_connected | duplicate."""
    serial: str
    status: str
    path: str | None = None
    usb_id: str | None = None
    product: str | None = None
    hub: str | None = None               # parent hub's sysfs name ("3-8.4"; "usb3" for a root hub)
    port: int | None = None
    hub_id: str | None = None            # parent hub's vid:pid
    hub_product: str | None = None
    root_hub: bool = False
    devnum: int = 0
    duplicates: tuple[str, ...] = ()     # every path the serial is on, when duplicated

    @property
    def connected(self) -> bool:
        return self.status == "connected"

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["duplicates"] = list(self.duplicates)
        return d


def locate(serial: str, sysfs: pathlib.Path | None = None, devices: list[UsbDevice] | None = None,
           path: str | None = None) -> PortLocation:
    """Find a device by serial (or, for a receiver identified by its port, by `path`) and describe its hub port."""
    root = sysfs or SYSFS_USB
    devs = scan(root) if devices is None else devices
    if serial:
        found = [d for d in devs if serial_matches(d, serial)]
    else:
        found = [d for d in devs if path and d.path == path]
    if not found:
        return PortLocation(serial=serial, status="not_connected")
    if len(found) > 1:
        return PortLocation(serial=serial, status="duplicate", duplicates=tuple(d.path for d in found))
    d = found[0]
    hub, port, is_root = parent_of(d.path)
    hub_dir = root / hub
    hub_id = f"{_read(hub_dir / 'idVendor')}:{_read(hub_dir / 'idProduct')}"
    return PortLocation(serial=serial, status="connected", path=d.path, usb_id=d.usb_id, product=d.product or None,
                        hub=hub, port=port, hub_id=hub_id if hub_id != ":" else None,
                        hub_product=_read(hub_dir / "product") or None, root_hub=is_root,
                        devnum=d.devnum)


def serial_at(path: str, sysfs: pathlib.Path | None = None) -> str | None:
    """The serial of whatever is plugged in at that sysfs path now; None if nothing is."""
    d = (sysfs or SYSFS_USB) / path
    if not (d / "idVendor").exists():
        return None
    return _read(d / "serial")


# ------------------------------------------------------------------ uhubctl output
@dataclass
class UhubPort:
    port: int
    status: str                      # hex word, e.g. "0103"
    flags: list[str] = field(default_factory=list)
    attached: str = ""               # "[0bda:2838 Realtek RTL2838UHIDIR OBS-R01]" contents, if any

    @property
    def powered(self) -> bool:
        return "power" in self.flags


@dataclass
class UhubHub:
    location: str
    usb_id: str                      # vid:pid
    description: str
    ppps: bool | None                # per-port power switching; False = ganged, None = not stated
    ports: dict[int, UhubPort] = field(default_factory=dict)


_HUB_LINE = re.compile(r"^Current status for hub (\S+) \[([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s*(.*)\]\s*$")
_PORT_LINE = re.compile(r"^\s*Port (\d+): ([0-9a-fA-F]{4})((?:\s+[a-zA-Z0-9_.-]+)*)\s*(?:\[(.*)\])?\s*$")


def parse_uhubctl(text: str) -> dict[str, UhubHub]:
    """uhubctl's listing: "Current status for hub 3-8 [0bda:5411 Generic USB2.1 Hub, USB 2.10, 4 ports, ppps]"
    followed by "  Port 1: 0103 power enable connect [0bda:2838 ...]" lines."""
    hubs: dict[str, UhubHub] = {}
    cur: UhubHub | None = None
    for line in text.splitlines():
        m = _HUB_LINE.match(line.strip())
        if m:
            desc = m.group(3)
            tail = [t.strip().lower() for t in desc.split(",")]
            ppps = True if "ppps" in tail else False if "ganged" in tail else None
            cur = UhubHub(location=m.group(1), usb_id=m.group(2).lower(), description=desc, ppps=ppps)
            hubs[cur.location] = cur
            continue
        m = _PORT_LINE.match(line)
        if m and cur is not None:
            p = int(m.group(1))
            cur.ports[p] = UhubPort(port=p, status=m.group(2), flags=m.group(3).split(), attached=m.group(4) or "")
    return hubs


# ------------------------------------------------------------------ policy
@dataclass(frozen=True)
class HubPolicy:
    switchable_hubs: tuple[str, ...] = DEFAULT_SWITCHABLE_HUBS
    unswitchable_ports: tuple[str, ...] = ()       # "<vid:pid>:<port>" or "<hub location>:<port>"

    @classmethod
    def from_settings(cls, hubs: Iterable[str] | None, unswitchable: Iterable[str] | None) -> "HubPolicy":
        return cls(tuple(str(h).lower() for h in (DEFAULT_SWITCHABLE_HUBS if hubs is None else hubs)),
                   tuple(str(p).lower() for p in (unswitchable or ())))

    def port_excluded(self, hub: str, hub_id: str | None, port: int) -> bool:
        for entry in self.unswitchable_ports:
            key, _, num = entry.rpartition(":")
            if num.isdigit() and int(num) == port and key in (hub.lower(), (hub_id or "").lower()):
                return True
        return False

    def static_check(self, loc: PortLocation) -> tuple[bool, str]:
        """Everything that can be decided from sysfs and the settings alone (no uhubctl)."""
        if loc.status == "not_connected":
            return False, "not connected"
        if loc.status == "duplicate":
            return False, f"duplicate serial ({', '.join(loc.duplicates)})"
        assert loc.hub is not None and loc.port is not None
        if loc.root_hub:
            return False, f"on a root hub port ({loc.hub} port {loc.port})"
        if (loc.hub_id or "").lower() not in self.switchable_hubs:
            return False, f"hub {loc.hub} ({loc.hub_id}) is not in the power-switchable hub list"
        if self.port_excluded(loc.hub, loc.hub_id, loc.port):
            return False, f"port {loc.port} of hub {loc.hub} is listed as not power-switchable"
        return True, f"hub {loc.hub} ({loc.hub_id}) port {loc.port}"

    def check(self, loc: PortLocation, listing: dict[str, UhubHub] | None) -> tuple[bool, str]:
        """Whether uhubctl may power-cycle that one port. Without uhubctl's word for it, no (conservative)."""
        ok, why = self.static_check(loc)
        if not ok:
            return ok, why
        if listing is None:
            return False, "uhubctl listing unavailable"
        hub = listing.get(str(loc.hub))
        if hub is None:
            return False, f"uhubctl does not list hub {loc.hub} as power-switchable"
        if hub.ppps is False:
            return False, f"uhubctl reports hub {loc.hub} as ganged (cycling one port would cycle them all)"
        if hub.ppps is None:
            return False, f"uhubctl does not say hub {loc.hub} switches ports individually"
        if loc.port not in hub.ports:
            return False, f"uhubctl does not list port {loc.port} of hub {loc.hub}"
        return True, why


# ------------------------------------------------------------------ uhubctl runner
Runner = Callable[[list[str], float], "tuple[int, str]"]


def find_uhubctl(name: str = "uhubctl") -> str | None:
    if os.sep in name:
        return name if os.access(name, os.X_OK) else None
    return shutil.which(name) or next((p for p in (f"/usr/sbin/{name}", f"/usr/local/sbin/{name}")
                                       if os.access(p, os.X_OK)), None)


def run_uhubctl(argv: list[str], timeout: float) -> tuple[int, str]:
    """Runs uhubctl for real. Refused in tests and development: it switches USB power."""
    from ..inventory.hosts import hardware_blocked
    why = hardware_blocked()
    if why:
        raise UhubctlError(f"not running uhubctl: {why}")
    exe = find_uhubctl(argv[0])
    if exe is None:
        raise UhubctlError(f"{argv[0]} is not installed (apt install uhubctl)")
    try:
        r = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise UhubctlError(f"uhubctl timed out after {timeout:.0f} s") from exc
    except OSError as exc:
        raise UhubctlError(f"uhubctl could not run: {exc}") from exc
    return r.returncode, (r.stdout or "") + (r.stderr or "")


class Uhubctl:
    def __init__(self, binary: str = "uhubctl", runner: Runner | None = None):
        self.binary = binary
        self.runner = runner or run_uhubctl

    def listing(self, hub: str) -> dict[str, UhubHub]:
        """uhubctl -l <hub> (no action: only reads port status). Raises UhubctlError when it gives nothing usable."""
        rc, out = self.runner([self.binary, "-l", hub], 30.0)
        hubs = parse_uhubctl(out)
        if rc != 0 or not hubs:
            raise UhubctlError(f"uhubctl -l {hub} (exit {rc}): {out.strip()[-300:] or 'no output'}")
        return hubs

    def cycle(self, hub: str, port: int, delay_s: float) -> str:
        """Power-cycle that one port: off, wait delay_s, on."""
        argv = [self.binary, "-l", hub, "-p", str(int(port)), "-a", "cycle", "-d", f"{delay_s:g}"]
        rc, out = self.runner(argv, delay_s + 30.0)
        if rc != 0:
            raise UhubctlError(f"{' '.join(argv)} failed (exit {rc}): {out.strip()[-300:] or 'no output'}")
        return out


def describe(loc: PortLocation, policy: HubPolicy) -> dict[str, Any]:
    """For status pages: the location plus a sysfs-and-settings-only switchability verdict (uhubctl is not run)."""
    ok, why = policy.static_check(loc)
    flags = []
    if loc.status == "not_connected":
        flags.append("not connected")
    elif loc.status == "duplicate":
        flags.append("duplicate serial")
    elif not ok:
        flags.append("port not power-switchable")
    return {**loc.as_dict(), "switchable": ok, "switchable_detail": why, "flags": flags}


def receiver_ports(receivers: Iterable[Any], policy: HubPolicy, sysfs: pathlib.Path | None = None) -> dict[str, Any]:
    """receiver id -> describe() of where it is plugged in, for the local USB receivers (remote and simulated ones
    are left out). One sysfs scan for all of them."""
    root = sysfs or SYSFS_USB
    devs = scan(root)
    out: dict[str, Any] = {}
    for rx in receivers:
        if getattr(rx, "remote", False) or getattr(rx, "driver", "") == "sim":
            continue
        by_path = getattr(rx, "identity", "serial") == "usb_path"
        loc = locate("" if by_path else str(rx.serial or ""), root, devs, path=getattr(rx, "usb_path", "") or None)
        out[rx.id] = describe(loc, policy)
    return out


def policy_from_config(section: dict[str, Any]) -> HubPolicy:
    """[usb_hubs] in starlab.toml: switchable = ["0bda:5411"], unswitchable_ports = ["0bda:5411:4"]."""
    return HubPolicy.from_settings(section.get("switchable"), section.get("unswitchable_ports"))
