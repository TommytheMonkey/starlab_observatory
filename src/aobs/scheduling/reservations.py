"""Exclusive receiver reservations (brief 7): one Starlab job per physical device at a time.

A reservation is a row in sched_reservations keyed by the device itself (host, driver, and serial or USB
port), not by receiver ID, so two inventory entries that point at the same hardware still exclude each other.
Taking one is a single INSERT against that primary key, which the database makes atomic: when two jobs race,
exactly one insert succeeds.

Every reservation expires (LEASE_S after it was taken or last renewed). The runner renews it while it
prepares and records; if the broker crashes, the lease runs out and the device can be taken again. A
reservation is reclaimed early when its job has already ended (abandoned), and a restarted broker takes over
the reservation of a job it adopts. Expired and abandoned reservations are logged when they are cleared.

Reservations only cover Starlab's own jobs. Other programs (SDR++, dump1090, a person running rtl_sdr) are
detected separately from the device's USB state just before recording (runner preflight).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import secrets
import socket
from typing import Any, Callable

from sqlalchemy import Engine, delete, select, update
from sqlalchemy.exc import IntegrityError

from ..db.schema import sched_jobs, sched_reservations
from ..logjson import log_event
from .hardware import Receiver

LOG = logging.getLogger("aobs.reservations")
LEASE_S = 180                     # a reservation not renewed for this long is free again
RENEW_S = 30
INSTANCE = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"
ENDED = ("completed", "failed", "missed", "cancelled", "skipped", "preempted", "partial", "recorded", "processing")


class ReservationBusy(RuntimeError):
    """The device is reserved by another Starlab job."""

    def __init__(self, message: str, holder: dict[str, Any]):
        super().__init__(message)
        self.holder = holder


def device_key(rx: Receiver) -> str:
    ident = f"port:{rx.usb_path}" if rx.identity == "usb_path" and rx.usb_path else f"sn:{rx.serial}"
    return f"{rx.host}|{rx.driver}|{ident}"


class Reservations:
    def __init__(self, engine: Engine, clock: Callable[[], dt.datetime], lease_s: int = LEASE_S,
                 holder: str = INSTANCE):
        self.engine, self.clock, self.lease_s, self.holder = engine, clock, lease_s, holder

    # ------------------------------------------------------------ reading
    def current(self) -> list[dict[str, Any]]:
        now = self.clock()
        with self.engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(select(sched_reservations)
                                                  .order_by(sched_reservations.c.receiver_id)).mappings()]
        for r in rows:
            r["expired"] = r["expires_at"] <= now
            r["mine"] = r["holder"] == self.holder
            for k in ("acquired_at", "renewed_at", "expires_at"):
                r[k] = r[k].isoformat()
            r.pop("token", None)
        return rows

    def held_by(self, rx: Receiver) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            r = conn.execute(select(sched_reservations).where(sched_reservations.c.device_key == device_key(rx))
                             ).mappings().first()
        if r is None or r["expires_at"] <= self.clock():
            return None
        return dict(r)

    # ------------------------------------------------------------ taking and giving back
    def acquire(self, rx: Receiver, job_id: str, purpose: str = "") -> str:
        """Reserve the device for job_id and return the token, or raise ReservationBusy."""
        key = device_key(rx)
        token = secrets.token_hex(8)
        now = self.clock()
        with self.engine.begin() as conn:
            cur = conn.execute(select(sched_reservations).where(sched_reservations.c.device_key == key)
                               ).mappings().first()
            if cur is not None:
                why = self._stale(conn, cur, job_id, now)
                if why is None:
                    raise ReservationBusy(
                        f"{rx.id} is reserved by Starlab job {cur['job_id']} ({cur['purpose'] or 'recording'}) until "
                        f"{cur['expires_at']:%H:%M:%S}Z unless that job renews it", dict(cur))
                # Delete exactly the row we looked at: if someone else replaced it meanwhile, our insert fails below.
                conn.execute(delete(sched_reservations).where(sched_reservations.c.device_key == key,
                                                              sched_reservations.c.token == cur["token"]))
                log_event(LOG, "reservation reclaimed", receiver=rx.id, device=key, old_job=cur["job_id"],
                          old_holder=cur["holder"], why=why, job_id=job_id)
            try:
                with conn.begin_nested():
                    conn.execute(sched_reservations.insert().values(
                        device_key=key, receiver_id=rx.id, job_id=job_id, token=token, holder=self.holder,
                        purpose=purpose[:200], acquired_at=now, renewed_at=now,
                        expires_at=now + dt.timedelta(seconds=self.lease_s)))
            except IntegrityError:
                raise ReservationBusy(f"{rx.id} was reserved by another Starlab job at the same moment", {}) from None
        log_event(LOG, "reservation acquired", receiver=rx.id, device=key, job_id=job_id)
        return token

    def _stale(self, conn: Any, cur: Any, job_id: str, now: dt.datetime) -> str | None:
        """Why an existing reservation may be replaced (None: it stands)."""
        if cur["job_id"] == job_id:
            return "the same job takes it again (broker restart)"
        if cur["expires_at"] <= now:
            return f"expired at {cur['expires_at']:%H:%M:%S}Z (holder {cur['holder']} stopped renewing it)"
        st = conn.execute(select(sched_jobs.c.status).where(sched_jobs.c.job_id == cur["job_id"])).scalar()
        if st is None or st in ENDED:
            return f"abandoned: its job {cur['job_id']} is {st or 'gone'}"
        return None

    def renew(self, rx: Receiver, token: str) -> bool:
        """Extend the lease; False if the reservation was lost (expired and taken by someone else)."""
        now = self.clock()
        with self.engine.begin() as conn:
            n = conn.execute(update(sched_reservations)
                             .where(sched_reservations.c.device_key == device_key(rx),
                                    sched_reservations.c.token == token)
                             .values(renewed_at=now, expires_at=now + dt.timedelta(seconds=self.lease_s))).rowcount
        return bool(n)

    def release(self, rx: Receiver, token: str) -> bool:
        with self.engine.begin() as conn:
            n = conn.execute(delete(sched_reservations).where(sched_reservations.c.device_key == device_key(rx),
                                                              sched_reservations.c.token == token)).rowcount
        if n:
            log_event(LOG, "reservation released", receiver=rx.id, device=device_key(rx))
        return bool(n)

    def sweep(self) -> list[dict[str, Any]]:
        """Clear expired reservations and those of jobs that have ended (crash recovery); returns what was cleared."""
        now = self.clock()
        cleared = []
        with self.engine.begin() as conn:
            for cur in conn.execute(select(sched_reservations)).mappings().all():
                why = self._stale(conn, cur, "", now)
                if why is None:
                    continue
                conn.execute(delete(sched_reservations).where(sched_reservations.c.device_key == cur["device_key"],
                                                              sched_reservations.c.token == cur["token"]))
                cleared.append({"receiver_id": cur["receiver_id"], "job_id": cur["job_id"], "why": why})
                log_event(LOG, "reservation cleared", receiver=cur["receiver_id"], job_id=cur["job_id"], why=why)
        return cleared
