"""Receiver broker: lends receivers to the scanning agent without breaking what already runs.

Rules it enforces:
  * One lease per receiver, with a TTL. The watchdog releases an expired lease
    and restores everything it changed.
  * The broker also takes a lease in the legacy coordinator, so the satellite
    recorder and other coordinator-aware tools see the receiver as busy.
  * OBS-R05 shares its dongle with ADS-B (observatory-adsb, dump1090). Leasing
    R05 stops ADS-B, and releasing it puts the antenna switch back on the port
    ADS-B had and restarts ADS-B on whatever librtlsdr index R05 now has.
  * Every state change is persisted, so after a crash or restart the broker
    restores the switch and ADS-B before serving anything else.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import secrets
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol

from ..config import Config
from ..logjson import log_event
from ..timeutil import iso, parse_utc, utc_now
from . import radio

LOG = logging.getLogger("aobs.broker")
MAX_TTL_S = 3600


class BrokerError(RuntimeError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class Hardware(Protocol):
    def switch_state(self) -> dict[str, Any]: ...
    def switch_select(self, port: int) -> None: ...
    def adsb_active(self) -> bool: ...
    def adsb_stop(self) -> None: ...
    def adsb_start(self, device_index: int) -> None: ...
    def device_index(self, serial: str) -> int | None: ...
    def device_busy(self, serial: str) -> list[str]: ...
    def coordinator_reserve(self, receiver: str, owner: str, ttl_s: int, purpose: str) -> str: ...
    def coordinator_release(self, receiver: str, token: str) -> None: ...
    def popen(self, cmd: list[str], stdout: Any) -> subprocess.Popen[bytes]: ...


@dataclass
class Lease:
    receiver: str
    token: str
    owner: str
    purpose: str
    created_at: str
    expires_at: str
    coord_token: str
    prior_port: int | None = None
    port: int | None = None
    adsb_was_active: bool = False
    ops: int = 0

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("coord_token")
        return d


@dataclass
class _Running:
    proc: subprocess.Popen[bytes] | None = None
    what: str = ""
    started: float = field(default_factory=time.monotonic)


class Broker:
    def __init__(self, config: Config, hw: Hardware, clock: Callable[[], dt.datetime] = utc_now):
        b = config.section("broker")
        sw = config.section("antenna_switch")
        self.hw = hw
        self.clock = clock
        self.receivers = list(b.get("receivers", ["OBS-R05"]))
        self.adsb_serial = str(config.section("legacy").get("adsb_serial", "OBS-R05"))
        self.switch_receiver = str(sw.get("receiver", "OBS-R05"))
        self.ports = {int(p["port"]): p for p in sw.get("ports", []) if p.get("antenna")}
        self.state_path = config.data_root / "broker" / "lease.json"
        self.capture_dir = config.data_root / "captures"
        self.retention = dt.timedelta(hours=float(b.get("capture_retention_hours", 72)))
        self._lock = threading.RLock()
        self._op_lock = threading.Lock()
        self._leases: dict[str, Lease] = {}
        self._running = _Running()
        # (receiver, now) -> start of the next scheduled reservation; ad-hoc leases must end before it
        self.reservation_guard: Callable[[str, dt.datetime], dt.datetime | None] | None = None

    # ------------------------------------------------------------ persistence
    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({r: asdict(l) for r, l in self._leases.items()}, indent=2))
        os.replace(tmp, self.state_path)

    def recover(self, keep: Callable[[Lease], bool] | None = None) -> list[str]:
        """Called once at startup: restore anything a previous broker process left leased.

        A lease that `keep` accepts (a scheduled recording that is still running) is kept as is, so
        ADS-B is not restarted onto a dongle that is mid-recording; the runner releases it when done.
        """
        if not self.state_path.exists():
            return []
        try:
            saved = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            saved = {}
        restored = []
        with self._lock:
            for receiver, data in saved.items():
                lease = Lease(**data)
                self._leases[receiver] = lease
                if keep is not None and keep(lease):
                    lease.expires_at = iso(self.clock() + dt.timedelta(seconds=MAX_TTL_S))
                    log_event(LOG, "kept lease of a running recording", receiver=receiver, owner=lease.owner)
                    continue
                self._release_locked(receiver, "broker restart")
                restored.append(receiver)
            self._save()
        return restored

    def lease_token_for(self, owner: str) -> str | None:
        with self._lock:
            return next((l.token for l in self._leases.values() if l.owner == owner), None)

    # ------------------------------------------------------------ leases
    def lease(self, owner: str, purpose: str = "", ttl_s: int = 600, receiver: str = "OBS-R05",
              port: int | None = None) -> dict[str, Any]:
        if receiver not in self.receivers:
            raise BrokerError(f"{receiver} is not lendable (broker.receivers = {self.receivers})", 403)
        ttl_s = self._cap_ttl(owner, receiver, max(30, min(int(ttl_s), MAX_TTL_S)))
        with self._lock:
            current = self._leases.get(receiver)
            if current:
                if current.owner != owner:
                    raise BrokerError(f"{receiver} is leased by {current.owner} until {current.expires_at}")
                return self._renew_locked(current, ttl_s)
            if port is not None:
                self._check_port(receiver, port)
            coord_token = self.hw.coordinator_reserve(receiver, f"aobs-broker:{owner}", ttl_s + 60, purpose)
            lease = Lease(receiver=receiver, token=secrets.token_urlsafe(18), owner=owner, purpose=purpose,
                          created_at=iso(self.clock()), expires_at=iso(self.clock() + dt.timedelta(seconds=ttl_s)),
                          coord_token=coord_token)
            self._leases[receiver] = lease
            self._save()  # persist before touching hardware so a crash mid-way still gets undone
            try:
                if receiver == self.switch_receiver:
                    lease.prior_port = self.hw.switch_state().get("port")
                if receiver == self.adsb_serial:
                    lease.adsb_was_active = self.hw.adsb_active()
                    self._save()
                    if lease.adsb_was_active:
                        self.hw.adsb_stop()
                self._wait_free(receiver)
                if port is not None:
                    self.hw.switch_select(port)
                    lease.port = port
                self._save()
            except Exception:
                self._release_locked(receiver, "lease setup failed")
                raise
            log_event(LOG, "lease granted", receiver=receiver, owner=owner, purpose=purpose, ttl_s=ttl_s,
                      prior_port=lease.prior_port, port=port, adsb_paused=lease.adsb_was_active)
            return lease.public()

    def _cap_ttl(self, owner: str, receiver: str, ttl_s: int) -> int:
        """Scheduled recordings outrank ad-hoc leases: an ad-hoc lease must end 30 s before the next reservation."""
        if owner.startswith("scheduler:") or self.reservation_guard is None:
            return ttl_s
        now = self.clock()
        nxt = self.reservation_guard(receiver, now)
        if nxt is None:
            return ttl_s
        room = int((nxt - now).total_seconds()) - 30
        if room < 30:
            raise BrokerError(f"{receiver} is reserved for a scheduled recording starting {iso(nxt)}")
        return min(ttl_s, room)

    def renew(self, token: str, ttl_s: int = 600) -> dict[str, Any]:
        with self._lock:
            lease = self._by_token(token)
            return self._renew_locked(lease, self._cap_ttl(lease.owner, lease.receiver,
                                                           max(30, min(int(ttl_s), MAX_TTL_S))))

    # ------------------------------------------------------------ scheduled jobs
    def take_for_job(self, receiver: str, job_id: str, port: int | None, until: dt.datetime) -> str:
        """Lease a receiver for a scheduled job, ending any ad-hoc lease on it first (jobs have priority)."""
        with self._lock:
            current = self._leases.get(receiver)
            if current and not current.owner.startswith("scheduler:"):
                self._release_locked(receiver, f"scheduled job {job_id} needs {receiver}")
            ttl = int(min(MAX_TTL_S, (until - self.clock()).total_seconds() + 120))
            lease = self.lease(f"scheduler:{job_id}", f"scheduled job {job_id}", ttl, receiver, port)
            return str(self._leases[receiver].token if lease else "")

    def renew_for_job(self, token: str, until: dt.datetime) -> None:
        self.renew(token, int(min(MAX_TTL_S, (until - self.clock()).total_seconds() + 120)))

    def release_for_job(self, token: str) -> None:
        self.release(token, "scheduled job finished")

    def _renew_locked(self, lease: Lease, ttl_s: int) -> dict[str, Any]:
        lease.expires_at = iso(self.clock() + dt.timedelta(seconds=ttl_s))
        self.hw.coordinator_reserve(lease.receiver, f"aobs-broker:{lease.owner}", ttl_s + 60, lease.purpose)
        self._save()
        return lease.public()

    def release(self, token: str, reason: str = "released by owner") -> dict[str, Any]:
        with self._lock:
            lease = self._by_token(token)
            return self._release_locked(lease.receiver, reason)

    def _release_locked(self, receiver: str, reason: str) -> dict[str, Any]:
        lease = self._leases.get(receiver)
        if lease is None:
            return {"released": False}
        self._stop_running()
        problems = []
        if receiver == self.switch_receiver and lease.prior_port is not None and lease.port not in (None, lease.prior_port):
            try:
                self.hw.switch_select(lease.prior_port)
            except Exception as exc:  # noqa: BLE001 - keep restoring the rest
                problems.append(f"switch restore failed: {exc}")
        if receiver == self.adsb_serial and lease.adsb_was_active:
            try:
                index = self.hw.device_index(self.adsb_serial)
                if index is None:
                    raise BrokerError(f"{self.adsb_serial} is not on USB")
                self.hw.adsb_start(index)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"ADS-B restart failed: {exc}")
        try:
            self.hw.coordinator_release(receiver, lease.coord_token)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"coordinator release failed: {exc}")
        del self._leases[receiver]
        self._save()
        log_event(LOG, "lease released", logging.ERROR if problems else logging.INFO, receiver=receiver,
                  owner=lease.owner, reason=reason, ops=lease.ops, problems=problems)
        return {"released": True, "reason": reason, "problems": problems}

    def expire(self) -> list[str]:
        """Watchdog tick: release expired leases, prune old captures."""
        now = self.clock()
        expired = []
        with self._lock:
            for receiver, lease in list(self._leases.items()):
                if parse_utc(lease.expires_at) <= now:
                    self._release_locked(receiver, "lease expired")
                    expired.append(receiver)
        self._prune(now)
        return expired

    def _by_token(self, token: str) -> Lease:
        for lease in self._leases.values():
            if secrets.compare_digest(lease.token, token):
                return lease
        raise BrokerError("Unknown or expired lease token", 404)

    def _check_port(self, receiver: str, port: int) -> None:
        if receiver != self.switch_receiver:
            raise BrokerError(f"The antenna switch feeds {self.switch_receiver}, not {receiver}", 400)
        if port not in self.ports:
            raise BrokerError(f"Port {port} has no antenna; valid ports: {sorted(self.ports)}", 400)

    def _wait_free(self, receiver: str, timeout_s: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            busy = self.hw.device_busy(receiver)
            if not busy:
                return
            if time.monotonic() >= deadline:
                raise BrokerError(f"{receiver} is still held by: {', '.join(busy)}")
            time.sleep(0.5)

    # ------------------------------------------------------------ operations
    def select_port(self, token: str, port: int) -> dict[str, Any]:
        with self._lock:
            lease = self._by_token(token)
            self._check_port(lease.receiver, port)
        with self._op_lock:
            self.hw.switch_select(port)
            with self._lock:
                lease.port = port
                self._save()
        log_event(LOG, "antenna selected", receiver=lease.receiver, port=port, antenna=self.ports[port]["antenna"])
        return {"port": port, **self.ports[port]}

    def _begin_op(self, token: str, seconds: float) -> Lease:
        with self._lock:
            lease = self._by_token(token)
            need = self.clock() + dt.timedelta(seconds=seconds + 30)
            if parse_utc(lease.expires_at) < need:
                self._renew_locked(lease, int(seconds + 30))
            lease.ops += 1
            return lease

    def _run(self, cmd: list[str], seconds: float, stdout: Any, what: str, kill_after: bool) -> tuple[int, str]:
        """Run a radio command. kill_after=True stops it after `seconds` (rtl_fm has no duration flag)."""
        proc = self.hw.popen(cmd, stdout)
        with self._lock:
            self._running = _Running(proc, what)
        try:
            try:
                proc.wait(timeout=seconds if kill_after else seconds + 30)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            err = proc.stderr.read().decode(errors="replace")[-2000:] if proc.stderr else ""
            return proc.returncode, err
        finally:
            with self._lock:
                self._running = _Running()

    def _stop_running(self) -> None:
        proc = self._running.proc
        if proc and proc.poll() is None:
            proc.terminate()

    def _capture_path(self, ext: str) -> tuple[str, pathlib.Path]:
        now = self.clock()
        cid = f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        path = self.capture_dir / f"{now:%Y-%m-%d}" / f"{cid}.{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return cid, path

    def sweep(self, token: str, spec: radio.Sweep) -> dict[str, Any]:
        spec.validate()
        lease = self._begin_op(token, spec.seconds)
        with self._op_lock:
            cid, path = self._capture_path("csv")
            with path.open("wb") as out:
                rc, err = self._run(spec.command(lease.receiver), spec.seconds, out, "sweep", kill_after=False)
            bins = radio.parse_rtl_power(path.read_text())
        if not bins:
            raise BrokerError(f"rtl_power produced no data (exit {rc}): {err.strip()[-300:]}", 502)
        summary = radio.find_peaks(bins)
        log_event(LOG, "sweep", receiver=lease.receiver, port=lease.port, start_hz=spec.start_hz,
                  stop_hz=spec.stop_hz, bins=len(bins), peaks=len(summary["peaks"]))
        return {"id": cid, "receiver": lease.receiver, "antenna": self._antenna(lease), "at": iso(self.clock()),
                "bins": len(bins), "bin_hz": spec.bin_hz, **summary, "file": f"{path.parent.name}/{path.name}"}

    def audio(self, token: str, spec: radio.AudioCapture) -> dict[str, Any]:
        spec.validate()
        lease = self._begin_op(token, spec.seconds)
        with self._op_lock:
            cid, path = self._capture_path("wav")
            raw = path.with_suffix(".raw")
            with raw.open("wb") as out:
                rc, err = self._run(spec.command(lease.receiver), spec.seconds, out, "audio", kill_after=True)
            seconds = radio.write_wav(str(path), raw.read_bytes(), spec.wav_rate)
            raw.unlink()
        if seconds < 0.5:
            if spec.squelch and rc in (0, -15) and "Tuned to" in err:
                # Squelch held the whole time: rtl_fm ran fine, the channel was just quiet.
                path.unlink(missing_ok=True)
                return {"id": cid, "receiver": lease.receiver, "antenna": self._antenna(lease), "freq_hz": spec.freq_hz,
                        "mode": spec.mode, "seconds": 0.0, "squelch_open": False, "at": iso(self.clock()), "file": None}
            raise BrokerError(f"rtl_fm produced no audio (exit {rc}): {err.strip()[-300:]}", 502)
        log_event(LOG, "audio capture", receiver=lease.receiver, freq_hz=spec.freq_hz, mode=spec.mode, seconds=round(seconds, 1))
        return {"id": cid, "receiver": lease.receiver, "antenna": self._antenna(lease), "freq_hz": spec.freq_hz,
                "mode": spec.mode, "seconds": round(seconds, 2), "gain_db": spec.gain_db, "bandwidth_hz": spec.rate,
                "squelch_open": True, "at": iso(self.clock()), "file": f"{path.parent.name}/{path.name}"}

    def iq(self, token: str, spec: radio.IqCapture) -> dict[str, Any]:
        spec.validate()
        lease = self._begin_op(token, spec.seconds)
        with self._op_lock:
            cid, path = self._capture_path("cu8")
            rc, err = self._run(spec.command(lease.receiver, str(path)), spec.seconds, subprocess.DEVNULL, "iq",
                                kill_after=False)
        size = path.stat().st_size if path.exists() else 0
        if size == 0:
            raise BrokerError(f"rtl_sdr produced no samples (exit {rc}): {err.strip()[-300:]}", 502)
        return {"id": cid, "receiver": lease.receiver, "antenna": self._antenna(lease), "freq_hz": spec.freq_hz,
                "sample_rate": spec.sample_rate, "bytes": size, "format": "cu8", "at": iso(self.clock()),
                "file": f"{path.parent.name}/{path.name}"}

    def capture_file(self, rel: str) -> pathlib.Path:
        path = (self.capture_dir / rel).resolve()
        if self.capture_dir.resolve() not in path.parents or not path.is_file():
            raise BrokerError("No such capture", 404)
        return path

    def _antenna(self, lease: Lease) -> str | None:
        port = lease.port if lease.port is not None else lease.prior_port
        return self.ports.get(port, {}).get("antenna") if port is not None else None

    def _prune(self, now: dt.datetime) -> None:
        if not self.capture_dir.exists():
            return
        cutoff = (now - self.retention).timestamp()
        for path in self.capture_dir.glob("*/*"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    # ------------------------------------------------------------ status
    def state(self) -> dict[str, Any]:
        with self._lock:
            leases = {r: {k: v for k, v in l.public().items() if k != "token"} for r, l in self._leases.items()}
            running = {"what": self._running.what, "for_s": round(time.monotonic() - self._running.started, 1)} \
                if self._running.proc else None
        try:
            sw = self.hw.switch_state()
            switch = {"port": sw.get("port"), "antenna": self.ports.get(sw.get("port"), {}).get("antenna"),
                      "healthy": sw.get("healthy")}
        except Exception as exc:  # noqa: BLE001
            switch = {"error": str(exc)}
        try:
            adsb = self.hw.adsb_active()
        except Exception as exc:  # noqa: BLE001
            adsb = f"unknown: {exc}"
        return {"at": iso(self.clock()), "lendable": self.receivers, "leases": leases, "running": running,
                "switch": switch, "adsb_active": adsb,
                "ports": [{"port": p, "antenna": v["antenna"], "label": v.get("label")} for p, v in sorted(self.ports.items())]}
