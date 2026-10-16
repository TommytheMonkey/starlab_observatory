"""A small Slack Web API client on the standard library (no Slack SDK needed for posting).

Tokens are sent only in the Authorization header and never logged; errors carry Slack's error code, never
the request. HTTP 429 is retried once after Slack's Retry-After (capped); other failures raise SlackError.
`transport` is injectable so tests never reach slack.com.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

BASE = "https://slack.com/api/"
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, dict[str, str], bytes]]


class SlackError(RuntimeError):
    def __init__(self, method: str, error: str, detail: str = ""):
        super().__init__(f"Slack {method}: {error}" + (f" ({detail})" if detail else ""))
        self.method, self.error = method, error


def urllib_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read() or b""


class SlackApi:
    def __init__(self, token: str, transport: Transport | None = None, timeout: float = 15,
                 sleep: Callable[[float], None] = time.sleep):
        self.token, self.transport, self.timeout, self.sleep = token, transport or urllib_transport, timeout, sleep

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json; charset=utf-8"}
        for attempt in range(2):
            try:
                status, hdrs, raw = self.transport(BASE + method, headers, body, self.timeout)
            except (OSError, urllib.error.URLError) as exc:
                raise SlackError(method, "network", type(exc).__name__) from None
            if status == 429 and attempt == 0:
                self.sleep(min(float(hdrs.get("retry-after") or 1), 30))
                continue
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                raise SlackError(method, f"http_{status}") from None
            if not data.get("ok"):
                raise SlackError(method, str(data.get("error") or f"http_{status}"))
            data["_scopes"] = hdrs.get("x-oauth-scopes")
            return data
        raise SlackError(method, "ratelimited")

    def post(self, channel: str, text: str, blocks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return self.call("chat.postMessage", channel=channel, text=text, blocks=blocks, unfurl_links=False)

    def upload(self, channel: str, path: str, title: str, comment: str = "") -> dict[str, Any]:
        """files.getUploadURLExternal -> upload -> files.completeUploadExternal (the current upload flow)."""
        import pathlib
        p = pathlib.Path(path)
        data = p.read_bytes()
        got = self.call("files.getUploadURLExternal", filename=p.name, length=len(data))
        q = urllib.parse.urlencode({"filename": p.name})
        status, _, _ = self.transport(got["upload_url"] + ("&" if "?" in got["upload_url"] else "?") + q,
                                      {"Content-Type": "application/octet-stream"}, data, max(self.timeout, 60))
        if status != 200:
            raise SlackError("files.upload", f"http_{status}")
        return self.call("files.completeUploadExternal", files=[{"id": got["file_id"], "title": title}],
                         channel_id=channel, initial_comment=comment or None)
