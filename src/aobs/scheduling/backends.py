"""Where a recording runs (brief 7): this computer, a remote Linux host over SSH, or (later) a receiver agent.

The runner hands every backend the same RecordingRequest, built from a booked job and validated field by field
(IDs, serial, driver, format, times, numbers, paths). The request reaches the recorder as JSON (a run file on
stdin), never as words in a shell command: remote commands are fixed scripts whose few arguments are
validated and quoted (shlex) by inventory.hosts.SshHost.

A backend answers five things:

  preflight(req, rx, diag)   is the receiver reachable, is there room for the file, is the device free
  start(req) -> handle       start the recorder (detached: it survives a broker restart or an SSH drop)
  poll(handle)               running | finished | unreachable | rebooted | lost
  stop(handle, hard)         SIGTERM (keeps what was recorded) or SIGKILL, only to the recorder it started
  collect(handle, req)       the recorder's result, with the file at req.raw on this computer

Local recordings run `python -m aobs.scheduling.capture` as before. Remote recordings copy capture.py (a
single file that needs only Python 3 and numpy, plus SoapySDR or rtl_sdr for real receivers) to the host,
record into <work_dir>/recordings/<job id>/ there, and bring the file back over the same SSH connection;
size and SHA-256 are compared before the remote copy is removed. In development and in tests (see
inventory.hosts.hardware_blocked) a remote host may only run simulated receivers.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import math
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Protocol

from ..inventory.hosts import LOCAL, hardware_blocked
from .diagnose import Diagnostics, device_errors, explain, log_tail, message_for
from .hardware import BYTES_PER_SAMPLE, DRIVERS, Receiver

LOG = logging.getLogger("aobs.backends")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SERIAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}$")
WORK_DIR_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
PYTHON_RE = re.compile(r"^[A-Za-z0-9._/+-]{1,200}$")
STORAGE_MARGIN = 256 * 1024**2            # bytes beyond the estimate that must be free at start
SSH_CODES = {"timeout": "ssh_unreachable", "refused": "ssh_unreachable", "unreachable": "ssh_unreachable",
             "dns": "ssh_unreachable", "closed": "ssh_unreachable", "ssh": "ssh_unreachable",
             "auth_failed": "ssh_auth", "host_key_changed": "ssh_host_key", "host_key_unknown": "ssh_host_key"}


class BackendError(RuntimeError):
    def __init__(self, message: str, code: str = "backend"):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ requests
def _scalar(v: Any) -> bool:
    return v is None or isinstance(v, (bool, int, str)) or (isinstance(v, float) and math.isfinite(v))


def _clean_path(p: str, what: str) -> str:
    s = str(p or "")
    if not s.startswith("/") or "\0" in s or "\n" in s or ".." in pathlib.PurePosixPath(s).parts:
        raise BackendError(f"{what} must be an absolute path without '..' ({s!r})", "bad_request")
    return s


@dataclasses.dataclass(frozen=True)
class RecordingRequest:
    """One recording, as every backend receives it."""
    job_id: str
    receiver_id: str
    host: str
    driver: str
    serial: str
    radio: dict[str, Any]
    format: str
    record_start: str
    record_end: str
    raw: str                                 # where the file must end up on this computer
    log: str
    estimated_bytes: int = 0
    soapy_driver: str = ""
    simulated: bool = False
    steps: list[dict[str, Any]] | None = None
    rtl_sdr_path: str = "rtl_sdr"

    @classmethod
    def from_job(cls, job: dict[str, Any], rx: Receiver, settings: dict[str, Any]) -> "RecordingRequest":
        spec, paths = job["spec"], job["paths"]
        req = cls(job_id=job["job_id"], receiver_id=rx.id, host=rx.host, driver=rx.driver, serial=rx.serial,
                  radio=dict(spec["radio"]), format=spec["recording"]["format"], record_start=job["record_start"],
                  record_end=job["record_end"], raw=paths["raw"], log=paths["log"],
                  estimated_bytes=int(job.get("estimated_bytes") or 0),
                  soapy_driver=rx.soapy if rx.gain_model == "elements" else "", simulated=rx.simulated,
                  steps=spec.get("steps") or None,
                  rtl_sdr_path=settings.get("rtl_sdr_path", "/usr/bin/rtl_sdr") if not rx.remote else "rtl_sdr")
        req.validate()
        return req

    def validate(self) -> None:
        bad = []
        if not JOB_ID_RE.match(self.job_id):
            bad.append(f"job id {self.job_id!r}")
        if self.driver not in DRIVERS:
            bad.append(f"driver {self.driver!r}")
        if not SERIAL_RE.match(self.serial):
            bad.append(f"serial {self.serial!r}")
        if self.format not in BYTES_PER_SAMPLE:
            bad.append(f"format {self.format!r}")
        if self.soapy_driver and not re.match(r"^[a-z0-9_]{1,32}$", self.soapy_driver):
            bad.append(f"SoapySDR driver {self.soapy_driver!r}")
        try:
            s, e = dt.datetime.fromisoformat(self.record_start), dt.datetime.fromisoformat(self.record_end)
            if s.tzinfo is None or e <= s:
                bad.append("recording times")
        except (TypeError, ValueError):
            bad.append("recording times")
        for k in ("center_frequency_hz", "sample_rate_hz"):
            v = self.radio.get(k)
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                bad.append(f"radio.{k} {v!r}")
        for k, v in self.radio.items():
            if not (_scalar(v) or (isinstance(v, dict) and all(_scalar(x) for x in v.values()))):
                bad.append(f"radio.{k}")
        if self.rtl_sdr_path != "rtl_sdr":
            _clean_path(self.rtl_sdr_path, "rtl_sdr path")
        if bad:
            raise BackendError("refusing a malformed recording request: " + ", ".join(bad), "bad_request")
        _clean_path(self.raw, "recording file")
        _clean_path(self.log, "recording log")
        if self.steps and self.host != LOCAL:
            raise BackendError("scans and gain sweeps (stepped captures) run on this computer only", "unsupported")

    @property
    def seconds(self) -> float:
        return (dt.datetime.fromisoformat(self.record_end) - dt.datetime.fromisoformat(self.record_start)).total_seconds()

    def run_doc(self, raw: str, result: str) -> dict[str, Any]:
        """The recorder's run file (aobs.scheduling.capture)."""
        run: dict[str, Any] = {"job_id": self.job_id, "driver": self.driver, "serial": self.serial, "radio": self.radio,
                               "format": self.format, "record_start": self.record_start,
                               "record_end": self.record_end, "raw": raw, "result": result,
                               "rtl_sdr_path": self.rtl_sdr_path}
        if self.soapy_driver:
            run["soapy_driver"] = self.soapy_driver
        if self.simulated:
            run["simulated"] = True
        if self.steps:
            run["steps"] = self.steps
        return run


