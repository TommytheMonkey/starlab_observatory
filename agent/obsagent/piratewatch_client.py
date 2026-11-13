"""Client for the satcom pirate watch's control endpoint on observatory (starlab-dev aobs.piratewatch.control).

The watch holds OBS-R05 most of the time. Releasing it frees R05 for SDR++ server; resuming lets the watch take
it back. The endpoint takes the broker's bearer token and listens next to the broker on port 8752, so the URL is
derived from the broker URL unless PIRATEWATCH_URL is set.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

PORT = 8752


class PirateWatchError(RuntimeError):
    pass


def url_from_broker(broker_url: str) -> str:
    override = os.environ.get("PIRATEWATCH_URL")
    if override:
        return override.rstrip("/")
    u = urllib.parse.urlsplit(broker_url)
    return urllib.parse.urlunsplit((u.scheme, f"{u.hostname}:{PORT}", "", "", "")).rstrip("/")


class PirateWatchClient:
    def __init__(self, url: str, token: str, timeout: float = 40):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        req = urllib.request.Request(f"{self.url}/v1/piratewatch{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("detail") or exc.reason
            except (ValueError, AttributeError):
                detail = exc.reason
            raise PirateWatchError(f"pirate watch {method} {path or '/'} -> {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise PirateWatchError(f"pirate watch unreachable at {self.url} ({getattr(exc, 'reason', exc)}); "
                                   "is starlab-pirate-watch running on observatory?") from exc

    def status(self) -> dict[str, Any]:
        return self._call("GET", "")

    def release(self, minutes: int) -> dict[str, Any]:
        return self._call("POST", "/release", {"minutes": int(minutes)})

    def resume(self) -> dict[str, Any]:
        return self._call("POST", "/resume", {})
