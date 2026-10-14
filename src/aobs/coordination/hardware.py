"""The broker's live Hardware implementation (see broker.Hardware)."""

from __future__ import annotations

import os
import pathlib
import subprocess
from typing import Any

from ..config import Config
from ..hardware import antsw, rtlsdr
from ..legacy import load_legacy
from .broker import BrokerError

ADSB_ENV = pathlib.Path("/run/observatory-sdr/adsb.env")
ADSB_DROPIN = pathlib.Path("/etc/systemd/system/observatory-adsb.service.d/device-index.conf")


def _secret_env(path: pathlib.Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"')
    return values


class LiveHardware:
    def __init__(self, config: Config):
        self.config = config
        sw = config.section("antenna_switch")
        self.switch_url = str(sw["base_url"])
        token_file = pathlib.Path(os.path.expanduser(str(sw.get("token_file", "~/.config/observatory/antswitch.env"))))
        self.switch_token = _secret_env(token_file)["ANTSW_API_TOKEN"]
        self.adsb_unit = str(config.section("legacy").get("adsb_unit", "observatory-adsb.service"))

    # antenna switch
    def switch_state(self) -> dict[str, Any]:
        return antsw.get_state(self.switch_url)

    def switch_board_ports(self) -> dict[str, Any]:
        return antsw.get_ports(self.switch_url, timeout=3.0)

    def switch_push_names(self, ports: dict[int, dict[str, Any]]) -> dict[str, Any]:
        return antsw.push_names(self.switch_url, self.switch_token, ports)

    def switch_select(self, port: int) -> None:
        antsw.select(self.switch_url, self.switch_token, port)
        now = antsw.get_state(self.switch_url).get("port")
        if now != port:
            raise BrokerError(f"ANTSW-01 reports port {now} after selecting {port}", 502)

    # ADS-B (dump1090 only takes --device-index, never a serial)
    def adsb_active(self) -> bool:
        return subprocess.run(["systemctl", "is-active", "--quiet", self.adsb_unit]).returncode == 0

    def _sudo_systemctl(self, verb: str) -> None:
        r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", verb, self.adsb_unit], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise BrokerError(f"systemctl {verb} {self.adsb_unit} failed: {r.stderr.strip()[-300:]}", 500)

    def adsb_stop(self) -> None:
        self._sudo_systemctl("stop")

    def adsb_start(self, device_index: int) -> None:
        if not ADSB_DROPIN.exists() and device_index != 0:
            # Without the drop-in the unit hard-codes index 0, which is now another receiver.
            raise BrokerError(f"ADS-B receiver is librtlsdr index {device_index}, but {ADSB_DROPIN} is missing; "
                              "not starting dump1090 on the wrong dongle", 500)
        ADSB_ENV.write_text(f"ADSB_DEVICE_INDEX={device_index}\n")
        self._sudo_systemctl("start")

    # USB
    def device_index(self, serial: str) -> int | None:
        serials = rtlsdr.librtlsdr_index_map()
        return serials.index(serial) if serial in serials else None

    def device_busy(self, serial: str) -> list[str]:
        dev = next((d for d in rtlsdr.list_rtl_devices() if d.serial == serial), None)
        if dev is None:
            raise BrokerError(f"{serial} is not on USB", 503)
        holders, _ = rtlsdr.usb_holders(dev.dev_node)
        busy = [f"{h.comm}[{h.pid}]" for h in holders if h.pid != os.getpid()]
        if serial == self.config.section("legacy").get("adsb_serial") and self.adsb_active():
            busy.append(self.adsb_unit)
        return busy

    # legacy coordinator leases (re-read the station config every time; it changes)
    def coordinator_reserve(self, receiver: str, owner: str, ttl_s: int, purpose: str) -> str:
        legacy = load_legacy(self.config)
        try:
            lease = legacy.coordinator.reserve(legacy.station, receiver, "", owner, 30, ttl_seconds=ttl_s,
                                               preempt=False, purpose=purpose or "aobs scanning")
        except legacy.coordinator.CoordinatorError as exc:
            raise BrokerError(str(exc)) from exc
        return str(lease["token"])

    def coordinator_release(self, receiver: str, token: str) -> None:
        legacy = load_legacy(self.config)
        legacy.coordinator.release(legacy.station, receiver, token=token)

    def popen(self, cmd: list[str], stdout: Any) -> subprocess.Popen[bytes]:
        return subprocess.Popen(cmd, stdout=stdout, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