def storage_check(diag: Diagnostics, where: str, free: int | None, need: int) -> bool:
    if free is None:
        return diag.check("storage", None, f"free space at {where} could not be read")
    ok = free >= need
    return diag.check("storage", ok, f"{free / 1e9:.1f} GB free at {where}, {need / 1e9:.2f} GB needed",
                      "" if ok else "disk_full")


class Backend(Protocol):
    kind: str

    def preflight(self, req: RecordingRequest, rx: Receiver, diag: Diagnostics) -> bool: ...
    def start(self, req: RecordingRequest) -> dict[str, Any]: ...
    def poll(self, handle: dict[str, Any]) -> str: ...
    def stop(self, handle: dict[str, Any], hard: bool = False) -> bool: ...
    def collect(self, handle: dict[str, Any], req: RecordingRequest) -> dict[str, Any]: ...


# ------------------------------------------------------------------ this computer
class LocalBackend:
    """`python -m aobs.scheduling.capture` in its own session on this computer (RunnerHardware does the I/O)."""
    kind = "local"

    def __init__(self, scheduler: Any, hw: Any, sleep: Callable[[float], None] = time.sleep, busy_wait_s: float = 20):
        self.s, self.hw, self.sleep, self.busy_wait_s = scheduler, hw, sleep, busy_wait_s
        self._procs: dict[str, Any] = {}          # job id -> the Popen this broker started

    def preflight(self, req: RecordingRequest, rx: Receiver, diag: Diagnostics) -> bool:
        ok = diag.check("connected", bool(self.hw.present(rx)),
                        "simulated receiver" if rx.simulated else
                        f"serial {rx.serial} {'is' if self.hw.present(rx) else 'is not'} on USB",
                        "" if rx.simulated or self.hw.present(rx) else "device_missing")
        if not ok:
            return False
        directory = str(pathlib.Path(req.raw).parent)
        try:
            free = self.s._disk_free(directory)
        except OSError:
            free = None
        if not storage_check(diag, directory, free, req.estimated_bytes + STORAGE_MARGIN):
            return False
        deadline = time.monotonic() + self.busy_wait_s
        while True:                                  # a just-finished program may need a moment to let go
            busy = self.hw.busy(rx)
            if not busy:
                return diag.check("not in use", True, "no other program has the device open")
            if time.monotonic() > deadline:
                text = "; ".join(busy)
                code = "usb_claimed" if "claimed" in text else "busy"
                detail = text if text.startswith(("in use", "open in")) else f"held by {text}"
                return diag.check("not in use", False, detail, code)
            self.sleep(1)

    def start(self, req: RecordingRequest) -> dict[str, Any]:
        directory = pathlib.Path(req.raw).parent
        directory.mkdir(parents=True, exist_ok=True)
        run = req.run_doc(req.raw, req.raw + ".result.json")
        path = directory / f".{req.job_id}.run.json"      # job id in argv lets alive() match the process
        path.write_text(json.dumps(run, indent=2))
        proc = self.hw.spawn([sys.executable, "-m", "aobs.scheduling.capture", str(path)], req.log)
        self._procs[req.job_id] = proc
        return {"backend": self.kind, "host": LOCAL, "pid": proc.pid, "job_id": req.job_id}

    def poll(self, handle: dict[str, Any]) -> str:
        proc = self._procs.get(handle["job_id"])
        running = proc.poll() is None if proc is not None else self.hw.alive(handle["pid"], handle["job_id"])
        return "running" if running else "finished"

    def stop(self, handle: dict[str, Any], hard: bool = False) -> bool:
        """Signal the recorder's process group through the hardware layer, which first proves (by its command
        line) that the PID is this job's recorder; test doubles without signal() never signal anything."""
        pid, jid = handle.get("pid"), handle["job_id"]
        send = getattr(self.hw, "signal", None)
        if not pid or send is None:
            return False
        return bool(send(int(pid), jid, signal.SIGKILL if hard else signal.SIGTERM))

    def collect(self, handle: dict[str, Any], req: RecordingRequest) -> dict[str, Any]:
        proc = self._procs.pop(req.job_id, None)
        try:
            capture = json.loads(pathlib.Path(req.raw + ".result.json").read_text())
        except (OSError, ValueError):
            capture = {"status": "failed", "error": "capture process ended without writing a result"}
        code = proc.poll() if proc is not None else None
        if code is not None:                      # known only for a recorder this broker started (not after a restart)
            capture["exit_code"] = code
        return capture


