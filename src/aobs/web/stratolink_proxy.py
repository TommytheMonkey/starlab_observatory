"""The Stratolink page (/stratolink): Starlab serves it with the same sign-in as the rest of the web page and forwards
its API calls to the Stratolink daemon's control endpoint on this computer (the SSTV and Pirate Watch pages' pattern).

The daemon (`python -m aobs.stratolink run`, systemd unit starlab-stratolink, user tommy) owns the UDP receiver, its
archive and its files; every call goes to its HTTP endpoint with the broker's bearer token, plus X-Starlab-User so its
log says who created a flight or registered a tracker. The daemon validates every path and body itself.

[stratolink] url in the Starlab config overrides where the daemon listens (default http://127.0.0.1:8754).
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

LOG = logging.getLogger("aobs.stratolink.web")
PAGE = pathlib.Path(__file__).resolve().parent / "stratolink.html"
DEFAULT_URL = "http://127.0.0.1:8754"
PATH = re.compile(r"[A-Za-z0-9!._/-]{0,200}")
PASS_HEADERS = ("Content-Type", "Content-Disposition", "Cache-Control")
DOWN = ("Stratolink is not answering at {url}. It may be stopped (systemctl status starlab-stratolink) "
        "or restarting.")
# map tiles come from the viewer's browser: OpenStreetMap, OpenTopoMap (terrain) and Esri World Imagery (satellite)
TILES = "https://tile.openstreetmap.org https://*.tile.opentopomap.org https://server.arcgisonline.com"


def daemon_url(config: Any) -> str:
    try:
        return str(config.section("stratolink").get("url") or DEFAULT_URL).rstrip("/")
    except Exception:  # noqa: BLE001 - no config (tests, early start): the default
        return DEFAULT_URL


def register(app: FastAPI, ui_auth: Callable[..., str], api_token: str, url: Callable[[], str],
             page_headers: dict[str, str], opener: Callable[..., Any] = urllib.request.urlopen) -> None:
    csp = page_headers.get("Content-Security-Policy", "")
    if "img-src" in csp:
        csp = re.sub(r"img-src ([^;]*)", lambda m: f"img-src {m.group(1).strip()} {TILES}", csp, count=1)
    else:
        csp = f"{csp.rstrip('; ')}; img-src 'self' data: {TILES}".lstrip("; ")
    headers = {**page_headers, "Content-Security-Policy": csp}

    def forward(method: str, path: str, query: str, who: str, body: bytes | None = None,
                timeout: float = 15.0) -> Response:
        base = url()
        req = urllib.request.Request(f"{base}/v1/stratolink{'/' + path if path else ''}{'?' + query if query else ''}",
                                     data=body, method=method,
                                     headers={"Authorization": f"Bearer {api_token}", "X-Starlab-User": who,
                                              **({"Content-Type": "application/json"} if body is not None else {})})
        try:
            with opener(req, timeout=timeout) as r:
                status, data, hdrs = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, data, hdrs = e.code, e.read(), e.headers
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            LOG.info("Stratolink unreachable at %s: %s", base, exc)
            return JSONResponse({"error": DOWN.format(url=base), "daemon_down": True}, status_code=503)
        out = {k: hdrs[k] for k in PASS_HEADERS if hdrs and hdrs.get(k)}
        out.setdefault("Cache-Control", "no-store")
        return Response(content=data, status_code=status, headers=out,
                        media_type=out.pop("Content-Type", "application/json"))

    @app.get("/stratolink", response_class=HTMLResponse)
    def stratolink_page() -> HTMLResponse:        # read per request, like /rf, so page edits need no restart
        return HTMLResponse(PAGE.read_text(), headers=headers)

    @app.get("/v1/stratolink")
    def sl_status(who: str = Depends(ui_auth)) -> Response:
        return forward("GET", "", "", who)

    @app.get("/v1/stratolink/{path:path}")
    def sl_get(path: str, request: Request, who: str = Depends(ui_auth)) -> Response:
        path = path.strip("/")
        if not PATH.fullmatch(path) or ".." in path:
            return JSONResponse({"error": "not found"}, status_code=404)
        big = path.startswith("firmware/") or "/export." in path
        return forward("GET", path, request.url.query, who, timeout=120.0 if big else 15.0)

    @app.post("/v1/stratolink/{path:path}")
    async def sl_post(path: str, request: Request, who: str = Depends(ui_auth)) -> Response:
        path = path.strip("/")
        if not PATH.fullmatch(path) or ".." in path:
            return JSONResponse({"error": "not found"}, status_code=404)
        body = await request.body()
        if len(body) > 256 * 1024:
            return JSONResponse({"error": "too large"}, status_code=413)
        try:
            json.loads(body or b"{}")
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        import asyncio
        return await asyncio.to_thread(forward, "POST", path, "", who, body or b"{}", 30.0)
