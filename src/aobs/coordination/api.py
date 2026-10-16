"""HTTP API for the broker. Every endpoint needs `Authorization: Bearer <broker token>`."""

from __future__ import annotations

import hashlib
import hmac
import logging
import pathlib
import secrets
import threading
import time
from typing import Any, Callable

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import radio
from .broker import Broker, BrokerError


class LeaseIn(BaseModel):
    owner: str = Field(min_length=1, max_length=64)
    purpose: str = ""
    ttl_s: int = 600
    receiver: str | None = None          # default: the broker's only lendable receiver
    port: int | None = None


class RenewIn(BaseModel):
    ttl_s: int = 600


class PortIn(BaseModel):
    port: int


class SweepIn(BaseModel):
    start_hz: int
    stop_hz: int
    bin_hz: int = 12_500
    seconds: float = 10.0
    gain_db: float | None = None
    direct: bool = False
    crop: float = 0.0
    ppm: int = 0


class AudioIn(BaseModel):
    freq_hz: int
    mode: str = "nfm"
    seconds: float = 15.0
    gain_db: float | None = None
    squelch: int = 0
    direct: bool = False
    bandwidth_hz: int | None = None
    ppm: int = 0


class IqIn(BaseModel):
    freq_hz: int
    sample_rate: int = 2_048_000
    seconds: float = 5.0
    gain_db: float | None = None


class ScheduleIn(BaseModel):
    jobs: list[dict[str, Any]]
    transaction_mode: str = "all_or_nothing"
    dry_run: bool = False
    requested_by: str = ""


class ProcessIn(BaseModel):
    params: dict[str, Any] = {}
    origin: str = "api"
    requested_by: str = ""


class RfSaveIn(BaseModel):
    layout: dict[str, Any]
    base_version: int | None = None
    note: str = Field(default="", max_length=500)
    dry_run: bool = False