# ------------------------------------------------------------------ a remote host over SSH
PREFLIGHT_SH = r"""
w=$1; py=$2; drv=$3; tool=$4
printf 'boot\t%s\n' "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
if err=$(mkdir -p "$w/recordings" 2>&1) && [ -w "$w/recordings" ]; then printf 'writable\tyes\n'
else printf 'writable\tno\t%s\n' "$(printf %s "$err" | tr -d '\t\n' | head -c 200)"; fi
printf 'free_kb\t%s\n' "$(df -Pk "$w/recordings" 2>/dev/null | awk 'NR==2 {print $4}')"
if command -v "$py" >/dev/null 2>&1; then
  printf 'python\t%s\n' "$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>&1 | tr -d '\t\n' | head -c 100)"
else printf 'python\tmissing\n'; fi
case $drv in
  rtlsdr) mods='import numpy'; command -v "$tool" >/dev/null 2>&1 && printf 'tool\tok\n' || printf 'tool\tmissing\t%s\n' "$tool" ;;
  sim) mods='import numpy' ;;
  *) mods='import numpy, SoapySDR' ;;
esac
if out=$("$py" -c "$mods" 2>&1); then printf 'modules\tok\n'
else printf 'modules\tmissing\t%s\n' "$(printf %s "$out" | tail -n 1 | tr -d '\t\n' | head -c 200)"; fi
"""

