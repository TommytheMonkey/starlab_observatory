"""Listening sessions: lend one receiver to SDR++ server, with the same ownership rules as a recording.

Starting a session (all checked under the scheduler's booking lock):
  * the receiver is eligible: run by the broker (not an independent legacy recorder), not behind the
    antenna switch, and its backend can isolate it (sdrpp.py);
  * it is on USB, nobody else has it open or claimed, it is not leased, no other session holds it;
  * nothing is recording or reserved on it now, and no booking starts before the session would end.
    With end_before_passes (default on) the session is shortened to end pass_margin_s before the next
    booking instead; if that leaves less than min_minutes it is refused;
  * a legacy coordinator lease is taken too, for receivers the original station's tools know.

While it runs, the watcher (tick, every few seconds):
  * marks it failed if the server exits by itself, and frees the receiver;
  * counts connected clients and ends it after idle_minutes without one;
  * shortens it for bookings made since (end_before_passes), warns warn_minutes ahead, ends it on time.
Bookings and automatic passes see sessions too (store.session_conflicts, planner). Nothing is stopped
except the session's own process group, verified by its session marker (procs.py).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import secrets
import socket
import subprocess
import threading
from typing import Any, Callable

from sqlalchemy import and_, select

from ..timeutil import fmt_clock
from ..db.schema import rx_sessions, sched_jobs
from ..scheduling.store import HOLDING, SchedulerError
from . import procs

LOG = logging.getLogger("aobs.sessions")
ACTIVE = ("starting", "running", "ending")
DEFAULTS = {"enabled": True, "port_range": [5259, 5299], "trusted_addresses": [], "allow_all_interfaces": False,
            "idle_minutes": 30, "max_minutes": 240, "min_minutes": 5, "default_minutes": 60, "pass_margin_s": 120,
            "warn_minutes": 5, "end_before_passes": True}


def interface_addresses() -> dict[str, list[str]]:
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, timeout=5).stdout
        return {i["ifname"]: [a["local"] for a in i.get("addr_info", [])] for i in json.loads(out)}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"lo": ["127.0.0.1"]}


def _row(r: Any) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, dt.datetime) else v) for k, v in dict(r).items()}


class SessionManager:
    def __init__(self, s: Any, backends: dict[str, Any], hw: Any = None,
                 leases: Callable[[], dict[str, Any]] = lambda: {},
                 interfaces: Callable[[], dict[str, list[str]]] = interface_addresses,
                 port_free: Callable[[str, int], bool] | None = None):
        self.s, self.backends, self.hw, self.leases, self.interfaces = s, backends, hw, leases, interfaces
        self.settings = {**DEFAULTS, **s.config.section("sessions")}
        self.port_free = port_free or self._port_free
        self.workroot = s.config.data_root / "sessions"
        self._procs: dict[str, Any] = {}            # Popen handles, to reap exited servers
        self._held: dict[str, str] = {}             # session -> coordinator token
        self._lock = threading.RLock()

    # ------------------------------------------------------------ reading
    def rows(self, active: bool | None = None, receiver: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        q = select(rx_sessions).order_by(rx_sessions.c.started_at.desc()).limit(limit)
        if active is True:
            q = q.where(rx_sessions.c.status.in_(ACTIVE))
        if receiver:
            q = q.where(rx_sessions.c.receiver_id == receiver)
        with self.s.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    def get(self, sid: str) -> dict[str, Any]:
        with self.s.engine.connect() as conn:
            r = conn.execute(select(rx_sessions).where(rx_sessions.c.session_id == sid)).mappings().first()
        if r is None:
            raise SchedulerError(f"no session {sid}", 404)
        return dict(r)

    def public(self, row: dict[str, Any]) -> dict[str, Any]:
        now = self.s.clock()
        d = _row(row)
        end = row.get("ended_at") or now
        d["duration_s"] = int((end - row["started_at"]).total_seconds())
        d["remaining_s"] = max(0, int((row["ends_at"] - now).total_seconds())) if row["status"] in ACTIVE else 0
        host = row["bind_addr"] if row["bind_addr"] not in ("0.0.0.0", "::") else socket.gethostname()
        d["connect_to"] = f"{host}:{row['port']}"
        d["simulated"] = row["backend"] == "sim"
        return d

    def listing(self) -> list[dict[str, Any]]:
        return [self.public(r) for r in self.rows()]

    def allowed_binds(self) -> list[dict[str, str]]:
        out = [{"address": "127.0.0.1", "label": "this computer only (use an SSH tunnel from elsewhere)"}]
        for name, addrs in self.interfaces().items():
            if name.startswith("tailscale"):
                out += [{"address": a, "label": f"VPN ({name}): devices on that private network"} for a in addrs]
        out += [{"address": a, "label": "trusted address (Settings: sessions.trusted_addresses)"}
                for a in self.settings["trusted_addresses"]]
        if self.settings["allow_all_interfaces"]:
            out.append({"address": "0.0.0.0", "label": "every network interface (allowed in the configuration)"})
        seen, uniq = set(), []
        for b in out:
            if b["address"] not in seen:
                seen.add(b["address"])
                uniq.append(b)
        return uniq

    # ------------------------------------------------------------ eligibility and checks
    def _backend(self, rx: Any) -> Any:
        return self.backends["sim" if rx.simulated else "local"]

    def static_reasons(self, rx: Any) -> list[str]:
        out = []
        if not self.settings["enabled"]:
            out.append("listening sessions are turned off on this installation")
        if rx.mode in ("legacy", "shadow"):
            out.append(f"{rx.id} is recorded by an independent service ({rx.legacy_unit or 'legacy recorder'}); "
                       "sessions are only offered for receivers the broker runs")
        if rx.switched:
            out.append(f"{rx.id} is fed through the antenna switch, which a session does not control yet")
        out += self._backend(rx).problems(rx)
        return out

    def next_booking(self, conn: Any, serial: str, after: dt.datetime) -> Any:
        return conn.execute(select(sched_jobs).where(and_(
            sched_jobs.c.device_serial == serial, sched_jobs.c.status.in_(HOLDING),
            sched_jobs.c.reservation_end > after)).order_by(sched_jobs.c.reservation_start).limit(1)).mappings().first()

    def _dynamic_reasons(self, conn: Any, rx: Any, now: dt.datetime) -> list[str]:
        out = []
        if not rx.simulated:
            pres = self.s.presence(rx)
            if pres.connected is not True:
                out.append(f"{rx.id} is not confirmed on USB: {pres.detail}")
            u = self.s.usb_usage(rx)
            if u is not None and not u["free"] and not u.get("managed_by_api"):
                out.append(f"{rx.id} is in use: {u['detail']}")
            busy = []
            hw_busy = getattr(self.hw, "busy", None)
            if hw_busy is not None:
                try:
                    busy = hw_busy(rx)
                except Exception as exc:  # noqa: BLE001
                    busy = [f"could not check ({exc})"]
            if busy:
                out.append(f"{rx.id} is open in another program: {', '.join(map(str, busy))}")
        lease = (self.leases() or {}).get(rx.id)
        if lease:
            out.append(f"{rx.id} is leased to {lease.get('owner')}")
        held = conn.execute(select(rx_sessions.c.session_id).where(and_(
            rx_sessions.c.device_serial == rx.serial, rx_sessions.c.status.in_(ACTIVE)))).first()
        if held:
            out.append(f"{rx.id} is already in session {held.session_id}")
        nxt = self.next_booking(conn, rx.serial, now)
        if nxt is not None and nxt["reservation_start"] <= now:
            out.append(f"{rx.id} is reserved now by {nxt['target'] or nxt['job_name']} ({nxt['source']}, {nxt['status']})")
        return out

    def eligibility(self, rid: str) -> dict[str, Any]:
        rx = self.s.receivers.get(rid)
        if rx is None:
            raise SchedulerError(f"no receiver {rid}", 404)
        now = self.s.clock()
        reasons = self.static_reasons(rx)
        with self.s.engine.connect() as conn:
            if not reasons:
                reasons += self._dynamic_reasons(conn, rx, now)
            nxt = self.next_booking(conn, rx.serial, now)
        free_until = None
        if nxt is not None and nxt["reservation_start"] > now:
            free_until = (nxt["reservation_start"] - dt.timedelta(seconds=self.settings["pass_margin_s"])).isoformat()
        return {"receiver_id": rid, "eligible": not reasons, "reasons": reasons, "simulated": rx.simulated,
                "backend": self._backend(rx).name, "binds": self.allowed_binds(),
                "defaults": {"minutes": self.settings["default_minutes"], "max_minutes": self.settings["max_minutes"],
                             "end_before_passes": self.settings["end_before_passes"],
                             "warn_minutes": self.settings["warn_minutes"],
                             "idle_minutes": self.settings["idle_minutes"]},
                "next_booking": None if nxt is None else {
                    "name": nxt["target"] or nxt["job_name"], "source": nxt["source"],
                    "reservation_start": nxt["reservation_start"].isoformat()},
                "free_until": free_until}

    def _port_free(self, addr: str, port: int) -> bool:
        with socket.socket(socket.AF_INET6 if ":" in addr else socket.AF_INET) as sock:
            try:
                sock.bind((addr, port))
                return True
            except OSError:
                return False

    def _choose_port(self, conn: Any, bind: str, wanted: int | None) -> int:
        lo, hi = (int(x) for x in self.settings["port_range"])
        used = {r.port for r in conn.execute(select(rx_sessions.c.port).where(rx_sessions.c.status.in_(ACTIVE)))}
        candidates = [wanted] if wanted else range(lo, hi + 1)
        for p in candidates:
            if wanted and not lo <= p <= hi:
                raise SchedulerError(f"port {p} is outside the allowed range {lo}-{hi}", 400)
            if p not in used and self.port_free(bind, p):
                return int(p)
        raise SchedulerError(f"no free port in {lo}-{hi} on {bind}" if not wanted else f"port {wanted} is in use", 409)

    # ------------------------------------------------------------ start / stop
    def start(self, rid: str, actor: str, minutes: int | None = None, bind: str | None = None,
              port: int | None = None, end_before_passes: bool | None = None,
              warn_minutes: int | None = None) -> dict[str, Any]:
        rx = self.s.receivers.get(rid)
        if rx is None:
            raise SchedulerError(f"no receiver {rid}", 404)
        minutes = int(minutes or self.settings["default_minutes"])
        if not self.settings["min_minutes"] <= minutes <= self.settings["max_minutes"]:
            raise SchedulerError(f"a session lasts {self.settings['min_minutes']}-{self.settings['max_minutes']} minutes", 400)
        bind = bind or "127.0.0.1"
        if bind not in {b["address"] for b in self.allowed_binds()}:
            raise SchedulerError(f"{bind} is not an allowed address; allowed: "
                                 f"{', '.join(b['address'] for b in self.allowed_binds())}", 400)
        policy = {"end_before_passes": self.settings["end_before_passes"] if end_before_passes is None
                  else bool(end_before_passes),
                  "warn_minutes": int(warn_minutes if warn_minutes is not None else self.settings["warn_minutes"]),
                  "idle_minutes": int(self.settings["idle_minutes"]), "margin_s": int(self.settings["pass_margin_s"])}
        problems = self.static_reasons(rx)
        if problems:
            raise SchedulerError("Cannot start an SDR++ server on " + rid + ": " + "; ".join(problems), 409)
        with self.s.locked(), self._lock:
            now = self.s.clock()
            with self.s.engine.begin() as conn:
                reasons = self._dynamic_reasons(conn, rx, now)
                if reasons:
                    raise SchedulerError("Not started, to protect the receiver: " + "; ".join(reasons), 409)
                ends = now + dt.timedelta(minutes=minutes)
                note = None
                nxt = self.next_booking(conn, rx.serial, now)
                if nxt is not None:
                    limit = nxt["reservation_start"] - dt.timedelta(seconds=policy["margin_s"])
                    what = f"{nxt['target'] or nxt['job_name']} ({nxt['source']}) at {fmt_clock(nxt['reservation_start'])}"
                    if limit < ends:
                        if not policy["end_before_passes"]:
                            raise SchedulerError(f"Not started: {what} is booked on {rid} before this session would "
                                                 "end. Choose a shorter session, or let it end before passes.", 409)
                        if limit - now < dt.timedelta(minutes=self.settings["min_minutes"]):
                            raise SchedulerError(f"Not started: {what} is booked on {rid}; less than "
                                                 f"{self.settings['min_minutes']} minutes are free before it.", 409)
                        ends, note = limit, f"ends at {fmt_clock(limit)}, before {what}"
                port_ = self._choose_port(conn, bind, port)
                sid = f"s{now:%Y%m%d%H%M%S}{secrets.token_hex(3)}"
                conn.execute(rx_sessions.insert().values(
                    session_id=sid, kind="sdrpp", backend=self._backend(rx).name, receiver_id=rid,
                    device_serial=rx.serial, host="local", status="starting", bind_addr=bind, port=port_, pid=None,
                    pgid=None, started_at=now, ends_at=ends, ended_at=None, end_reason=None, policy=policy,
                    warned_at=None, clients=0, last_client_at=None, created_by=actor[:128],
                    detail={"note": note}, created_at=now, updated_at=now))
            token = None
            if self.hw is not None and hasattr(self.hw, "coordinator_reserve"):
                try:
                    token = self.hw.coordinator_reserve(rx, f"aobs-session:{sid}",
                                                        int((ends - now).total_seconds()) + 300, "SDR++ listening session")
                except Exception as exc:  # noqa: BLE001
                    self._finish(sid, "failed", f"the station coordinator refused the receiver: {exc}")
                    raise SchedulerError(f"Not started: the station coordinator refused {rid}: {exc}", 409) from exc
            if token:
                self._held[sid] = token
            try:
                launched = self._backend(rx).launch(sid, rx, bind, port_, self.workroot / sid,
                                                    int((ends - self.s.clock()).total_seconds()) + 60)
            except Exception as exc:  # noqa: BLE001
                self._release(rx, sid)
                self._finish(sid, "failed", f"the server could not be started: {exc}")
                raise SchedulerError(f"SDR++ server not started: {exc}", 500) from exc
            self._procs[sid] = launched.pop("_proc", None)
            with self.s.engine.begin() as conn:
                conn.execute(rx_sessions.update().where(rx_sessions.c.session_id == sid).values(
                    status="running", pid=launched["pid"], pgid=launched["pgid"], updated_at=self.s.clock(),
                    detail={"note": note, **launched}))
        LOG.info("session %s: %s on %s:%s for %s until %s", sid, rid, bind, port_, actor, ends.isoformat())
        self._notify(sid, f"SDR++ server for {rid} started by {actor} on {bind}:{port_}, until {fmt_clock(ends)}"
                     + (f" ({note})" if note else ""))
        return self.public(self.get(sid))

    def stop(self, sid: str, actor: str, reason: str = "stopped by the owner") -> dict[str, Any]:
        with self._lock:
            row = self.get(sid)
            if row["status"] not in ACTIVE:
                return self.public(row)
            with self.s.engine.begin() as conn:
                conn.execute(rx_sessions.update().where(rx_sessions.c.session_id == sid).values(
                    status="ending", updated_at=self.s.clock()))
            what = procs.stop_group(int(row["pid"]), int(row["pgid"]), sid) if row.get("pid") else "never started"
            self._reap(sid)
            rx = self.s.receivers.get(row["receiver_id"])
            self._release(rx, sid)
            self._finish(sid, "ended", f"{reason} ({actor}); server {what}")
        self._notify(sid, f"SDR++ server for {row['receiver_id']} ended: {reason}")
        return self.public(self.get(sid))

    def _release(self, rx: Any, sid: str) -> None:
        token = self._held.pop(sid, None)
        if token and rx is not None and self.hw is not None:
            try:
                self.hw.coordinator_release(rx, token)
            except Exception:  # noqa: BLE001
                LOG.exception("releasing the coordinator lease of %s failed", sid)

    def _reap(self, sid: str) -> None:
        p = self._procs.pop(sid, None)
        if p is not None:
            try:
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass

    def _finish(self, sid: str, status: str, reason: str) -> None:
        now = self.s.clock()
        with self.s.engine.begin() as conn:
            conn.execute(rx_sessions.update().where(rx_sessions.c.session_id == sid).values(
                status=status, ended_at=now, end_reason=reason[:500], updated_at=now))

    def _notify(self, sid: str, text: str) -> None:
        try:
            with self.s.engine.begin() as conn:
                self.s.notify(conn, "session", None, text, {"session_id": sid}, self.s.clock())
        except Exception:  # noqa: BLE001 - a notification must never break session handling
            LOG.exception("session notification failed")

    def _alive(self, row: dict[str, Any]) -> bool:
        p = self._procs.get(row["session_id"])
        if p is not None and p.poll() is not None:
            return False
        return procs.is_session_process(row.get("pid"), row["session_id"])

    # ------------------------------------------------------------ watcher
    def tick(self) -> None:
        now = self.s.clock()
        for row in self.rows(active=True, limit=100):
            sid = row["session_id"]
            try:
                if row["status"] == "starting":
                    continue
                if not self._alive(row):
                    self._reap(sid)
                    self._release(self.s.receivers.get(row["receiver_id"]), sid)
                    tail = ""
                    log = (row.get("detail") or {}).get("log")
                    if log and pathlib.Path(log).is_file():
                        tail = " ".join(pathlib.Path(log).read_text(errors="replace").strip().splitlines()[-2:])[-200:]
                    self._finish(sid, "failed", "the SDR++ server exited by itself" + (f": {tail}" if tail else ""))
                    self._notify(sid, f"SDR++ server for {row['receiver_id']} stopped unexpectedly; the receiver is free")
                    continue
                vals: dict[str, Any] = {}
                clients = len(procs.established_clients(int(row["port"])))
                vals["clients"] = clients
                if clients:
                    vals["last_client_at"] = now
                since = vals.get("last_client_at") or row.get("last_client_at") or row["started_at"]
                det = dict(row.get("detail") or {})
                if not (det.get("served") or {}).get("confirmed"):
                    det["served"] = self.backends[row["backend"]].served(row)
                    vals["detail"] = det
                policy = row["policy"] or {}
                ends = row["ends_at"]
                with self.s.engine.connect() as conn:
                    nxt = self.next_booking(conn, row["device_serial"], now)
                if nxt is not None:
                    limit = nxt["reservation_start"] - dt.timedelta(seconds=int(policy.get("margin_s", 120)))
                    if limit < ends:
                        # end_before_passes: the session yields. Without it, bookings were refused; a pass that
                        # still arrived (planner, imported window) is skipped loudly by the planner instead.
                        if policy.get("end_before_passes"):
                            ends = max(now, limit)
                            vals["ends_at"] = ends
                            det["note"] = f"ends at {fmt_clock(ends)}, before {nxt['target'] or nxt['job_name']}"
                            vals["detail"] = det
                warn = dt.timedelta(minutes=int(policy.get("warn_minutes", 5)))
                if not row.get("warned_at") and now >= ends - warn and now < ends:
                    vals["warned_at"] = now
                    self._notify(sid, f"SDR++ server for {row['receiver_id']} ends at {fmt_clock(ends)} "
                                      f"({det.get('note') or 'scheduled end'})")
                with self.s.engine.begin() as conn:
                    conn.execute(rx_sessions.update().where(rx_sessions.c.session_id == sid).values(
                        updated_at=now, **vals))
                if now >= ends:
                    self.stop(sid, "starlab", det.get("note") and f"ended for a booking: {det['note']}" or "scheduled end")
                elif now - since >= dt.timedelta(minutes=int(policy.get("idle_minutes", 30))):
                    self.stop(sid, "starlab", f"no client connected for {policy.get('idle_minutes', 30)} minutes")
            except Exception:  # noqa: BLE001 - one session's trouble must not stop the others' handling
                LOG.exception("session %s check failed", sid)

    def recover(self) -> None:
        """Broker start: keep sessions whose server is still running; close the rest so no receiver stays locked."""
        for row in self.rows(active=True, limit=100):
            if row.get("pid") and procs.is_session_process(row["pid"], row["session_id"]):
                LOG.info("session %s still running (pid %s); adopted", row["session_id"], row["pid"])
                continue
            self._finish(row["session_id"], "failed", "stale: the broker restarted and the SDR++ server was gone")

    def start_watcher(self, every_s: float = 3.0) -> None:
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(every_s):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    LOG.exception("session watcher failed")
        threading.Thread(target=loop, name="sessions", daemon=True).start()
        self._stop_watcher = stop

    # ------------------------------------------------------------ for the scheduler
    def conflicts_for_booking(self, serial: str, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
        with self.s.engine.connect() as conn:
            rows = conn.execute(select(rx_sessions).where(and_(
                rx_sessions.c.device_serial == serial, rx_sessions.c.status.in_(ACTIVE),
                rx_sessions.c.started_at < end, rx_sessions.c.ends_at > start))).mappings().all()
        return [dict(r) for r in rows]
