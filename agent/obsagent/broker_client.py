"""Client for the observatory receiver broker, plus a receiver session that holds one lease.

The session borrows OBS-R05 on first use, keeps the lease alive while tools run,
and hands the receiver back (so ADS-B resumes) after `idle_release_s` of quiet.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

LOG = logging.getLogger("obsagent.broker")


class BrokerError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class BrokerClient:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 360) -> Any:
        req = urllib.request.Request(self.url + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read())
                detail = body.get("error") or body.get("detail") or exc.reason
            except (ValueError, AttributeError):
                detail = exc.reason
            if exc.code == 404 and detail == "Not Found":
                # FastAPI's own 404: the route itself is missing, not the thing it names
                detail = ("this broker has no such endpoint (it is probably running older code than the bot; "
                          "restart observatory-broker on observatory)")
            raise BrokerError(f"broker {method} {path.split('?')[0]} -> {exc.code}: {detail}", exc.code) from exc
        except urllib.error.URLError as exc:
            raise BrokerError(f"broker unreachable: {exc.reason}") from exc

    def state(self) -> dict[str, Any]:
        return self._call("GET", "/v1/state")

    def passes(self, hours: float) -> list[dict[str, Any]]:
        return self._call("GET", f"/v1/passes?hours={hours:g}")["passes"]

    def lease(self, owner: str, purpose: str, ttl_s: int, port: int | None) -> dict[str, Any]:
        return self._call("POST", "/v1/lease", {"owner": owner, "purpose": purpose, "ttl_s": ttl_s, "port": port})

    def renew(self, token: str, ttl_s: int) -> dict[str, Any]:
        return self._call("POST", f"/v1/lease/{token}/renew", {"ttl_s": ttl_s})

    def release(self, token: str) -> dict[str, Any]:
        return self._call("DELETE", f"/v1/lease/{token}")

    def port(self, token: str, port: int) -> dict[str, Any]:
        return self._call("POST", f"/v1/lease/{token}/port", {"port": port})

    def sweep(self, token: str, **spec: Any) -> dict[str, Any]:
        return self._call("POST", f"/v1/lease/{token}/sweep", spec)

    def audio(self, token: str, **spec: Any) -> dict[str, Any]:
        return self._call("POST", f"/v1/lease/{token}/audio", spec)

    # ---- unified scheduler
    def schedule(self, jobs: list[dict[str, Any]], transaction_mode: str = "all_or_nothing", dry_run: bool = False,
                 requested_by: str = "") -> dict[str, Any]:
        return self._call("POST", "/v1/schedule", {"jobs": jobs, "transaction_mode": transaction_mode,
                                                   "dry_run": dry_run, "requested_by": requested_by}, timeout=60)

    def capabilities(self) -> dict[str, Any]:
        return self._call("GET", "/v1/capabilities", timeout=30)

    def jobs(self, **params: Any) -> list[dict[str, Any]]:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return self._call("GET", f"/v1/jobs?{q}", timeout=30)["jobs"]

    def job(self, job_id: str) -> dict[str, Any]:
        return self._call("GET", f"/v1/jobs/{urllib.parse.quote(job_id)}", timeout=30)

    def cancel(self, job_id: str, reason: str) -> dict[str, Any]:
        return self._call("POST", f"/v1/jobs/{urllib.parse.quote(job_id)}/cancel", {"reason": reason}, timeout=30)

    def recordings(self, **params: Any) -> list[dict[str, Any]]:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return self._call("GET", f"/v1/recordings?{q}", timeout=60)["recordings"]

    def notifications(self, after: int) -> list[dict[str, Any]]:
        return self._call("GET", f"/v1/notifications?after={int(after)}", timeout=30)["notifications"]

    def job_file(self, job_id: str, path: str) -> bytes:
        req = urllib.request.Request(f"{self.url}/v1/jobs/{urllib.parse.quote(job_id)}/file?"
                                     f"{urllib.parse.urlencode({'path': path})}",
                                     headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()

    # ---- generic access to the catalog/status endpoints
    def get(self, path: str, **params: Any) -> Any:
        q = urllib.parse.urlencode({k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items() if v is not None})
        return self._call("GET", path + (f"?{q}" if q else ""), timeout=120)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self._call("POST", path, body, timeout=120)

    def artifact_bytes(self, artifact_id: str) -> bytes:
        req = urllib.request.Request(f"{self.url}/v1/artifacts/{urllib.parse.quote(artifact_id)}/file",
                                     headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()

    def download(self, rel: str) -> bytes:
        req = urllib.request.Request(f"{self.url}/v1/captures/{urllib.parse.quote(rel)}",
                                     headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()


class ReceiverSession:
    """One shared lease on OBS-R05 for the whole agent."""

    def __init__(self, client: BrokerClient, owner: str = "slack-agent", ttl_s: int = 600, idle_release_s: float = 120):
        self.client = client
        self.owner = owner
        self.ttl_s = ttl_s
        self.idle_release_s = idle_release_s
        self._lock = threading.RLock()
        self._token: str | None = None
        self._port: int | None = None
        self._last_use = 0.0
        self._busy = 0
        self._op = threading.Lock()  # one radio operation at a time, so a port change can't land mid-sweep
        threading.Thread(target=self._reaper, name="lease-reaper", daemon=True).start()

    def acquire(self, port: int | None = None, purpose: str = "") -> str:
        with self._lock:
            self._last_use = time.monotonic()
            if self._token:
                try:
                    self.client.renew(self._token, self.ttl_s)
                except BrokerError:
                    self._token = None  # expired or broker restarted; take a fresh lease below
            if not self._token:
                lease = self.client.lease(self.owner, purpose or "Slack agent request", self.ttl_s, port)
                self._token, self._port = lease["token"], lease.get("port") or lease.get("prior_port")
                LOG.info("leased %s (port %s)", lease["receiver"], self._port)
            elif port is not None and port != self._port:
                self.client.port(self._token, port)
                self._port = port
            return self._token

    @contextlib.contextmanager
    def use(self, port: int | None = None, purpose: str = "") -> Iterator[str]:
        """Hold the receiver for one operation; the idle reaper never releases mid-operation."""
        with self._op:
            with self._lock:
                token = self.acquire(port, purpose)
                self._busy += 1
            try:
                yield token
            finally:
                with self._lock:
                    self._busy -= 1
                    self._last_use = time.monotonic()

    def release(self, only_if_idle: bool = False) -> dict[str, Any] | None:
        with self._op:  # waits for any running operation to finish
            with self._lock:
                if not self._token:
                    return None
                if only_if_idle and time.monotonic() - self._last_use <= self.idle_release_s:
                    return None
                token, self._token, self._port = self._token, None, None
            result = self.client.release(token)
        LOG.info("released receiver: %s", result)
        return result

    @property
    def held(self) -> bool:
        return self._token is not None

    def _reaper(self) -> None:
        while True:
            time.sleep(10)
            with self._lock:
                idle = self._token and not self._busy and time.monotonic() - self._last_use > self.idle_release_s
            if idle:
                try:
                    self.release(only_if_idle=True)
                except BrokerError as exc:
                    LOG.warning("idle release failed: %s", exc)
