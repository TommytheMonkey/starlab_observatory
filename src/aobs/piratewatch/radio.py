"""Starts rtl_power / rtl_fm on the leased receiver and streams their output.

Both tools open a physical SDR, so in tests and development (aobs.inventory.hosts.hardware_blocked)
they run only as mocks from `tool_prefix`, by absolute path; with no mock they are refused.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
from typing import Callable

from ..inventory.hosts import hardware_blocked
from .config import Settings


class RadioError(RuntimeError):
    pass


def resolve(tool: str, prefix: str | None) -> str:
    if prefix:
        path = os.path.join(prefix, tool)
        if not os.access(path, os.X_OK):
            raise RadioError(f"mock {path} is missing")
        return path
    why = hardware_blocked()
    if why:
        raise RadioError(f"not running {tool}: {why}")
    return tool


def sweep_command(s: Settings, index: int) -> list[str]:
    cmd = ["rtl_power", "-f", f"{s.start_hz}:{s.stop_hz}:{s.bin_hz}", "-i", str(int(s.integration_s)),
           "-d", str(int(index))]
    if s.gain_db is not None:
        cmd += ["-g", f"{s.gain_db:g}"]
    if s.crop:
        cmd += ["-c", f"{round(s.crop * 100)}%"]
    if s.ppm:
        cmd += ["-p", str(s.ppm)]
    return cmd + ["-"]


def audio_command(s: Settings, index: int, freq_hz: int) -> list[str]:
    # -s is both the channel filter width and the output rate; never -r (rtl_fm's resampler SIGFPEs).
    cmd = ["rtl_fm", "-f", str(int(freq_hz)), "-M", "fm", "-s", str(s.audio_bw_hz), "-d", str(int(index)), "-E", "dc"]
    if s.gain_db is not None:
        cmd += ["-g", f"{s.gain_db:g}"]
    if s.ppm:
        cmd += ["-p", str(s.ppm)]
    return cmd + ["-"]


class Stream:
    """A running tool with its stdout pumped into a queue (lines or byte chunks) so callers can poll."""

    def __init__(self, cmd: list[str], prefix: str | None, lines: bool):
        if "-T" in cmd:          # rtl_power/rtl_fm -T switches the bias tee on: never, there is no amplifier to power
            raise RadioError(f"refusing to run {cmd[0]} with the bias tee on (-T)")
        argv = [resolve(cmd[0], prefix), *cmd[1:]]
        self.cmd = argv
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        self.out: queue.Queue[bytes | str | None] = queue.Queue()
        self._err: list[bytes] = []
        threading.Thread(target=self._pump, args=(lines,), daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    def _pump(self, lines: bool) -> None:
        assert self.proc.stdout is not None
        if lines:
            for raw in self.proc.stdout:
                self.out.put(raw.decode(errors="replace"))
        else:
            while chunk := self.proc.stdout.read1(8192):
                self.out.put(chunk)
        self.out.put(None)

    def _pump_err(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self._err.append(raw)
            del self._err[:-60]

    def get(self, timeout: float) -> bytes | str | None | bool:
        """Next item; None at end of stream; False on timeout."""
        try:
            return self.out.get(timeout=timeout)
        except queue.Empty:
            return False

    @property
    def stderr(self) -> str:
        return b"".join(self._err).decode(errors="replace")[-1500:]

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


def default_device_index(serial: str) -> int:
    if hardware_blocked():
        raise RadioError("device index lookup is disabled in tests/development")
    from ..hardware.rtlsdr import librtlsdr_index_map
    serials = librtlsdr_index_map()
    if serials.count(serial) != 1:
        raise RadioError(f"{serial} is not on USB (or more than one RTL-SDR reports that serial)")
    return serials.index(serial)


def default_holders(serial: str) -> list[str]:
    """Other processes with the receiver's USB node open (SDR++ server bypasses the broker)."""
    from ..hardware.rtlsdr import list_rtl_devices, usb_holders
    dev = next((d for d in list_rtl_devices() if d.serial == serial), None)
    if dev is None:
        return [f"{serial} not on USB"]
    holders, _ = usb_holders(dev.dev_node)
    return [f"{h.comm}[{h.pid}]" for h in holders if h.pid != os.getpid()]


def eeprom_bias_forced(index: int, prefix: str | None) -> bool | None:
    """Whether the dongle's EEPROM forces the bias tee on at every open (RTL-SDR Blog librtlsdr honours this flag,
    so no rtl_* option can turn it off). rtl_eeprom with no write options only reads. None = flag not reported."""
    argv = [resolve("rtl_eeprom", prefix), "-d", str(int(index))]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise RadioError("rtl_eeprom timed out") from exc
    for line in (r.stdout + r.stderr).splitlines():
        if line.strip().lower().startswith("bias tee always on:"):
            return line.split(":", 1)[1].strip().lower() in ("yes", "on", "1", "enabled")
    if "Current configuration" not in r.stdout + r.stderr:
        raise RadioError(f"rtl_eeprom could not read the EEPROM (exit {r.returncode}): {(r.stderr or r.stdout)[-300:]}")
    return None


def default_bias_off(serial: str) -> None:
    """Switch the bias tee off by serial (it is a GPIO that keeps whatever the last user set)."""
    if hardware_blocked():
        raise RadioError("bias tee control is disabled in tests/development")
    from ..scheduling.stepcapture import set_rtl_bias_tee
    try:
        set_rtl_bias_tee(serial, False)
    except RuntimeError as exc:
        raise RadioError(f"could not switch {serial}'s bias tee off: {exc}") from exc


DeviceIndex = Callable[[str], int]
Holders = Callable[[str], list[str]]
BiasForced = Callable[[int], "bool | None"]
BiasOff = Callable[[str], None]
