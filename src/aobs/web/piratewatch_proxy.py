"""The Pirate Watch page (/piratewatch): Starlab serves it with the same sign-in as the rest of the web page, and
forwards its API calls to the watch's control endpoint on this computer.

The watch (`python -m aobs.piratewatch`, its own systemd unit, user tommy) owns its hit log and clips; Starlab never
opens them. Every call goes to the watch's HTTP endpoint with the broker's own bearer token, which the watch also
accepts. Only the watch's read-only views, its clips, and release / resume are forwarded; nothing else.

[piratewatch] url in the Starlab config overrides where the watch listens (default http://127.0.0.1:8752).
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import urllib.error
import urllib.request
from typing import Any, Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

LOG = logging.getLogger("aobs.piratewatch.web")
PAGE = pathlib.Path(__file__).resolve().parent / "piratewatch.html"
DEFAULT_URL = "http://127.0.0.1:8752"
READ_PATHS = re.compile(r"hits|heatmap|ignored|clips/\d{1,12}")
PASS_HEADERS = ("Content-Type", "Content-Range", "Accept-Ranges", "Cache-Control")
DOWN = ("The pirate watch is not answering at {url}. It may be stopped (systemctl status starlab-pirate-watch) "
        "or restarting.")


def watch_url(config: Any) -> str:
    try:
        return str(config.section("piratewatch").get("url") or DEFAULT_URL).rstrip("/")
    except Exception:  # noqa: BLE001 - no config (tests, early start): the default
        return DEFAULT_URL


def register(app: FastAPI, ui_auth: Callable[..., str], api_token: str, url: Callable[[], str],
             page_headers: dict[str, str], opener: Callable[..., Any] = urllib.request.urlopen) -> None:

    def forward(method: str, path: str, query: str = "", body: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None, timeout: float = 10.0) -> Response:
        base = url()
        req = urllib.request.Request(f"{base}/v1/piratewatch{'/' + path if path else ''}{'?' + query if query else ''}",
                                     data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Authorization": f"Bearer {api_token}",
                                              **({"Content-Type": "application/json"} if body is not None else {}),
                                              **(headers or {})})
        try:
            with opener(req, timeout=timeout) as r:
                status, data, hdrs = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, data, hdrs = e.code, e.read(), e.headers
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            LOG.info("pirate watch unreachable at %s: %s", base, exc)
            return JSONResponse({"error": DOWN.format(url=base), "watch_down": True}, status_code=503)
        out = {k: hdrs[k] for k in PASS_HEADERS if hdrs and hdrs.get(k)}
        out.setdefault("Cache-Control", "no-store")
        return Response(content=data, status_code=status, headers=out,
                        media_type=out.pop("Content-Type", "application/json"))

    @app.get("/piratewatch", response_class=HTMLResponse)
    def piratewatch_page() -> HTMLResponse:         # read per request, like /rf, so page edits need no restart
        return HTMLResponse(PAGE.read_text(), headers=page_headers)

    @app.get("/v1/piratewatch")
    def pw_status(who: str = Depends(ui_auth)) -> Response:
        return forward("GET", "")

    @app.get("/v1/piratewatch/{path:path}")
    def pw_read(path: str, request: Request, who: str = Depends(ui_auth)) -> Response:
        path = path.strip("/")
        if not READ_PATHS.fullmatch(path):
            return JSONResponse({"error": "not found"}, status_code=404)
        rng = request.headers.get("range")
        return forward("GET", path, request.url.query, headers={"Range": rng} if rng else None,
                       timeout=30.0 if path.startswith("clips/") else 10.0)

    @app.post("/v1/piratewatch/release")
    def pw_release(body: dict[str, Any], who: str = Depends(ui_auth)) -> Response:
        minutes = body.get("minutes", 0)
        LOG.info("pirate watch release (%s min) by %s", minutes, who)
        return forward("POST", "release", body={"minutes": minutes}, timeout=30.0)

    @app.post("/v1/piratewatch/resume")
    def pw_resume(who: str = Depends(ui_auth)) -> Response:
        LOG.info("pirate watch resume by %s", who)
        return forward("POST", "resume", body={}, timeout=15.0)

