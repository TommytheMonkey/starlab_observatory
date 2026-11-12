"""Holds OBS-R05 through the Starlab broker's ad-hoc lease (POST /v1/lease), so the watch never
fights scheduled recordings, the Slack agent, or anything else that asks the broker.

The broker caps an ad-hoc lease to end 30 s before the next scheduled job on the receiver,
and a job that starts takes the receiver by ending the lease. The keeper renews every few
seconds; when a renewal fails or the granted time runs short it sets `lost`, and the
daemon stops its radio process at once so the job finds the dongle free.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import threading
import urllib.error
import urllib.request
from typing import Any, Callable

LOG = logging.getLogger("aobs.piratewatch.lease")
UTC = dt.timezone.utc


class LeaseError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def read_token(path: str) -> str:
    """The token file is either the bare token or KEY=VALUE lines (BROKER_TOKEN=...)."""
    text = pathlib.Path(path).expanduser().read_text().strip()
    for line in text.splitlines():
        if "=" in line and "TOKEN" in line.split("=", 1)[0].upper():
            return line.split("=", 1)[1].strip().strip("'\"")
    return text.splitlines()[0].strip() if text else ""


class BrokerClient:
    def __init__(self, url: str, token: str, timeout: float = 30.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("detail", "")
            except ValueError:
                detail = ""
            raise LeaseError(f"{method} {path}: HTTP {exc.code} {detail}".strip(), exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LeaseError(f"{method} {path}: {exc}") from exc


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


class LeaseKeeper:
    def __init__(self, client: BrokerClient, receiver: str, owner: str, port: int | None, ttl_s: int,
                 renew_every_s: float, clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(UTC),
                 margin_s: float = 15.0):
        self.client = client
        self.receiver = receiver
        self.owner = owner
        self.port = port
        self.ttl_s = ttl_s
        self.renew_every_s = renew_every_s
        self.clock = clock
        self.margin_s = margin_s
        self.token: str | None = None
        self.expires_at: dt.datetime | None = None
        self.port_corrections = 0
        self.lost = threading.Event()
        self.lost_reason = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def held(self) -> bool:
        return self.token is not None and not self.lost.is_set()

    def acquire(self) -> dict[str, Any]:
        body = {"owner": self.owner, "purpose": "satcom pirate watch (243-270 MHz)", "ttl_s": self.ttl_s,
                "receiver": self.receiver, "port": self.port}
        lease = self.client.call("POST", "/v1/lease", body)
        self.token = lease["token"]
        self.expires_at = _parse(lease["expires_at"])
        self.lost.clear()
        self.port_corrections = 0
        self.lost_reason = ""
        if self.remaining() < self.margin_s:
            self._lose(f"lease too short ({self.remaining():.0f} s): a scheduled job is about to start")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pirate-lease", daemon=True)
        self._thread.start()
        return lease

    def remaining(self) -> float:
        return (self.expires_at - self.clock()).total_seconds() if self.expires_at else 0.0

    def _lose(self, reason: str) -> None:
        if not self.lost.is_set():
            self.lost_reason = reason
            LOG.warning("lease lost: %s", reason)
            self.lost.set()

    def renew_once(self) -> None:
        if self.token is None:
            return
        try:
            lease = self.client.call("POST", f"/v1/lease/{self.token}/renew", {"ttl_s": self.ttl_s})
            self.expires_at = _parse(lease["expires_at"])
            fixed = int(lease.get("port_corrections") or 0)
            if fixed > self.port_corrections:     # the broker found the switch on another port and re-selected ours
                LOG.warning("ANTSW-01 was not on port %s (board rebooted or changed by hand); the broker re-selected it "
                            "(%d time(s) this lease)", self.port, fixed)
            self.port_corrections = fixed
        except LeaseError as exc:
            if exc.status in (404, 409):
                self._lose(str(exc))
                return
            LOG.warning("lease renewal failed (will retry): %s", exc)
        if self.remaining() < self.margin_s:
            self._lose(f"lease ends in {self.remaining():.0f} s (scheduled job ahead)")

    def _loop(self) -> None:
        while not self._stop.wait(self.renew_every_s):
            self.renew_once()
            if self.lost.is_set():
                return

    def release(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        token, self.token = self.token, None
        self.expires_at = None
        if token:
            try:
                self.client.call("DELETE", f"/v1/lease/{token}")
            except LeaseError as exc:
                if exc.status != 404:
                    LOG.warning("lease release failed: %s", exc)
