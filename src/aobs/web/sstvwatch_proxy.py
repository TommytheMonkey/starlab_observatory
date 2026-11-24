"""The SSTV page (/sstvwatch): Starlab serves it with the same sign-in as the rest of the web page and forwards its
API calls to the SSTV watch's control endpoint on this computer (the Pirate Watch page's pattern).

The watch (`python -m aobs.sstvwatch`, its own systemd unit, user tommy) owns its gallery and files; every call goes
to its HTTP endpoint with the broker's bearer token, which the watch also accepts. Only the gallery's read-only views,
the image/audio/IQ files, and release / resume are forwarded.

[sstvwatch] url in the Starlab config overrides where the watch listens (default http://127.0.0.1:8753).
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

LOG = logging.getLogger("aobs.sstvwatch.web")
PAGE = pathlib.Path(__file__).resolve().parent / "sstvwatch.html"
DEFAULT_URL = "http://127.0.0.1:8753"
READ_PATHS = re.compile(r"images|facets|images/\d{1,12}(?:/(?:image|thumb|audio|iq))?")
PASS_HEADERS = ("Content-Type", "Content-Range", "Accept-Ranges", "Cache-Control", "Content-Disposition")
DOWN = ("The SSTV watch is not answering at {url}. It may be stopped (systemctl status starlab-sstv-watch) "
        "or restarting.")


def watch_url(config: Any) -> str:
    try:
        return str(config.section("sstvwatch").get("url") or DEFAULT_URL).rstrip("/")
    except Exception:  # noqa: BLE001 - no config (tests, early start): the default
        return DEFAULT_URL


def register(app: FastAPI, ui_auth: Callable[..., str], api_token: str, url: Callable[[], str],
             page_headers: dict[str, str], opener: Callable[..., Any] = urllib.request.urlopen) -> None:

    def forward(method: str, path: str, query: str = "", body: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None, timeout: float = 10.0) -> Response:
        base = url()
        req = urllib.request.Request(f"{base}/v1/sstvwatch{'/' + path if path else ''}{'?' + query if query else ''}",
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
            LOG.info("SSTV watch unreachable at %s: %s", base, exc)
            return JSONResponse({"error": DOWN.format(url=base), "watch_down": True}, status_code=503)
        out = {k: hdrs[k] for k in PASS_HEADERS if hdrs and hdrs.get(k)}
        out.setdefault("Cache-Control", "no-store")
        return Response(content=data, status_code=status, headers=out,
                        media_type=out.pop("Content-Type", "application/json"))

    @app.get("/sstvwatch", response_class=HTMLResponse)
    def sstvwatch_page() -> HTMLResponse:           # read per request, like /rf, so page edits need no restart
        return HTMLResponse(PAGE.read_text(), headers=page_headers)

    @app.get("/v1/sstvwatch")
    def sw_status(who: str = Depends(ui_auth)) -> Response:
        return forward("GET", "")

    @app.get("/v1/sstvwatch/{path:path}")
    def sw_read(path: str, request: Request, who: str = Depends(ui_auth)) -> Response:
        path = path.strip("/")
        if not READ_PATHS.fullmatch(path):
            return JSONResponse({"error": "not found"}, status_code=404)
        rng = request.headers.get("range")
        big = path.endswith(("/audio", "/iq", "/image"))
        return forward("GET", path, request.url.query, headers={"Range": rng} if rng else None,
                       timeout=60.0 if big else 10.0)

    @app.post("/v1/sstvwatch/release")
    def sw_release(body: dict[str, Any], who: str = Depends(ui_auth)) -> Response:
        minutes = body.get("minutes", 0)
        LOG.info("SSTV watch release (%s min) by %s", minutes, who)
        return forward("POST", "release", body={"minutes": minutes}, timeout=30.0)

    @app.post("/v1/sstvwatch/resume")
    def sw_resume(who: str = Depends(ui_auth)) -> Response:
        LOG.info("SSTV watch resume by %s", who)
        return forward("POST", "resume", body={}, timeout=15.0)
