"""Turn what recorders, USB libraries and SSH print into messages a person can act on (brief 7).

explain(text, rx) looks for known failure signatures in an error message or a recorder log and returns the
first match as {code, message, action}; the message names the receiver ("OBS-R01 could not start because
another process has claimed its USB interface."). Unknown errors pass through unchanged with code "unknown",
so nothing is ever hidden behind a vague summary. Diagnostics keeps a job's timestamped record of checks and
errors (sched_jobs.execution["diagnostics"]).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable

# (code, pattern, message with {rx}, what to do). First match wins, so specific signatures come first.
SIGNATURES: list[tuple[str, str, str, str]] = [
    ("usb_claimed", r"usb_claim_interface error|LIBUSB_ERROR_BUSY|claimed its USB interface|Device or resource busy",
     "{rx} could not start because another process has claimed its USB interface.",
     "Close the other program (SDR++, dump1090, rtl_tcp, another rtl_sdr), or wait for it to finish."),
    ("busy", r"held by |in use by |open in |is held by",
     "{rx} could not start because another program is using it.",
     "Close that program or wait for it to finish; Starlab never opens a receiver a second time."),
    ("reserved", r"reserved by Starlab job|reserved by another Starlab job",
     "{rx} could not start because another Starlab job holds its reservation.",
     "Two jobs were booked on the same device; check the receiver's schedule."),
    ("api_busy", r"sdrplay_api_(?:Fail|ServiceNotResponding)|device (?:is )?(?:already )?in use|SDRPLAY_DEVICE_BUSY",
     "{rx} could not start because the SDRplay API reports the device is in use or not responding.",
     "Check that no other SDRplay program is open; restart sdrplay.service between recordings if it hangs."),
    ("ssh_host_key", r"host_key_changed|REMOTE HOST IDENTIFICATION HAS CHANGED",
     "{rx}'s host presented a different SSH key, so Starlab did not connect.",
     "Compare the new key with the host and accept it under Settings → Receiver hosts only if it matches."),
    ("ssh_auth", r"auth_failed|Permission denied \(publickey",
     "{rx}'s host refused Starlab's SSH key.",
     "Add Starlab's public key to ~/.ssh/authorized_keys of that user on the host."),
    ("usb_permission", r"LIBUSB_ERROR_ACCESS|usb_open error -3|insufficient permissions",
     "{rx} could not be opened: this user is not allowed to access its USB device.",
     "Install the udev rules for the receiver (or add the user to the plugdev group) and replug it."),
    ("usb_disconnected", r"LIBUSB_ERROR_NO_DEVICE|cb transfer status: 5|No such device|device (?:has been )?disconnected"
                         r"|usb_open error -4|Failed to submit transfer",
     "{rx} disappeared from USB during the recording.",
     "Check the cable, hub and power supply; the kernel log shows USB resets and disconnects."),
    ("device_missing", r"No supported devices found|No matching devices|No devices found|Failed to open rtlsdr device"
                       r"|not connected|no match for serial|SoapySDR::Device::make\(\) no match",
     "{rx} was not found on USB.",
     "Check that it is plugged in to the right computer; the receiver page shows what is connected."),
    ("disk_full", r"No space left on device|ENOSPC|Disk quota exceeded|not enough free space|insufficient disk",
     "{rx} cannot record: there is not enough disk space for the recording.",
     "Free space on the recording disk (Settings → Storage shows usage) or choose another storage destination."),
    ("permission", r"Permission denied|EACCES|Read-only file system|Operation not permitted",
     "{rx}'s recording could not write its files (permission denied).",
     "Check the owner and mode of the recording folder for the user Starlab runs as."),
    ("missing_python_module", r"No module named '?(numpy|SoapySDR)'?",
     "{rx} cannot record on this host: the Python module {m1} is not installed there.",
     "Install it on that host (python3-numpy, python3-soapysdr / SoapySDR Python bindings)."),
    ("missing_tool", r"(rtl_sdr|python3?|SoapySDRUtil)(?::| is)? (?:command )?not found|No such file or directory: '([^']+)'"
                     r"|could not run ([^:]+):",
     "{rx} cannot record on this host: a required program is not installed ({m1}).",
     "Install the receiver's tools on that host (Receivers → host test lists what was found)."),
    ("ssh_unreachable", r"Connection refused|Connection timed out|No route to host|Network is unreachable|"
                        r"no answer from the host|Could not resolve hostname|Connection closed by|Broken pipe|"
                        r"Connection reset|ssh_exchange_identification|kex_exchange_identification",
     "{rx}'s host could not be reached over SSH.",
     "Check that the host is switched on and on the network; Starlab retries until the recording window ends."),
    ("host_rebooted", r"host rebooted|boot id changed",
     "{rx}'s host restarted during the recording, which stopped it.",
     "Look at why the host restarted (power, updates); the part recorded before the restart is kept if any."),
    ("recorder_crashed", r"ended without writing a result|Segmentation fault|core dumped|Killed|Aborted",
     "{rx}'s recorder stopped unexpectedly (crashed or was killed).",
     "The recorder log below shows its last lines; out-of-memory kills appear in the kernel log."),
    ("recorder_timeout", r"did not stop|timed out waiting for the recorder",
     "{rx}'s recorder did not stop at the end of the recording and was stopped by Starlab.",
     "The recorder or the receiver driver may be hung; check the recorder log."),
    ("short_recording", r"expected bytes|of \d+ expected|exited early|is cut|not a whole number",
     "{rx} recorded less than the schedule asked for.",
     "Usually USB drops or the device stalling; check IQ health and the recorder log."),
    ("rate_rejected", r"did not accept|reads back|not accept the sample rate|rejects",
     "{rx}'s driver did not accept the requested settings.",
     "Choose a sample rate, gain or bandwidth the receiver supports (receiver capabilities)."),
]

_COMPILED = [(c, re.compile(p, re.I), m, a) for c, p, m, a in SIGNATURES]


def explain(text: str, rx_id: str = "the receiver") -> dict[str, str]:
    t = text or ""
    for code, rx, msg, action in _COMPILED:
        m = rx.search(t)
        if m:
            groups = [g for g in m.groups() if g] if m.groups() else []
            return {"code": code, "message": msg.format(rx=rx_id, m1=groups[0] if groups else "?"), "action": action,
                    "detail": t.strip()[-400:]}
    return {"code": "unknown", "message": f"{rx_id}: {t.strip()[-300:] or 'unknown error'}", "action": "",
            "detail": t.strip()[-400:]}


def message_for(code: str, rx_id: str) -> dict[str, str] | None:
    """The plain message and action for a known failure code (start checks report codes directly)."""
    for c, _rx, msg, action in _COMPILED:
        if c == code:
            return {"code": code, "message": msg.format(rx=rx_id, m1="?"), "action": action}
    return None


DEVICE_ERRORS = re.compile(r"LIBUSB_ERROR_\w+|cb transfer status: \d+|Failed to submit transfer|lost \d+ samples|"
                           r"usb_claim_interface error|SoapySDR stream error|Samples? (?:were )?dropped", re.I)


def device_errors(log_text: str) -> list[str]:
    """Device error lines in a recorder log (USB transfer failures, dropped samples)."""
    seen: dict[str, None] = {}
    for line in (log_text or "").splitlines():
        if DEVICE_ERRORS.search(line):
            seen.setdefault(line.strip()[:200])
    return list(seen)


def log_tail(path: str, max_bytes: int = 4000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            n = f.tell()
            f.seek(max(0, n - max_bytes))
            return f.read().decode(errors="replace")
    except OSError:
        return ""


class Diagnostics:
    """A job's timestamped execution record: each start check, what happened, and translated errors."""

    def __init__(self, clock: Callable[[], dt.datetime], rx_id: str):
        self.clock, self.rx_id = clock, rx_id
        self.entries: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []

    def check(self, name: str, ok: bool | None, detail: str, code: str = "") -> bool:
        """ok None: could not be checked (recorded, not fatal)."""
        self.checks.append({"check": name, "ok": ok, "detail": detail, "at": self.clock().isoformat(),
                            **({"code": code} if code else {})})
        return ok is not False

    def note(self, stage: str, message: str, level: str = "info", **extra: Any) -> None:
        self.entries.append({"at": self.clock().isoformat(), "stage": stage, "level": level, "message": message,
                             **extra})

    def error(self, stage: str, text: str) -> dict[str, str]:
        e = explain(text, self.rx_id)
        self.note(stage, e["message"], "error", code=e["code"], action=e["action"], detail=e["detail"])
        return e

    def as_dict(self) -> dict[str, Any]:
        return {"checks": self.checks, "diagnostics": self.entries}