RF_COOKIE = "aobs_rf"
RF_SESSION_S = 30 * 86400          # the editor stays signed in for 30 days on that browser
RF_LINK_S = 15 * 60                # a sign-in link must be opened within 15 minutes
RF_PAGE = pathlib.Path(__file__).resolve().parents[1] / "scheduling" / "rf_ui.html"
SETUP_PAGE = pathlib.Path(__file__).resolve().parents[1] / "web" / "setup.html"
PAGE_HEADERS = {"Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                           "script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"}


LINK_EXPIRED_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Starlab: sign-in link expired</title>
<style>body{font:15px/1.5 system-ui,sans-serif;max-width:640px;margin:48px auto;padding:0 16px;color:#1d2330;background:#fff}
code{background:#eef1f6;padding:2px 6px;border-radius:4px}@media (prefers-color-scheme:dark){body{background:#12151c;color:#e4e8f0}
code{background:#232936}}</style></head><body>
<h1>This sign-in link has expired</h1>
<p>Sign-in links work for 15 minutes (24 hours while setup is unfinished). To get a new one, open a terminal on the
computer Starlab runs on, or log in to it with SSH from this one, and run:</p>
<p><code>starlab web-link</code></p>
<p>It prints one link per address this computer can be reached at. Open the one that matches how you reach it. If the
Slack integration is set up, you can also ask it for a link.</p>
<p>Not sure how to log in with SSH? See the guide <i>Opening Starlab from another computer</i> in Starlab's docs.</p>
</body></html>"""


def _install_access_check(app: FastAPI, allowed: Any) -> None:
    """Refuse clients outside [broker] allowed_clients (default: private networks and Tailscale)."""
    from fastapi.responses import PlainTextResponse

    from ..web.access import REFUSED

    @app.middleware("http")
    async def access_check(request: Request, call_next: Any) -> Any:
        ip = request.client.host if request.client else ""
        if not allowed.allows(ip):
            return PlainTextResponse(REFUSED.format(ip=ip or "unknown"), status_code=403)
        return await call_next(request)


class CancelIn(BaseModel):
    reason: str = "cancelled by owner"
    actor: str = "slack"


def create_app(broker: Broker, api_token: str, watchdog_s: float = 5.0,
               passes: Callable[[float], list[dict[str, Any]]] | None = None, scheduler: Any = None,
               access: Any = None) -> FastAPI:
    app = FastAPI(title="aobs receiver broker", version="1")
    stop = threading.Event()
    if access is not None:
        _install_access_check(app, access)

    def auth(authorization: str = Header(default="")) -> None:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(value, api_token):
            raise HTTPException(401, "bad or missing bearer token")

    # ---- RF editor sign-in: a signed, expiring link (posted by the bot on request) sets a session cookie.
    def rf_sig(purpose: str, exp: int) -> str:
        from ..web.auth import sign
        return sign(sched().s.config, purpose, exp)

    def ui_auth(request: Request, authorization: str = Header(default=""),
                aobs_rf: str | None = Cookie(default=None)) -> str:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and secrets.compare_digest(value, api_token):
            return "api"
        exp_s, _, sig = (aobs_rf or "").partition(".")
        if exp_s.isdigit() and int(exp_s) > time.time() and hmac.compare_digest(sig, rf_sig("session", int(exp_s))):
            # cookie sessions must also send this header, which a cross-site form cannot (CSRF)
            if request.method != "GET" and request.headers.get("x-aobs-rf") != "1":
                raise HTTPException(403, "missing X-AOBS-RF header")
            return "owner (web)"
        raise HTTPException(401, "not signed in: run `starlab web-link` on the Starlab computer and open a link it prints")

    @app.exception_handler(BrokerError)
    async def broker_error(_: Request, exc: BrokerError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    @app.exception_handler(radio.RadioError)
    async def radio_error(_: Request, exc: radio.RadioError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    from ..scheduling.store import SchedulerError

    @app.exception_handler(SchedulerError)
    async def scheduler_error(_: Request, exc: SchedulerError) -> JSONResponse:
        return JSONResponse({"error": str(exc), **({"errors": exc.errors} if getattr(exc, "errors", None) else {})},
                            status_code=exc.status)

    from ..inventory.store import InventoryError
    from ..settings.store import SettingsError

    @app.exception_handler(SettingsError)
    async def settings_error(_: Request, exc: SettingsError) -> JSONResponse:
        return JSONResponse({"error": str(exc), "errors": exc.errors}, status_code=exc.status)

    @app.exception_handler(InventoryError)
    async def inventory_error(_: Request, exc: InventoryError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    @app.on_event("startup")
    def _start() -> None:
        if scheduler is not None:
            broker.recover(keep=scheduler.keeps_lease)
            scheduler.start()
        else:
            broker.recover()

        def loop() -> None:
            while not stop.wait(watchdog_s):
                broker.expire()
        threading.Thread(target=loop, name="broker-watchdog", daemon=True).start()

    @app.on_event("shutdown")
    def _stop() -> None:
        stop.set()
        if scheduler is not None:
            scheduler.stop()

    deps = [Depends(auth)]

    # Radio operations block for their duration, so they are plain (threadpool) handlers.
    @app.get("/v1/state", dependencies=deps)
    def state() -> dict[str, Any]:
        return broker.state()

    @app.get("/v1/passes", dependencies=deps)
    def upcoming_passes(hours: float = 24.0) -> dict[str, Any]:
        if passes is None:
            raise BrokerError("Pass schedule not available", 501)
        return {"passes": passes(min(hours, 72.0))}

    @app.post("/v1/lease", dependencies=deps)
    def lease(body: LeaseIn) -> dict[str, Any]:
        return broker.lease(body.owner, body.purpose, body.ttl_s, body.receiver, body.port)

    @app.post("/v1/lease/{token}/renew", dependencies=deps)
    def renew(token: str, body: RenewIn) -> dict[str, Any]:
        return broker.renew(token, body.ttl_s)

    @app.delete("/v1/lease/{token}", dependencies=deps)
    def release(token: str) -> dict[str, Any]:
        return broker.release(token)

    @app.post("/v1/lease/{token}/port", dependencies=deps)
    def port(token: str, body: PortIn) -> dict[str, Any]:
        return broker.select_port(token, body.port)

    @app.post("/v1/lease/{token}/sweep", dependencies=deps)
    def sweep(token: str, body: SweepIn) -> dict[str, Any]:
        return broker.sweep(token, radio.Sweep(**body.model_dump()))

    @app.post("/v1/lease/{token}/audio", dependencies=deps)
    def audio(token: str, body: AudioIn) -> dict[str, Any]:
        return broker.audio(token, radio.AudioCapture(**body.model_dump()))

    @app.post("/v1/lease/{token}/iq", dependencies=deps)
    def iq(token: str, body: IqIn) -> dict[str, Any]:
        return broker.iq(token, radio.IqCapture(**body.model_dump()))

    @app.get("/v1/captures/{day}/{name}", dependencies=deps)
    def capture(day: str, name: str) -> FileResponse:
        return FileResponse(broker.capture_file(f"{day}/{name}"))

    # ------------------------------------------------------------ unified scheduler
    def sched() -> Any:
        if scheduler is None:
            raise BrokerError("The scheduler is not enabled on this broker", 501)
        return scheduler

    @app.post("/v1/schedule", dependencies=deps)
    def schedule(body: ScheduleIn) -> dict[str, Any]:
        return sched().s.submit(body.jobs, body.transaction_mode, body.requested_by, body.dry_run)

    @app.get("/v1/capabilities", dependencies=deps)
    def capabilities() -> dict[str, Any]:
        return sched().s.capabilities()

    @app.get("/v1/jobs", dependencies=deps)
    def jobs(receiver: str | None = None, status: str | None = None, source: str | None = None,
             hours_back: float = 6, hours_ahead: float = 48) -> dict[str, Any]:
        import datetime as dt
        s = sched().s
        now = s.clock()
        rows = s.jobs(receiver=receiver, status=status.split(",") if status else None, source=source,
                      start=now - dt.timedelta(hours=hours_back), end=now + dt.timedelta(hours=min(hours_ahead, 24 * 31)))
        for r in rows:
            r.pop("spec", None)
        return {"jobs": rows}

    @app.get("/v1/jobs/{job_id}", dependencies=deps)
    def job(job_id: str) -> dict[str, Any]:
        found = sched().s.get(job_id)
        if found is None:
            raise BrokerError(f"no job {job_id}", 404)
        return found

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=deps)
    def cancel(job_id: str, body: CancelIn) -> dict[str, Any]:
        return sched().s.cancel(job_id, body.reason, body.actor)

    @app.get("/v1/recordings", dependencies=deps)
    def recordings(receiver: str | None = None, satellite: str | None = None, date: str | None = None,
                   job_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        from ..scheduling.service import recordings as find
        svc = sched()
        return {"recordings": find(svc.s, receiver, satellite, date, job_id, min(limit, 100), svc.processor)}

    @app.get("/v1/jobs/{job_id}/file", dependencies=deps)
    def job_file(job_id: str, path: str) -> FileResponse:
        from ..scheduling.service import allowed_file
        return FileResponse(allowed_file(sched().s, job_id, path))

    # ---- processing attempts and the artifact index
    def proc() -> Any:
        svc = sched()
        if svc.processor is None:
            raise BrokerError("Processing is not enabled", 501)
        return svc.processor

    @app.post("/v1/recordings/{recording_id}/decode", dependencies=deps)
    def decode(recording_id: str, body: ProcessIn) -> dict[str, Any]:
        return proc().submit(recording_id, "decode", body.params, body.origin, body.requested_by)

    @app.post("/v1/recordings/{recording_id}/analyze", dependencies=deps)
    def analyze(recording_id: str, body: ProcessIn) -> dict[str, Any]:
        return proc().submit(recording_id, "analyze", body.params, body.origin, body.requested_by)

    @app.get("/v1/recordings/{recording_id}/results", dependencies=deps)
    def results(recording_id: str, attempt_id: str | None = None, kind: str | None = None) -> dict[str, Any]:
        p = proc()
        rec = sched().s.get(recording_id)
        if rec is None:
            raise BrokerError(f"no recording {recording_id}", 404)
        rec.pop("spec", None)
        return {"recording": rec, "attempts": p.attempts(recording_id),
                "artifacts": p.artifacts(recording_id, kind, attempt_id)}

    @app.get("/v1/attempts/{attempt_id}", dependencies=deps)
    def attempt(attempt_id: str) -> dict[str, Any]:
        return proc().attempt(attempt_id)

    @app.post("/v1/attempts/{attempt_id}/cancel", dependencies=deps)
    def cancel_attempt(attempt_id: str) -> dict[str, Any]:
        return proc().cancel(attempt_id)

    @app.get("/v1/artifacts/{artifact_id}/text", dependencies=deps)
    def artifact_text(artifact_id: str, tail_lines: int = 200, contains: str = "") -> dict[str, Any]:
        return proc().artifact_text(artifact_id, tail_lines, contains)

    @app.get("/v1/artifacts/{artifact_id}/file", dependencies=deps)
    def artifact_file(artifact_id: str) -> FileResponse:
        path, mime = proc().artifact_file(artifact_id)
        return FileResponse(path, media_type=mime, filename=path.name)

    # ---- status, health, catalog
    def cat() -> Any:
        svc = sched()
        if getattr(svc, "catalog", None) is None:
            raise BrokerError("catalog not enabled", 501)
        return svc.catalog

    @app.get("/v1/status", dependencies=deps)
    def status(receiver: str | None = None) -> dict[str, Any]:
        st = sched().status
        if receiver:
            s = sched().s
            rx = s.receivers.get(receiver.upper().replace("SDRPLAY-", ""))
            if rx is None:
                raise BrokerError(f"no receiver {receiver}", 404)
            return st.receiver(rx, broker.state().get("leases", {}))
        return st.status()

    @app.get("/v1/health", dependencies=deps)
    def health_ep() -> dict[str, Any]:
        from ..scheduling.health import health
        svc = sched()
        return health(svc.s, svc, svc.processor)

    @app.get("/v1/catalog/recordings", dependencies=deps)
    def catalog_recordings(receiver: str | None = None, satellite: str | None = None, date: str | None = None,
                           since: str | None = None, until: str | None = None, frequency_hz: int | None = None,
                           format: str | None = None, status: str | None = None, decoded: bool | None = None,
                           analyzed: bool | None = None, min_size_gb: float | None = None, experiment: str | None = None,
                           recording_id: str | None = None, page: int = 1, per_page: int = 10) -> dict[str, Any]:
        return cat().recordings(receiver, satellite, date, since, until, frequency_hz, format, status, decoded, analyzed,
                                min_size_gb, experiment, recording_id, page, per_page)

    @app.get("/v1/catalog/recordings/{recording_id}/results", dependencies=deps)
    def catalog_results(recording_id: str, type: str | None = None, attempt_id: str | None = None) -> dict[str, Any]:
        return cat().results(recording_id, type, attempt_id)

    @app.post("/v1/catalog/recordings/{recording_id}/process", dependencies=deps)
    def catalog_process(recording_id: str, body: dict[str, Any]) -> dict[str, Any]:
        kind = body.get("kind")
        if kind not in ("decode", "analyze", "compare"):
            raise BrokerError("kind must be decode, analyze or compare", 400)
        return proc().submit(recording_id, kind, body.get("params") or {}, body.get("origin", "api"), body.get("requested_by", ""))

    @app.post("/v1/catalog/recordings/{recording_id}/metadata", dependencies=deps)
    def catalog_metadata(recording_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return cat().correct_metadata(recording_id, body.get("fields") or {}, body.get("by", "api"))

    @app.get("/v1/catalog/jobs", dependencies=deps)
    def catalog_jobs(when: str | None = None, state: str | None = None, receiver: str | None = None,
                     limit: int = 40) -> dict[str, Any]:
        return {"jobs": cat().jobs(when, state, receiver, min(limit, 200))}

    @app.get("/v1/catalog/jobs/{job_id}", dependencies=deps)
    def catalog_job(job_id: str) -> dict[str, Any]:
        return cat().job_detail(job_id)

    @app.post("/v1/catalog/jobs/{job_id}/clone", dependencies=deps)
    def catalog_clone(job_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return cat().clone(job_id, body["start"], body.get("end"), body.get("duration_seconds"), body.get("receiver_id"),
                           body.get("job_name"), bool(body.get("dry_run", True)), body.get("requested_by", ""))

    @app.post("/v1/catalog/cancel", dependencies=deps)
    def catalog_cancel(body: dict[str, Any]) -> dict[str, Any]:
        if body.get("batch"):
            b = body["batch"]
            return cat().cancel_batch(b.get("receiver"), b.get("source"), b.get("start"), b.get("end"), b.get("job_ids"),
                                      body.get("confirm"), body.get("reason", "batch cancel"), body.get("actor", "slack"))
        return cat().cancel(body.get("job_id"), body.get("attempt_id"), body.get("confirm"),
                            body.get("reason", "cancelled from Slack"), body.get("actor", "slack"))

    @app.post("/v1/catalog/import", dependencies=deps)
    def catalog_import(body: dict[str, Any]) -> dict[str, Any]:
        return cat().import_recordings(body.get("roots"), bool(body.get("dry_run", True)))

    @app.get("/v1/artifacts/{artifact_id}/link", dependencies=deps)
    def artifact_link(artifact_id: str, request: Request, ttl_s: int = 3600) -> dict[str, Any]:
        return cat().link(artifact_id, ttl_s, str(request.base_url))

    @app.get("/dl/{artifact_id}/{exp}/{sig}")
    def download(artifact_id: str, exp: int, sig: str) -> FileResponse:
        path = cat().resolve_link(artifact_id, exp, sig)     # signed + expiring; no bearer token needed
        return FileResponse(path, filename=path.name)

    # ---- RF signal-path editor
    def rf() -> Any:
        svc = sched()
        if getattr(svc.s, "rf", None) is None:
            raise BrokerError("the RF layout is not loaded on this broker", 501)
        return svc.s.rf

    @app.get("/rf", response_class=HTMLResponse)
    def rf_page() -> HTMLResponse:
        return HTMLResponse(RF_PAGE.read_text(), headers={"Cache-Control": "no-store",
                                                          "Content-Security-Policy": "default-src 'self'; "
                                                          "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                                                          "img-src 'self' data:; frame-ancestors 'none'"})

    @app.get("/", response_class=HTMLResponse)
    def landing(aobs_rf: str | None = Cookie(default=None)) -> RedirectResponse:
        """First run goes to the setup wizard; afterwards to the station page."""
        try:
            done = bool(((sched().s.config_store.current("setup") or {}).get("data") or {}).get("completed"))
        except Exception:  # noqa: BLE001
            done = True
        return RedirectResponse("/rf" if done else "/setup", status_code=303)

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page() -> HTMLResponse:
        return HTMLResponse(SETUP_PAGE.read_text(), headers=PAGE_HEADERS)

    @app.get("/v1/rf/link", dependencies=deps)
    def rf_link(request: Request) -> dict[str, Any]:
        exp = int(time.time()) + RF_LINK_S
        base = str(request.base_url).rstrip("/")
        return {"url": f"{base}/rf/login/{exp}/{rf_sig('login', exp)}", "expires_in_s": RF_LINK_S,
                "session_days": RF_SESSION_S // 86400}

    @app.get("/rf/login/{exp}/{sig}", response_model=None)
    def rf_login(exp: int, sig: str) -> RedirectResponse | HTMLResponse:
        from ..web.auth import setup_completed
        ok = exp >= time.time() and (
            hmac.compare_digest(sig, rf_sig("login", exp))
            or (hmac.compare_digest(sig, rf_sig("setup-login", exp)) and not setup_completed(sched().s.engine)))
        if not ok:
            return HTMLResponse(LINK_EXPIRED_PAGE, status_code=403, headers=PAGE_HEADERS)
        until = int(time.time()) + RF_SESSION_S
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(RF_COOKIE, f"{until}.{rf_sig('session', until)}", max_age=RF_SESSION_S, httponly=True,
                        samesite="strict", path="/")
        return resp

    def rx_status(rid: str) -> dict[str, Any]:
        svc = sched()
        rx = svc.s.receivers[rid]
        try:
            st = svc.status.receiver(rx, broker.state().get("leases", {}))
        except Exception as exc:  # noqa: BLE001 - the editor must still load
            return {"state": "UNKNOWN", "error": str(exc)}
        keep = ("state", "connected", "presence", "mode", "iq_health", "current_job", "next_job", "last_successful_recording",
                "last_error", "legacy_unit", "legacy_unit_state")
        return {k: st.get(k) for k in keep}

    @app.get("/v1/rf")
    def rf_get(who: str = Depends(ui_auth)) -> dict[str, Any]:
        v = rf().view()
        v["status"] = {rid: rx_status(rid) for rid in sched().s.receivers}
        v["receiver_info"] = {rid: {"serial": rx.serial, "hardware": rx.hardware, "driver": rx.driver, "mode": rx.mode,
                                    "bias_tee_supported": rx.bias_tee_supported, "bias_tee_fixed": rx.bias_tee_fixed,
                                    "name": rx.name, "origin": rx.origin, "readiness": rx.readiness,
                                    "state": {"verified": "verified", "simulated": "simulated"}.get(rx.readiness, "configured"),
                                    "in_service": rx.in_service, "schedulable": rx.schedulable,
                                    "test_kinds": list(rx.test_kinds()), "automatic": bool(rx.auto.get("enabled")),
                                    **rx_properties(rx)}
                              for rid, rx in sched().s.receivers.items()}
        v["usage"] = rf_usage()
        sm = getattr(sched().s, "sessions", None)
        v["sessions"] = {r["receiver_id"]: sm.public(r) for r in sm.rows(active=True)} if sm is not None else {}
        v["switch_board"] = rf().board_status()
        v["viewer"] = who
        return v

    def rx_properties(rx: Any) -> dict[str, Any]:
        """What the receiver supports, from its driver (tables for RTL-SDR/SDRplay, the probe for SoapySDR)."""
        from ..scheduling.hardware import freq_range_hz, gain_text, rates_text
        from ..inventory.drivers import DRIVERS
        hz = freq_range_hz(rx)
        a = rx.auto if rx.auto.get("enabled") else {}
        return {"host": getattr(rx, "host", "local") or "local", "identity": rx.identity, "usb_path": rx.usb_path,
                "driver_label": DRIVERS[rx.driver].label, "freq_range_mhz": [hz[0] / 1e6, hz[1] / 1e6] if hz else None,
                "sample_rates": rates_text(rx), "gain_controls": gain_text(rx), "formats": list(rx.formats()),
                "capabilities_source": "driver table" if rx.driver in ("rtlsdr", "sdrplay") else
                (rx.caps.get("source") or "not probed yet"),
                "auto": {"center_mhz": a["center_frequency_hz"] / 1e6, "sample_rate": a.get("sample_rate_hz"),
                         "format": a.get("format"), "satellites": len(a.get("norad_ids") or [])}
                if a.get("center_frequency_hz") else None}

    def rf_usage() -> dict[str, Any]:
        """Per part: the most recent recording whose stored path went through it (evidence it works)."""
        from sqlalchemy import select as _select
        from ..db.schema import sched_jobs as _jobs
        import datetime as _dt
        svc = sched()
        since = svc.s.clock() - _dt.timedelta(days=14)
        with svc.s.engine.connect() as conn:
            rows = conn.execute(_select(_jobs.c.job_id, _jobs.c.record_start, _jobs.c.spec, _jobs.c.result, _jobs.c.status,
                                        _jobs.c.target, _jobs.c.receiver_id)
                                .where(_jobs.c.record_start >= since, _jobs.c.status.notin_(("scheduled", "shadow", "cancelled", "skipped")))
                                .order_by(_jobs.c.record_start.desc()).limit(300)).all()
        out: dict[str, Any] = {}
        for r in rows:
            snap = ((r.spec or {}).get("resolved") or {}).get("rf_path")
            if not snap:
                continue
            res = r.result or {}
            iq = ((res.get("capture") or {}).get("iq_health") or res.get("iq_health") or {})
            for c in snap.get("chain", []):
                out.setdefault(c["id"], {"recording_id": r.job_id, "at": r.record_start.isoformat(), "receiver": r.receiver_id,
                                         "target": r.target, "status": r.status, "iq_health": iq.get("status"),
                                         "iq_label": iq.get("label")})
        return out

    @app.post("/v1/rf")
    def rf_save(body: RfSaveIn, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return rf().save(body.layout, body.base_version, who, body.note, body.dry_run)

    @app.post("/v1/rf/switch/push")
    def rf_push_switch(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return rf().push_board(who)

    # ---- Signal Path Test Lab
    lab_box: dict[str, Any] = {}

    def lab() -> Any:
        if "lab" not in lab_box:
            from ..scheduling.testlab import TestLab
            svc = sched()
            if getattr(svc.s, "rf", None) is None:
                raise BrokerError("the RF layout is not loaded on this broker", 501)
            lab_box["lab"] = TestLab(svc, broker.state)
        return lab_box["lab"]

    @app.get("/v1/rf/tests/options/{receiver_id}")
    def rf_test_options(receiver_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return lab().options(receiver_id)

    @app.get("/v1/rf/tests/availability/{receiver_id}")
    def rf_test_availability(receiver_id: str, duration_s: int = 60, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return lab().availability(receiver_id, max(5, min(duration_s, 600)))

    @app.post("/v1/rf/tests/scan-plan")
    def rf_scan_plan(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return lab().scan_plan(str(body.get("receiver_id") or ""), body)

    @app.get("/v1/rf/tests/compare-scans")
    def rf_compare_scans(a: str, b: str, regions: str | None = None, who: str = Depends(ui_auth)) -> dict[str, Any]:
        import json as _json
        regs = _json.loads(regions) if regions else None
        return lab().compare_scans(a, b, regs)

    @app.get("/v1/rf/tests/compare-termination")
    def rf_compare_termination(a: str, b: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return lab().compare_termination(a, b)

    @app.get("/v1/rf/satellite/options")
    def rf_sat_options(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return lab().satellite_options()

    @app.post("/v1/rf/satellite/existing")
    def rf_sat_existing(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return lab().satellite_existing(body, who)

    @app.post("/v1/rf/satellite/compare")
    def rf_sat_compare(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return lab().sat_compare(body, who)

    @app.get("/v1/rf/tests/{test_id}/text/{artifact_id}")
    def rf_test_text(test_id: str, artifact_id: str, tail_lines: int = 300, contains: str = "",
                     who: str = Depends(ui_auth)) -> dict[str, Any]:
        lab().file(test_id, artifact_id)                      # must belong to this test
        return proc().artifact_text(artifact_id, tail_lines, contains)

    @app.get("/v1/rf/dashboard")
    def rf_dashboard(who: str = Depends(ui_auth)) -> dict[str, Any]:
        from ..scheduling.componentperf import dashboard
        return dashboard(lab())

    @app.get("/v1/rf/components/compare")
    def rf_components_compare(a: str, b: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        from ..scheduling.componentperf import compare_antennas
        return compare_antennas(lab(), a, b)

    @app.get("/v1/rf/components/{component_id}/performance")
    def rf_component_perf(component_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        from ..scheduling.componentperf import performance
        return performance(lab(), component_id)

    @app.post("/v1/rf/tests/filter-test")
    def rf_filter_test(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return lab().filter_test(body, who)

    @app.get("/v1/rf/tests/compare")
    def rf_test_compare(a: str, b: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return lab().compare(a, b)

    @app.get("/v1/rf/tests/presets")
    def rf_test_presets(receiver_id: str | None = None, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return {"presets": lab().presets(receiver_id)}

    @app.post("/v1/rf/tests/presets")
    def rf_test_preset_save(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return lab().save_preset(body)

    @app.delete("/v1/rf/tests/presets/{preset_id}")
    def rf_test_preset_delete(preset_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return lab().delete_preset(preset_id)

    @app.get("/v1/rf/tests")
    def rf_tests_list(receiver: str | None = None, tag: str | None = None, part: str | None = None,
                      fmin_mhz: float | None = None, fmax_mhz: float | None = None, since: str | None = None,
                      until: str | None = None, status: str | None = None, sort: str = "date",
                      test_type: str | None = None, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return {"tests": lab().list(receiver, tag, part, fmin_mhz, fmax_mhz, since, until, status, sort,
                                    test_type=test_type)}

    @app.post("/v1/rf/tests")
    def rf_test_start(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        rid = str(body.get("receiver_id") or "")
        return lab().start(rid, body, who, bool(body.get("dry_run")))

    @app.get("/v1/rf/tests/{test_id}")
    def rf_test_get(test_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return lab().detail(test_id)

    @app.post("/v1/rf/tests/{test_id}")
    def rf_test_update(test_id: str, body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return lab().update(test_id, body)

    @app.post("/v1/rf/tests/{test_id}/cancel")
    def rf_test_cancel(test_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return lab().cancel(test_id, who)

    @app.get("/v1/rf/tests/{test_id}/files/{artifact_id}")
    def rf_test_file(test_id: str, artifact_id: str, download: bool = False, who: str = Depends(ui_auth)) -> FileResponse:
        path, mime = lab().file(test_id, artifact_id)
        return FileResponse(path, media_type=mime, filename=path.name if download else None)

    @app.post("/v1/rf/tests/{test_id}/iq-link")
    def rf_test_iq_link(test_id: str, request: Request, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return lab().iq_link(test_id, str(request.base_url))

    @app.get("/v1/rf/tests/{test_id}/report", response_class=HTMLResponse)
    def rf_test_report(test_id: str, compare_with: str | None = None, who: str = Depends(ui_auth)) -> HTMLResponse:
        return HTMLResponse(lab().report(test_id, compare_with), headers={"Cache-Control": "no-store"})

    @app.post("/v1/rf/components/{component_id}/rename")
    def rf_rename(component_id: str, body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return rf().rename(component_id, str(body.get("new_id") or ""), body.get("base_version"), who)

    @app.get("/v1/rf/history")
    def rf_history(limit: int = 50, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return {"versions": rf().history(min(limit, 200))}

    @app.get("/v1/rf/versions/{version}")
    def rf_version(version: int, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return rf().version(version)

    # ---- Receiver inventory: discovery, identify, add, edit, verify
    inv_box: dict[str, Any] = {}

    def inv() -> Any:
        if "inv" not in inv_box:
            from ..inventory.service import InventoryService
            inv_box["inv"] = getattr(sched(), "inventory", None) or InventoryService(sched().s)
        return inv_box["inv"]

    @app.get("/v1/inventory")
    def inventory_get(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return inv().overview()

    @app.post("/v1/inventory/identify")
    def inventory_identify(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return inv().identify(str(body.get("usb_path") or ""), who, str(body.get("host") or "local"))

    @app.post("/v1/inventory/discover")
    def inventory_discover(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        """Passive discovery on this computer and on every enabled remote host (or one host)."""
        svc = inv()
        want = str(body.get("host") or "all")
        ids = ["local"] + [h["id"] for h in (svc.hosts.listing() if svc.hosts else []) if h["enabled"]]
        if want != "all":
            ids = [want]
        results = [svc.discover_host(h, who) for h in ids]
        return {"results": results, **svc.overview()}

    # ---- Receiver hosts (Settings -> Receiver hosts): remote Linux computers over SSH, key authentication only
    def hosts() -> Any:
        h = inv().hosts
        if h is None:
            raise HTTPException(503, "receiver hosts are not available")
        return h

    @app.get("/v1/hosts")
    def hosts_list(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return {"hosts": inv().overview()["hosts"]}

    @app.get("/v1/hosts/client-key")
    def hosts_client_key(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return hosts().client_key()

    @app.post("/v1/hosts/scan-key")
    def hosts_scan(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return hosts().scan(body)

    @app.post("/v1/hosts")
    def hosts_add(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return hosts().add(body, who)

    @app.post("/v1/hosts/{host_id}/test")
    def hosts_test(host_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return hosts().test(host_id, who)

    @app.post("/v1/hosts/{host_id}/host-key")
    def hosts_accept_key(host_id: str, body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return hosts().accept_key(host_id, body, who)

    @app.post("/v1/hosts/{host_id}")
    def hosts_update(host_id: str, body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return hosts().update(host_id, body, who)

    @app.delete("/v1/hosts/{host_id}")
    def hosts_delete(host_id: str, revision: int, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return hosts().delete(host_id, revision, who)

    @app.post("/v1/inventory/receivers")
    def inventory_add(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return inv().add(body, who)

    @app.post("/v1/inventory/receivers/{receiver_id}")
    def inventory_update(receiver_id: str, body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return inv().update(receiver_id, body, who)

    @app.delete("/v1/inventory/receivers/{receiver_id}")
    def inventory_delete(receiver_id: str, revision: int, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return inv().delete(receiver_id, revision, who)

    @app.get("/v1/inventory/receivers/{receiver_id}/events")
    def inventory_events(receiver_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return {"events": sched().s.inventory.events(receiver_id)}

    @app.post("/v1/inventory/receivers/{receiver_id}/verify")
    def inventory_verify(receiver_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        body = inv().verify_body(lab().options(receiver_id))
        return lab().start(receiver_id, body, who)

    # ---- Configuration: one API over the settings documents and the other persisted configuration
    def config_overview() -> dict[str, Any]:
        from ..db.migrate import current_version, sqlite_path
        svc = sched()
        s_ = svc.s
        cur = s_.rf.current if getattr(s_, "rf", None) is not None else None
        rows = s_.inventory.rows()
        rules = {rid: {"mode": rx.mode, "automatic": bool(rx.auto.get("enabled")),
                       "satellites": rx.auto.get("norad_ids") or [], "min_elevation_deg": rx.auto.get("min_elevation_deg"),
                       "format": rx.auto.get("format")} for rid, rx in s_.receivers.items()}
        b = s_.config.section("broker")
        return {"documents": s_.config_store.describe(),
                "managed": {
                    "signal_paths": {"label": "Signal paths (antennas, parts, chains, switch ports)", "edit": "the diagram",
                                     "version": cur["version"] if cur else None,
                                     "saved_at": cur["created_at"].isoformat() if cur else None},
                    "receivers": {"label": "Receivers", "edit": "Receivers",
                                  "count": len(rows), "from_web": sum(r["origin"] == "ui" for r in rows),
                                  "remote": sum(r["host"] != "local" for r in rows)},
                    "manual_jobs": {"label": "Manual recording jobs", "edit": "Slack or the jobs API",
                                    "note": "stored in the schedule database as they are booked"},
                    "scheduling": {"label": "Automatic settings in the config file", "edit": "the config file",
                                   "note": "older automatic settings per receiver; new automatic recording is set up in "
                                           "Scheduler → Automatic rules", "receivers": rules}},
                "bootstrap": {"config_file": str(s_.config.path), "data_root": str(s_.config.data_root),
                              "database": str(sqlite_path(s_.engine) or "(not SQLite)"),
                              "schema_version": current_version(s_.engine),
                              "listen": f"{b.get('host', '127.0.0.1')}:{b.get('port', 8751)}",
                              "note": "set in observatory.toml; needed before the database can be opened"}}

    @app.get("/v1/config")
    def config_get(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return config_overview()

    @app.get("/v1/config/export")
    def config_export(who: str = Depends(ui_auth)) -> JSONResponse:
        from ..settings.transfer import export
        body = export(sched().s)
        name = f"starlab-config-{body['exported_at'][:19].replace(':', '').replace('-', '')}.json"
        return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="{name}"',
                                           "Cache-Control": "no-store"})

    @app.post("/v1/config/import")
    def config_import(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        from ..settings.transfer import import_config
        return import_config(sched().s, body.get("export"), who, dry_run=body.get("dry_run", True) is not False)

    @app.get("/v1/config/backups")
    def config_backups(who: str = Depends(ui_auth)) -> dict[str, Any]:
        from ..db.migrate import list_backups
        return {"backups": list_backups(sched().s.engine)}

    @app.post("/v1/config/backups")
    def config_backup_now(who: str = Depends(ui_auth)) -> dict[str, Any]:
        from ..db.migrate import backup
        path = backup(sched().s.engine, "manual")
        if path is None:
            raise SettingsError("this database is not a SQLite file; back it up with the database's own tools", 501)
        return {"ok": True, "file": path.name, "bytes": path.stat().st_size}

    @app.get("/v1/config/{doc}")
    def config_doc(doc: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        d = sched().s.config_store.describe()
        if doc not in d:
            raise SettingsError(f"unknown settings document {doc!r}", 404)
        return d[doc]

    @app.put("/v1/config/{doc}")
    def config_save(doc: str, body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        s_ = sched().s
        return s_.config_store.save(doc, body.get("data"), body.get("base_version"), who, str(body.get("note") or ""),
                                    config=s_.config)

    @app.post("/v1/config/{doc}/validate")
    def config_validate(doc: str, body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return sched().s.config_store.save(doc, body.get("data"), None, who, dry_run=True)

    @app.get("/v1/config/{doc}/history")
    def config_history(doc: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return {"versions": sched().s.config_store.history(doc)}

    @app.post("/v1/config/{doc}/restore/{version}")
    def config_restore(doc: str, version: int, who: str = Depends(ui_auth)) -> dict[str, Any]:
        s_ = sched().s
        return s_.config_store.restore(doc, version, who, config=s_.config)

    # ---- First-run setup wizard
    def setup() -> Any:
        from ..settings.setup import Setup
        return Setup(sched().s)

    @app.get("/v1/setup")
    def setup_get(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return setup().state()

    @app.post("/v1/setup/step")
    def setup_step(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return setup().step(str(body.get("step") or ""), str(body.get("action") or ""), who)

    @app.post("/v1/setup/validate")
    def setup_validate(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return setup().validate()

    @app.post("/v1/setup/finish")
    def setup_finish(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return setup().finish(who)

    @app.post("/v1/setup/reopen")
    def setup_reopen(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return setup().reopen(who)

    # ---- Satellites and predicted passes (built-in predictor)
    def passes_svc() -> Any:
        from ..satellites.passes import PassService
        return PassService(sched().s)

    @app.get("/v1/satellites")
    def satellites_get(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return passes_svc().satellites()

    @app.post("/v1/satellites/refresh")
    def satellites_refresh(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return passes_svc().refresh()

    @app.get("/v1/passes/predicted")
    def passes_predicted(hours: float = 48, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return passes_svc().predicted(hours)

    @app.post("/v1/passes/book")
    def passes_book(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return passes_svc().schedule(int(body.get("norad") or 0), str(body.get("aos") or ""),
                                     str(body.get("receiver_id") or ""), who, bool(body.get("dry_run")))

    # ---- Scheduler page: rules, calendar, overrides, recording form (brief 6)
    from ..scheduling import schedview

    def replan() -> dict[str, Any]:
        """Apply a change to the schedule right away (planning failures are reported, never raised)."""
        svc = sched()
        fn = getattr(svc, "reconcile_rules", None)
        return fn(refresh=False) if fn else {}

    def _when(value: str | None, default: Any) -> Any:
        import datetime as dt
        if not value:
            return default
        try:
            t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise SchedulerError(f"{value!r} is not an ISO-8601 time", 400) from None
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)

    @app.get("/v1/scheduler/overview")
    def sched_overview(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return schedview.overview(sched().s, sched())

    @app.get("/v1/scheduler/reservations")
    def sched_reservations(who: str = Depends(ui_auth)) -> dict[str, Any]:
        """Receivers held by Starlab jobs right now (brief 7)."""
        r = getattr(sched().s, "reservations", None)
        return {"reservations": r.current() if r is not None else []}

    @app.get("/v1/scheduler/rules")
    def sched_rules(who: str = Depends(ui_auth)) -> dict[str, Any]:
        s = sched().s
        out = []
        for r in s.rules.list():
            from ..scheduling.rules import receiver_warnings
            out.append({**r, "warnings": receiver_warnings(s, r)})
        return {"rules": out, "last_reconcile": getattr(getattr(sched(), "rules", None), "last", None) or {}}

    @app.post("/v1/scheduler/rules/validate")
    def sched_rule_validate(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return sched().s.rules.check(body.get("rule"), body.get("rule_id"))

    @app.post("/v1/scheduler/rules/preview")
    def sched_rule_preview(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        s = sched().s
        res = s.rules.check(body.get("rule"), body.get("rule_id"))
        if not res["ok"]:
            return {**res, "passes": []}
        from ..scheduling.rules import RulePlanner
        planner = getattr(sched(), "rules", None) or RulePlanner(s)
        return {**res, **planner.preview({**res["rule"], "rule_id": body.get("rule_id") or ""},
                                         float(body.get("hours") or 48))}

    @app.post("/v1/scheduler/rules")
    def sched_rule_create(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        rule = sched().s.rules.create(body.get("rule"), who)
        return {"rule": rule, "reconcile": replan()}

    @app.put("/v1/scheduler/rules/{rule_id}")
    def sched_rule_update(rule_id: str, body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        rule = sched().s.rules.update(rule_id, body.get("rule"), body.get("revision"), who)
        return {"rule": rule, "reconcile": replan()}

    @app.delete("/v1/scheduler/rules/{rule_id}")
    def sched_rule_delete(rule_id: str, revision: int, who: str = Depends(ui_auth)) -> dict[str, Any]:
        out = sched().s.rules.delete(rule_id, revision, who)
        return {**out, "reconcile": replan()}

    @app.get("/v1/scheduler/rules/{rule_id}/history")
    def sched_rule_history(rule_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return {"history": sched().s.rules.history(rule_id)}

    @app.post("/v1/scheduler/reconcile")
    def sched_reconcile(who: str = Depends(ui_auth)) -> dict[str, Any]:
        svc = sched()
        fn = getattr(svc, "reconcile_rules", None)
        return fn(refresh=True) if fn else {}

    @app.get("/v1/scheduler/calendar")
    def sched_calendar(start: str | None = None, end: str | None = None, satellite: str | None = None,
                       receiver: str | None = None, state: str | None = None,
                       who: str = Depends(ui_auth)) -> dict[str, Any]:
        import datetime as dt
        s = sched().s
        now = s.clock()
        a = _when(start, now - dt.timedelta(days=1))
        b = _when(end, now + dt.timedelta(days=7))
        if b <= a or b - a > dt.timedelta(days=400):
            raise SchedulerError("the calendar range must be positive and at most 400 days", 400)
        return schedview.calendar(s, a, b, satellite, receiver, [x for x in (state or "").split(",") if x])

    @app.get("/v1/scheduler/jobs/{job_id}")
    def sched_job(job_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return schedview.job_detail(sched().s, job_id)

    @app.post("/v1/scheduler/jobs/{job_id}/change")
    def sched_job_change(job_id: str, body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        res = sched().s.modify(job_id, dict(body.get("changes") or {}), who, bool(body.get("dry_run")))
        if not body.get("dry_run"):
            replan()
        return res

    @app.post("/v1/scheduler/jobs/{job_id}/revert")
    def sched_job_revert(job_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        res = sched().s.revert(job_id, who)
        return {**res, "reconcile": replan(), "job": schedview.job_detail(sched().s, job_id)}

    @app.post("/v1/scheduler/jobs/{job_id}/cancel")
    def sched_job_cancel(job_id: str, body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        reason = str(body.get("reason") or "").strip() or f"cancelled by {who} on the Scheduler page"
        return sched().s.cancel(job_id, reason, who)

    @app.get("/v1/scheduler/form")
    def sched_form(norad: int, aos: str, receiver_id: str | None = None, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return schedview.form_for_pass(sched().s, norad, aos, receiver_id)

    @app.post("/v1/scheduler/manual")
    def sched_manual(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        res = schedview.book(sched().s, dict(body.get("form") or {}), who, bool(body.get("dry_run")))
        if not body.get("dry_run"):
            replan()
        return res

    # ---- Optional Slack integration (brief 9). Settings live in the "slack" settings document; credentials are
    # write-only here and stored in the secrets folder.
    def slack() -> Any:
        sv = getattr(sched(), "slack", None)
        if sv is None:
            raise BrokerError("the Slack integration is not available on this broker", 501)
        return sv

    @app.get("/v1/slack")
    def slack_get(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return slack().overview()

    @app.get("/v1/slack/manifest")
    def slack_manifest(download: bool = False, who: str = Depends(ui_auth)) -> Any:
        import json as _json

        from ..slack.manifest import manifest, problems
        sv = slack()
        m = manifest(sv.cfg(), str(sched().s.config.section("site").get("name") or "Observatory"))
        if download:
            return Response(_json.dumps(m, indent=2) + "\n", media_type="application/json",
                            headers={"Content-Disposition": 'attachment; filename="starlab-slack-manifest.json"'})
        return {"manifest": m, "problems": problems(sv.cfg())}

    @app.put("/v1/slack/credentials")
    def slack_credentials(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        from ..slack.credentials import CredentialError
        try:
            out = slack().creds.update(dict(body or {}))
        except CredentialError as exc:
            raise BrokerError(str(exc), 400) from None
        from ..logjson import log_event
        log_event(logging.getLogger("aobs.slack"), "slack credentials changed", by=who,
                  fields=sorted(body or {}))      # names only, never values
        return {"credentials": out}

    @app.post("/v1/slack/test")
    def slack_test(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return slack().test(bool((body or {}).get("post_message")))

    @app.post("/v1/slack/apply")
    def slack_apply(who: str = Depends(ui_auth)) -> dict[str, Any]:
        """Start, restart or stop the integration with the saved settings and credentials (no broker restart)."""
        return slack().reload()

    from ..web.integrations_api import register as register_integrations
    register_integrations(app, sched, ui_auth)            # Google Calendar from the web page, shared integrations UI

    @app.post("/slack/commands")
    async def slack_commands(request: Request) -> JSONResponse:
        """Slash commands in HTTP mode. No bearer token: every request must carry a valid Slack signature."""
        body = await request.body()
        if len(body) > 64 * 1024:
            return JSONResponse({"error": "too large"}, status_code=413)
        code, out = slack().handle_http(dict(request.headers), body)
        return JSONResponse(out, status_code=code)

    # ---- Recording archive (brief 8): library, outcomes, decoding, files, storage and retention, profiles
    @app.get("/v1/archive/recordings")
    def archive_recordings(q: str | None = None, receiver: str | None = None, satellite: str | None = None,
                           host: str | None = None, status: str | None = None, outcome: str | None = None,
                           where: str | None = None, source: str | None = None, since: str | None = None,
                           until: str | None = None, format: str | None = None, sort: str = "newest", page: int = 1,
                           per_page: int = 25, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return cat().recordings(receiver=receiver, satellite=satellite, since=since, until=until, fmt=format,
                                status=status, page=page, per_page=per_page, q=q, host=host, outcome=outcome,
                                where=where, source=source, sort=sort)

    @app.get("/v1/archive/recordings/{recording_id}")
    def archive_detail(recording_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return cat().detail(recording_id)

    @app.post("/v1/archive/recordings/{recording_id}/process")
    def archive_process(recording_id: str, body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        kind = body.get("kind")
        if kind not in ("decode", "analyze", "transfer"):
            raise BrokerError("kind must be decode, analyze or transfer", 400)
        out = []
        if kind == "transfer" or body.get("transfer_first"):
            out.append(proc().submit(recording_id, "transfer", {}, "web", who))
        if kind != "transfer":
            out.append(proc().submit(recording_id, kind, body.get("params") or {}, "web", who))
        return {"attempts": out}

    @app.post("/v1/archive/recordings/{recording_id}/keep")
    def archive_keep(recording_id: str, body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        from ..scheduling.retention import set_keep
        return set_keep(sched().s, recording_id, bool(body.get("keep")), who)

    @app.post("/v1/archive/attempts/{attempt_id}/cancel")
    def archive_cancel_attempt(attempt_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return proc().cancel(attempt_id)

    @app.get("/v1/archive/files/{artifact_id}")
    def archive_file(artifact_id: str, download: bool = False, who: str = Depends(ui_auth)) -> FileResponse:
        path, mime = proc().artifact_file(artifact_id)
        inline = mime.startswith("image/") and not download
        return FileResponse(path, media_type=mime, filename=path.name,
                            content_disposition_type="inline" if inline else "attachment",
                            headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})

    @app.get("/v1/archive/files/{artifact_id}/text")
    def archive_file_text(artifact_id: str, tail_lines: int = 400, contains: str = "",
                          who: str = Depends(ui_auth)) -> dict[str, Any]:
        return proc().artifact_text(artifact_id, tail_lines, contains)

    @app.post("/v1/archive/files/{artifact_id}/link")
    def archive_file_link(artifact_id: str, request: Request, who: str = Depends(ui_auth)) -> dict[str, Any]:
        """A signed, expiring download link (for files too large to pass through the page, such as raw IQ)."""
        proc()._artifact(artifact_id)                 # same checks as any file: indexed, present, under the roots
        return cat().link(artifact_id, 3600, str(request.base_url))

    @app.get("/v1/archive/storage")
    def archive_storage(who: str = Depends(ui_auth)) -> dict[str, Any]:
        from ..scheduling.archive import storage_usage
        from ..scheduling.retention import Retention
        s = sched().s
        plan = Retention(s).plan()
        return {**storage_usage(s), "retention": plan}

    @app.post("/v1/archive/retention/apply")
    def archive_retention_apply(who: str = Depends(ui_auth)) -> dict[str, Any]:
        from ..scheduling.retention import Retention
        r = Retention(sched().s)
        if not r.enabled:
            raise BrokerError("retention is switched off in Settings → Storage; nothing was deleted", 409)
        return {"deleted": r.apply()}

    def profiles() -> Any:
        store = getattr(sched().s, "profiles", None)
        if store is None:
            raise BrokerError("decoder profiles are not available on this broker", 501)
        return store

    @app.get("/v1/archive/profiles")
    def archive_profiles(who: str = Depends(ui_auth)) -> dict[str, Any]:
        from ..satellites import predict
        s = sched().s
        return {"profiles": profiles().list(), "pipelines": sorted(s.pipelines),
                "catalog": [{"norad": n, "name": c["name"], "mode": c.get("mode")} for n, c in predict.CATALOG.items()]}

    @app.post("/v1/archive/profiles/validate")
    def archive_profile_validate(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return profiles().check(body.get("profile"), body.get("profile_id"))

    @app.post("/v1/archive/profiles")
    def archive_profile_create(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return {"profile": profiles().create(body.get("profile"), who)}

    @app.put("/v1/archive/profiles/{profile_id}")
    def archive_profile_update(profile_id: str, body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return {"profile": profiles().update(profile_id, body.get("profile"), body.get("revision"), who)}

    @app.delete("/v1/archive/profiles/{profile_id}")
    def archive_profile_delete(profile_id: str, revision: int, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return profiles().delete(profile_id, revision, who)

    @app.get("/v1/archive/profiles/{profile_id}/history")
    def archive_profile_history(profile_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return {"history": profiles().history(profile_id)}

    # ---- Listening sessions (SDR++ server)
    def sessions() -> Any:
        sm = getattr(sched().s, "sessions", None)
        if sm is None:
            raise BrokerError("listening sessions are not available on this broker", 501)
        return sm

    @app.get("/v1/sessions")
    def sessions_get(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return {"sessions": sessions().listing(), "binds": sessions().allowed_binds()}

    @app.get("/v1/sessions/eligibility/{receiver_id}")
    def sessions_eligibility(receiver_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return sessions().eligibility(receiver_id)

    @app.post("/v1/sessions")
    def sessions_start(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return sessions().start(str(body.get("receiver_id") or ""), who, body.get("minutes"), body.get("bind"),
                                body.get("port"), body.get("end_before_passes"), body.get("warn_minutes"))

    @app.post("/v1/sessions/{session_id}/stop")
    def sessions_stop(session_id: str, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return sessions().stop(session_id, who, "stopped from the web page")

    @app.get("/v1/notifications", dependencies=deps)
    def notifications(after: int = 0) -> dict[str, Any]:
        return {"notifications": sched().s.notifications(after)}

    @app.get("/v1/scheduler/status", dependencies=deps)
    def scheduler_status() -> dict[str, Any]:
        from ..scheduling.service import shadow_report
        svc = sched()
        return {"modes": {r.id: r.mode for r in svc.s.receivers.values()}, "last_plan": svc.last_plan,
                "last_rules": getattr(getattr(svc, "rules", None), "last", None), "last_calendar": svc.last_calendar,
                "shadow": shadow_report(svc.s)}

    return app