DEPLOY_SH = r"""
[ -f "$1" ] && exit 0
mkdir -p "$(dirname "$1")" && cat > "$1.tmp" && mv "$1.tmp" "$1"
"""

WRITE_RUN_SH = r"""
umask 077
mkdir -p "$1" && cat > "$1/run.json.tmp" && mv "$1/run.json.tmp" "$1/run.json"
"""

START_SH = r"""
script=$(cd "$(dirname "$3")" && pwd)/$(basename "$3")
cd "$1" || exit 3
setsid nohup "$2" "$script" run.json < /dev/null > capture.log 2>&1 &
printf 'pid\t%s\n' "$!"
printf 'boot\t%s\n' "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
"""

POLL_SH = r"""
printf 'boot\t%s\n' "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
if kill -0 "$1" 2>/dev/null && tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null | grep -q capture; then
  printf 'state\trunning\n'
else printf 'state\tstopped\n'; fi
[ -f "$2/result.json" ] && printf 'result\tyes\n'
exit 0
"""

STOP_SH = r"""
tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null | grep -q capture || exit 1
[ "$(readlink "/proc/$1/cwd")" = "$(cd "$3" 2>/dev/null && pwd)" ] || exit 1
kill -s "$2" -- "-$1" 2>/dev/null || kill -s "$2" "$1"
"""

COLLECT_SH = r"""
cd "$1" || exit 3
printf '%s\n' '--result--'; cat result.json 2>/dev/null
printf '\n%s\n' '--log--'; tail -c 4000 capture.log 2>/dev/null
printf '\n%s\n' '--files--'
for f in recording.*; do [ -f "$f" ] && printf '%s\t%s\n' "$f" "$(wc -c < "$f" | tr -d ' ')"; done
"""

HASH_SH = r"""
cd "$1" || exit 3
if command -v sha256sum >/dev/null 2>&1; then sha256sum -- "$2" | cut -d' ' -f1; else echo none; fi
"""

REMOVE_SH = r"""
case $1 in */recordings/*) rm -rf -- "$1" ;; *) exit 2 ;; esac
"""


