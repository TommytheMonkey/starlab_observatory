"""Is a receiver physically on USB? One answer for status, health, the test lab, booking and the runner.

present  - seen: RTL-SDR or SDRplay serial in sysfs, or the SDRplay API lists the serial
absent   - proven gone: serial not in sysfs (RTL), or no SDRplay device on USB at all, or the API
           answered and lists other serials only
unknown  - an SDRplay device is on USB but its identity cannot be confirmed right now (API service
           down, enumeration failed, or deferred during a recording window); never treated as absent

The API enumeration is cached (a minute when it answered, less when it failed) and only runs when an
SDRplay device sits on USB without its serial, which after the first enumeration it no longer does.
"""

from __future__ import annotations

import pathlib
import threading
import time
from dataclasses import dataclass
from typing import Callable

from ..hardware import sdrplay
from ..hardware.rtlsdr import SYSFS_USB, _read
from .hardware import Receiver

PRESENT, ABSENT, UNKNOWN = "present", "absent", "unknown"
REENUMERATE_GRACE_S = 10.0           # firmware load: the RSP leaves USB for about a second and comes back


@dataclass(frozen=True)
class Presence:
    state: str                       # present | absent | unknown
    detail: str
    reason: str = ""                 # unknown: api_unavailable | discovery_failed | discovery_deferred | re_enumerating

    @property
    def connected(self) -> bool | None:
        return {PRESENT: True, ABSENT: False}.get(self.state)

    def as_dict(self) -> dict[str, str]:
        return {"state": self.state, "detail": self.detail, **({"reason": self.reason} if self.reason else {})}


def _sysfs_serials(sysfs: pathlib.Path) -> set[str]:
    return {s for e in sysfs.glob("*") if (s := _read(e / "serial"))}


class PresenceProbe:
    def __init__(self, sysfs: pathlib.Path = SYSFS_USB, find: Callable[[], sdrplay.ApiFind] = sdrplay.api_find,
                 api_state: Callable[[], str] | None = None, quiet: Callable[[Receiver], bool] = lambda rx: False,
                 clock: Callable[[], float] = time.monotonic, ttl_s: float = 60, fail_ttl_s: float = 30):
        self.sysfs, self.find, self.quiet, self.clock = sysfs, find, quiet, clock
        self.api_state = api_state or self._unit_state
        self.ttl_s, self.fail_ttl_s = ttl_s, fail_ttl_s
        self._cache: tuple[float, tuple[tuple[str, str], ...], sdrplay.ApiFind] | None = None
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _unit_state() -> str:
        from .status import unit_state
        return unit_state("sdrplay.service")

    def __call__(self, rx: Receiver) -> bool:
        """Scheduler/runner gate: False only when the receiver is proven gone."""
        return self.check(rx).state != ABSENT

    def check(self, rx: Receiver) -> Presence:
        if not self.sysfs.is_dir():
            return Presence(UNKNOWN, f"USB inventory {self.sysfs} is unreadable", "discovery_failed")
        if rx.driver not in ("rtlsdr", "sdrplay"):       # Soapy receivers: serial as discovery reads it
            from ..inventory.discovery import find_device
            dev = find_device(rx.serial, self.sysfs, rx.usb_path if rx.identity == "usb_path" else None)
            if dev is None:
                return Presence(ABSENT, f"{rx.id} ({rx.serial or 'USB port ' + rx.usb_path}) not on USB")
            self._seen[rx.serial] = self.clock()
            return Presence(PRESENT, f"serial {rx.serial} on USB ({dev.usb_path})")
        if rx.serial in _sysfs_serials(self.sysfs):
            self._seen[rx.serial] = self.clock()
            return Presence(PRESENT, f"serial {rx.serial} on USB")
        if rx.driver != "sdrplay":
            return Presence(ABSENT, f"serial {rx.serial} not on USB")
        return self._sdrplay(rx)

    def _sdrplay(self, rx: Receiver) -> Presence:
        unnamed = [d for d in sdrplay.list_sdrplay_devices(self.sysfs) if not d.serial]
        if not unnamed:
            seen = self._seen.get(rx.serial)
            if seen is not None and self.clock() - seen < REENUMERATE_GRACE_S:
                return Presence(UNKNOWN, f"{rx.id} left USB {self.clock() - seen:.0f} s ago; it re-enumerates "
                                         "while the SDRplay API loads its firmware", "re_enumerating")
            return Presence(ABSENT, f"no SDRplay device with serial {rx.serial} on USB")
        where = f"an SDRplay device is on USB without its serial yet (firmware not loaded, {unnamed[0].sysfs_path})"
        found = self._find(rx, tuple((d.sysfs_path, d.devnum) for d in unnamed))
        if found is None:
            return Presence(UNKNOWN, f"{where}; not enumerated while a recording window is open", "discovery_deferred")
        if found.ok and rx.serial in found.serials:
            self._seen[rx.serial] = self.clock()
            return Presence(PRESENT, f"serial {rx.serial} listed by the SDRplay API (SoapySDR)")
        api = self.api_state()
        if api != "active":
            return Presence(UNKNOWN, f"{where}; sdrplay.service is {api}, so the API cannot identify it", "api_unavailable")
        if not found.ok:
            return Presence(UNKNOWN, f"{where}; {found.detail}", "discovery_failed")
        if found.serials:
            return Presence(ABSENT, f"{found.detail}, not {rx.serial}")
        return Presence(UNKNOWN, f"{where}; the API lists no device yet", "discovery_failed")

    def _find(self, rx: Receiver, usb: tuple[tuple[str, str], ...]) -> sdrplay.ApiFind | None:
        """Cached per USB device set: a replug or re-enumeration always asks the API again."""
        with self._lock:
            if self._cache is not None:
                at, key, found = self._cache
                if key == usb and self.clock() - at < (self.ttl_s if found.ok else self.fail_ttl_s):
                    return found
            if self.quiet(rx):
                return None
            found = self.find()
            self._cache = (self.clock(), usb, found)
            return found