def _fields(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if parts and parts[0]:
            out[parts[0]] = parts[1:]
    return out


TRANSFER_POLICIES = ("after_recording", "keep_on_host")


def exec_config(row: dict[str, Any]) -> dict[str, str]:
    """A host's recording settings (inv_hosts.exec_config), validated: python, work_dir and transfer.

    transfer after_recording (default): every recording is copied here, verified, then removed on the host.
    transfer keep_on_host: recordings stay on the host (size and SHA-256 recorded) until someone transfers
    them or retention removes them; nothing multi-gigabyte crosses the network unless it is wanted."""
    cfg = dict(row.get("exec_config") or {})
    py = str(cfg.get("python") or "python3")
    work = str(cfg.get("work_dir") or ".starlab").rstrip("/")
    transfer = str(cfg.get("transfer") or "after_recording")
    if transfer not in TRANSFER_POLICIES:
        raise BackendError(f"host {row['id']}: transfer must be one of {', '.join(TRANSFER_POLICIES)}", "bad_config")
    if not PYTHON_RE.match(py):
        raise BackendError(f"host {row['id']}: python must be a program name or path, not {py!r}", "bad_config")
    if not WORK_DIR_RE.match(work) or ".." in pathlib.PurePosixPath(work).parts:
        raise BackendError(f"host {row['id']}: work_dir must be a plain path without '..' ({work!r})", "bad_config")
    return {"python": py, "work_dir": work, "transfer": transfer}


class SshBackend:
    kind = "ssh"

    def __init__(self, scheduler: Any, hosts: Any, host_id: str, fetch_timeout_s: float = 3600):
        self.s, self.hosts, self.host_id, self.fetch_timeout_s = scheduler, hosts, host_id, fetch_timeout_s
        row = hosts.get(host_id)
        self.cfg = exec_config(row)
        self.host = hosts.host(host_id)
        src = pathlib.Path(__file__).with_name("capture.py").read_bytes()
        self.capture_src = src.decode()
        self.capture_path = f"{self.cfg['work_dir']}/bin/capture-{hashlib.sha256(src).hexdigest()[:12]}.py"

    def _dir(self, job_id: str) -> str:
        if not JOB_ID_RE.match(job_id):
            raise BackendError(f"bad job id {job_id!r}", "bad_request")
        return f"{self.cfg['work_dir']}/recordings/{job_id}"

    def _sh(self, script: str, *args: str, input: str | None = None, timeout: float = 30) -> Any:
        return self.host.run(["sh", "-c", script, "sh", *args], input=input, timeout=timeout)

    @staticmethod
    def _guard(req: RecordingRequest) -> None:
        why = hardware_blocked()
        if why and not req.simulated:
            raise BackendError(f"not recording {req.receiver_id} on host {req.host}: {why}", "blocked")

    # -------------------------------------------------------------- checks
    def preflight(self, req: RecordingRequest, rx: Receiver, diag: Diagnostics) -> bool:
        try:
            self._guard(req)
        except BackendError as exc:
            return diag.check("allowed", False, str(exc), exc.code)
        r = self._sh(PREFLIGHT_SH, self.cfg["work_dir"], self.cfg["python"], req.driver, "rtl_sdr", timeout=60)
        if r.error_kind or (r.returncode != 0 and "boot" not in r.stdout):
            text = (r.error or r.stderr or f"exit {r.returncode}").strip()
            code = SSH_CODES.get(r.error_kind or "")
            e = (message_for(code, rx.id) if code else None) or explain(text, rx.id)
            return diag.check("connected", False, f"host {self.host_id} ({self.host.label}): {e['message']} ({text})",
                              e["code"])
        f = _fields(r.stdout)
        diag.check("connected", True, f"host {self.host_id} ({self.host.label}) answered")
        if (f.get("writable") or ["no"])[0] != "yes":
            why = (f.get("writable") or ["", ""])[-1]
            return diag.check("storage", False, f"cannot write {self.cfg['work_dir']}/recordings on {self.host_id}: "
                              f"{why or 'permission denied'}", "permission")
        try:
            free = int((f.get("free_kb") or ["x"])[0]) * 1024
        except ValueError:
            free = None
        if not storage_check(diag, f"{self.host_id}:{self.cfg['work_dir']}", free, req.estimated_bytes + STORAGE_MARGIN):
            return False
        try:
            here = self.s._disk_free(str(pathlib.Path(req.raw).parent))
        except OSError:
            here = None
        if not storage_check(diag, f"{pathlib.Path(req.raw).parent} (this computer, for the copy)", here,
                             req.estimated_bytes + STORAGE_MARGIN):
            return False
        py = (f.get("python") or ["missing"])[0]
        if py == "missing" or not re.match(r"^3\.\d+$", py):
            return diag.check("recorder installed", False, f"{self.cfg['python']} is not available on {self.host_id}: "
                              f"{py}", "missing_tool")
        if (f.get("modules") or ["missing"])[0] != "ok":
            text = (f.get("modules") or ["", ""])[-1]
            e = explain(text, rx.id)
            return diag.check("recorder installed", False, f"{e['message']} ({text})", e["code"])
        if req.driver == "rtlsdr" and (f.get("tool") or ["missing"])[0] != "ok":
            return diag.check("recorder installed", False, f"rtl_sdr is not installed on {self.host_id}", "missing_tool")
        diag.check("recorder installed", True, f"Python {py} with the needed modules on {self.host_id}")
        return self._availability(rx, diag)

    def _availability(self, rx: Receiver, diag: Diagnostics) -> bool:
        if rx.simulated:
            return diag.check("not in use", True, "simulated receiver")
        from ..inventory import snapshot
        from ..inventory.discovery import receiver_like, usage
        try:
            snap = snapshot.take(self.host, timeout=60)
        except snapshot.SnapshotError as exc:
            return diag.check("not in use", None, f"could not read {self.host_id}'s USB state: {exc}")
        dev = _find(receiver_like(snap.devices), rx)
        if dev is None:
            return diag.check("connected", False, f"{rx.id} (serial {rx.serial}) is not on {self.host_id}'s USB",
                              "device_missing")
        u = usage(dev, holders=lambda node, _proc: snap.holders_of(node), mine=False)
        if u["managed_by_api"]:
            return diag.check("not in use", None, u["detail"])
        if not u["free"]:
            return diag.check("not in use", False, f"{u['detail']} on {self.host_id}",
                              "usb_claimed" if u["claimed"] else "busy")
        return diag.check("not in use", True, f"no other program has it open on {self.host_id}")

    # -------------------------------------------------------------- running
    def start(self, req: RecordingRequest) -> dict[str, Any]:
        self._guard(req)
        d = self._dir(req.job_id)
        r = self._sh(DEPLOY_SH, self.capture_path, input=self.capture_src, timeout=60)
        if r.returncode != 0:
            raise BackendError(f"could not install the recorder on {self.host_id}: {r.error or r.stderr.strip()}",
                               r.error_kind or "deploy")
        run = req.run_doc(f"recording.{req.format}", "result.json")
        r = self._sh(WRITE_RUN_SH, d, input=json.dumps(run, indent=2), timeout=60)
        if r.returncode != 0:
            raise BackendError(f"could not write the run file on {self.host_id}: {r.error or r.stderr.strip()}",
                               r.error_kind or "write")
        r = self._sh(START_SH, d, self.cfg["python"], self.capture_path, timeout=60)
        f = _fields(r.stdout)
        pid = (f.get("pid") or [""])[0]
        if r.returncode != 0 or not pid.isdigit():
            raise BackendError(f"could not start the recorder on {self.host_id}: {r.error or r.stderr.strip() or r.stdout}",
                               r.error_kind or "start")
        return {"backend": self.kind, "host": self.host_id, "pid": int(pid), "job_id": req.job_id, "dir": d,
                "boot_id": (f.get("boot") or [""])[0]}

    def poll(self, handle: dict[str, Any]) -> str:
        r = self._sh(POLL_SH, str(int(handle["pid"])), handle["dir"], timeout=30)
        if r.error_kind or r.returncode != 0:
            return "unreachable"
        f = _fields(r.stdout)
        boot = (f.get("boot") or [""])[0]
        if handle.get("boot_id") and boot and boot != handle["boot_id"]:
            return "rebooted"
        return "running" if (f.get("state") or [""])[0] == "running" else "finished"

    def stop(self, handle: dict[str, Any], hard: bool = False) -> bool:
        r = self._sh(STOP_SH, str(int(handle["pid"])), "KILL" if hard else "TERM", handle["dir"], timeout=30)
        return r.returncode == 0

    def collect(self, handle: dict[str, Any], req: RecordingRequest) -> dict[str, Any]:
        d = handle["dir"]
        r = self._sh(COLLECT_SH, d, timeout=60)
        if r.error_kind or r.returncode != 0:
            raise BackendError(f"could not read the result from {self.host_id}: {r.error or r.stderr.strip()}",
                               r.error_kind or "collect")
        text = r.stdout
        res_text = text.split("--result--\n", 1)[-1].split("\n--log--\n", 1)[0]
        log_text = text.split("\n--log--\n", 1)[-1].split("\n--files--\n", 1)[0]
        files = {}
        for line in text.split("\n--files--\n", 1)[-1].splitlines():
            name, _, size = line.partition("\t")
            if name in (f"recording.{req.format}", f"recording.{req.format}.part") and size.isdigit():
                files[name] = int(size)
        try:
            pathlib.Path(req.log).parent.mkdir(parents=True, exist_ok=True)
            with open(req.log, "a") as lf:
                lf.write(f"--- recorder log from {self.host_id}:{d} ---\n{log_text}\n")
        except OSError:
            pass
        try:
            capture = json.loads(res_text)
        except ValueError:
            capture = {"status": "failed", "error": "capture process ended without writing a result"}
        capture["remote"] = {"host": self.host_id, "dir": d}
        name = f"recording.{req.format}" if f"recording.{req.format}" in files else \
            (f"recording.{req.format}.part" if f"recording.{req.format}.part" in files else None)
        if name is None:
            capture["file"] = None
            return capture
        if self.cfg.get("transfer") == "keep_on_host":
            h = self._sh(HASH_SH, d, name, timeout=600)
            digest = h.stdout.strip() if h.returncode == 0 and not h.error_kind else ""
            capture["file"] = None
            capture["remote_file"] = {"host": self.host_id, "path": f"{d}/{name}", "dir": d, "bytes": files[name],
                                      "sha256": digest if digest not in ("", "none") else None,
                                      "partial": name.endswith(".part")}
            return capture
        dest = req.raw if not name.endswith(".part") else req.raw + ".part"
        got = self._fetch(d, name, files[name], pathlib.Path(dest))
        capture["file"] = dest
        capture["transfer"] = got
        if got["ok"]:
            self._sh(REMOVE_SH, d, timeout=60)          # the copy here is verified; remove the one on the host
        else:
            capture.update(status="failed", error=f"the recording could not be copied from {self.host_id}: {got['error']}")
        return capture

    def remove_recording(self, job_id: str) -> None:
        """Delete a recording's folder on the host (retention, or after a verified transfer)."""
        r = self._sh(REMOVE_SH, self._dir(job_id), timeout=60)
        if r.error_kind or r.returncode != 0:
            raise BackendError(f"could not remove {job_id} on {self.host_id}: {r.error or r.stderr.strip() or r.returncode}",
                               r.error_kind or "remove")

    def _fetch(self, d: str, name: str, size: int, dest: pathlib.Path) -> dict[str, Any]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name("." + dest.name + ".transfer")
        h = hashlib.sha256()
        t0 = time.monotonic()
        try:
            with open(tmp, "wb") as out:
                proc = subprocess.Popen(self.host.command(["cat", "--", f"{d}/{name}"]), stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
                assert proc.stdout is not None
                while True:
                    if time.monotonic() - t0 > self.fetch_timeout_s:
                        proc.kill()
                        raise BackendError("the copy took too long", "timeout")
                    chunk = proc.stdout.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                rc = proc.wait(timeout=60)
            got = tmp.stat().st_size
            if rc != 0:
                raise BackendError(f"copy failed (exit {rc}): {err.strip()[-200:]}", "transfer")
            if got != size:
                raise BackendError(f"copied {got} of {size} bytes", "transfer")
            r = self._sh(HASH_SH, d, name, timeout=600)
            remote_hash = r.stdout.strip()
            if r.returncode == 0 and remote_hash != "none" and remote_hash != h.hexdigest():
                raise BackendError("the copy's SHA-256 differs from the file on the host", "transfer")
            os.replace(tmp, dest)
            return {"ok": True, "bytes": got, "sha256": h.hexdigest(),
                    "verified": "size and SHA-256" if remote_hash not in ("", "none") else "size",
                    "seconds": round(time.monotonic() - t0, 1)}
        except (OSError, BackendError, subprocess.SubprocessError) as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            return {"ok": False, "error": str(exc)}


def _find(devices: list[Any], rx: Receiver) -> Any:
    for d in devices:
        if rx.identity == "usb_path" and rx.usb_path:
            if d.usb_path == rx.usb_path:
                return d
        elif d.serial == rx.serial:
            return d
    return None


# ------------------------------------------------------------------ future receiver agents
class AgentBackend:
    """A small Starlab service on the receiver's computer that takes RecordingRequests over HTTPS (not built yet).

    It will take the same request and return the same result as the other backends, so the runner does not
    change when it arrives. Until then, booking a receiver on an agent host fails clearly at the start check."""
    kind = "agent"

    def __init__(self, host_id: str):
        self.host_id = host_id

    def preflight(self, req: RecordingRequest, rx: Receiver, diag: Diagnostics) -> bool:
        return diag.check("connected", False, f"{rx.id} is on {self.host_id}, a receiver-agent host; receiver agents "
                          "are not available in this version of Starlab", "unsupported")

    def start(self, req: RecordingRequest) -> dict[str, Any]:
        raise BackendError("receiver agents are not available in this version of Starlab", "unsupported")

    def poll(self, handle: dict[str, Any]) -> str:
        return "lost"

    def stop(self, handle: dict[str, Any], hard: bool = False) -> bool:
        return False

    def collect(self, handle: dict[str, Any], req: RecordingRequest) -> dict[str, Any]:
        return {"status": "failed", "error": "receiver agents are not available"}


# ------------------------------------------------------------------ choosing a backend
class Backends:
    """The backend for each receiver: local, or the SSH backend of the host it is plugged into."""

    def __init__(self, scheduler: Any, local: LocalBackend, hosts: Any = None):
        self.s, self.local, self.hosts = scheduler, local, hosts
        self._ssh: dict[str, SshBackend] = {}
        self._lock = threading.Lock()

    def for_receiver(self, rx: Receiver) -> Any:
        return self.for_host(rx.host)

    def for_host(self, host_id: str) -> Any:
        if host_id == LOCAL:
            return self.local
        if self.hosts is None:
            raise BackendError(f"receiver host {host_id} is not configured on this broker", "bad_config")
        row = self.hosts.get(host_id)
        if (row.get("exec_config") or {}).get("backend") == "agent":
            return AgentBackend(host_id)
        with self._lock:
            b = self._ssh.get(host_id)
            if b is None or b.host.hostname != row["hostname"] or b.host.port != row["port"] \
                    or b.cfg != exec_config(row):
                b = self._ssh[host_id] = SshBackend(self.s, self.hosts, host_id)
            return b

    def for_handle(self, handle: dict[str, Any]) -> Any:
        return self.local if handle.get("backend") == "local" else self.for_host(handle["host"])


# ------------------------------------------------------------------ remote USB state for presence
class RemoteDevices:
    """Is a remote receiver on its host's USB? One snapshot per host, cached (the status page asks often)."""

    def __init__(self, hosts: Any, ttl_s: float = 60, fail_ttl_s: float = 30, clock: Callable[[], float] = time.monotonic):
        self.hosts, self.ttl_s, self.fail_ttl_s, self.clock = hosts, ttl_s, fail_ttl_s, clock
        self._cache: dict[str, tuple[float, Any, str]] = {}
        self._lock = threading.Lock()

    def _snapshot(self, host_id: str) -> tuple[Any, str]:
        from ..inventory import snapshot
        now = self.clock()
        with self._lock:
            hit = self._cache.get(host_id)
            if hit and now - hit[0] < (self.ttl_s if hit[1] is not None else self.fail_ttl_s):
                return hit[1], hit[2]
        try:
            snap, err = snapshot.take(self.hosts.host(host_id), timeout=30), ""
        except Exception as exc:  # noqa: BLE001 - unreachable, disabled or deleted host
            snap, err = None, str(exc)
        with self._lock:
            self._cache[host_id] = (now, snap, err)
        return snap, err

    def presence(self, rx: Receiver) -> Any:
        from ..inventory.discovery import receiver_like
        from .presence import ABSENT, PRESENT, UNKNOWN, Presence
        snap, err = self._snapshot(rx.host)
        if snap is None:
            return Presence(UNKNOWN, f"host {rx.host} could not be reached: {err}", "host_unreachable")
        dev = _find(receiver_like(snap.devices), rx)
        if dev is not None:
            return Presence(PRESENT, f"serial {rx.serial} on {rx.host}'s USB")
        if rx.driver == "sdrplay" and any(d.vid == "1df7" for d in snap.devices):
            return Presence(UNKNOWN, f"an SDRplay device is on {rx.host}'s USB; its serial is not readable there",
                            "discovery_failed")
        return Presence(ABSENT, f"serial {rx.serial} not on {rx.host}'s USB")

    def forget(self, host_id: str | None = None) -> None:
        with self._lock:
            if host_id is None:
                self._cache.clear()
            else:
                self._cache.pop(host_id, None)


def device_log_errors(log_path: str) -> list[str]:
    return device_errors(log_tail(log_path, 64_000))
