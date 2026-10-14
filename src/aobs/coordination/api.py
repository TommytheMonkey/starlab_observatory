"""HTTP API for the broker. Every endpoint needs `Authorization: Bearer <broker token>`."""

from __future__ import annotations

import hashlib
import hmac
import pathlib
import secrets
import threading
import time
from typing import Any, Callable

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import radio
from .broker import Broker, BrokerError


class LeaseIn(BaseModel):
    owner: str = Field(min_length=1, max_length=64)
    purpose: str = ""
    ttl_s: int = 600
    receiver: str = "OBS-R05"
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


class CancelIn(BaseModel):
    reason: str = "cancelled by owner"
    actor: str = "slack"


def create_app(broker: Broker, api_token: str, watchdog_s: float = 5.0,
               passes: Callable[[float], list[dict[str, Any]]] | None = None, scheduler: Any = None) -> FastAPI:
    app = FastAPI(title="aobs receiver broker", version="1")
    stop = threading.Event()

    def auth(authorization: str = Header(default="")) -> None:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(value, api_token):
            raise HTTPException(401, "bad or missing bearer token")

    # ---- RF editor sign-in: a signed, expiring link (posted by the bot on request) sets a session cookie.
    def rf_key() -> bytes:
        svc = sched()
        path = svc.s.config.data_root / "secrets" / "rf_ui_key"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(secrets.token_hex(32))
            path.chmod(0o600)
        return path.read_text().strip().encode()

    def rf_sig(purpose: str, exp: int) -> str:
        return hmac.new(rf_key(), f"{purpose}.{exp}".encode(), hashlib.sha256).hexdigest()[:40]

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
        raise HTTPException(401, "not signed in: ask the observatory bot for an RF editor link")

    @app.exception_handler(BrokerError)
    async def broker_error(_: Request, exc: BrokerError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    @app.exception_handler(radio.RadioError)
    async def radio_error(_: Request, exc: radio.RadioError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    from ..scheduling.store import SchedulerError

    @app.exception_handler(SchedulerError)
    async def scheduler_error(_: Request, exc: SchedulerError) -> JSONResponse:
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

    @app.get("/v1/rf/link", dependencies=deps)
    def rf_link(request: Request) -> dict[str, Any]:
        exp = int(time.time()) + RF_LINK_S
        base = str(request.base_url).rstrip("/")
        return {"url": f"{base}/rf/login/{exp}/{rf_sig('login', exp)}", "expires_in_s": RF_LINK_S,
                "session_days": RF_SESSION_S // 86400}

    @app.get("/rf/login/{exp}/{sig}")
    def rf_login(exp: int, sig: str) -> RedirectResponse:
        if exp < time.time() or not hmac.compare_digest(sig, rf_sig("login", exp)):
            raise HTTPException(403, "this sign-in link has expired; ask the bot for a new one")
        until = int(time.time()) + RF_SESSION_S
        resp = RedirectResponse("/rf", status_code=303)
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
        keep = ("state", "connected", "mode", "iq_health", "current_job", "next_job", "last_successful_recording",
                "last_error", "legacy_unit", "legacy_unit_state")
        return {k: st.get(k) for k in keep}

    @app.get("/v1/rf")
    def rf_get(who: str = Depends(ui_auth)) -> dict[str, Any]:
        v = rf().view()
        v["status"] = {rid: rx_status(rid) for rid in sched().s.receivers}
        v["receiver_info"] = {rid: {"serial": rx.serial, "hardware": rx.hardware, "driver": rx.driver, "mode": rx.mode,
                                    "bias_tee_supported": rx.bias_tee_supported, "bias_tee_fixed": rx.bias_tee_fixed}
                              for rid, rx in sched().s.receivers.items()}
        v["usage"] = rf_usage()
        v["switch_board"] = rf().board_status()
        v["viewer"] = who
        return v

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
                      who: str = Depends(ui_auth)) -> dict[str, Any]:
        return {"tests": lab().list(receiver, tag, part, fmin_mhz, fmax_mhz, since, until, status, sort)}

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

    @app.get("/v1/rf/history")
    def rf_history(limit: int = 50, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return {"versions": rf().history(min(limit, 200))}

    @app.get("/v1/rf/versions/{version}")
    def rf_version(version: int, who: str = Depends(ui_auth)) -> dict[str, Any]:
        return rf().version(version)

    @app.get("/v1/notifications", dependencies=deps)
    def notifications(after: int = 0) -> dict[str, Any]:
        return {"notifications": sched().s.notifications(after)}

    @app.get("/v1/scheduler/status", dependencies=deps)
    def scheduler_status() -> dict[str, Any]:
        from ..scheduling.service import shadow_report
        svc = sched()
        return {"modes": {r.id: r.mode for r in svc.s.receivers.values()}, "last_plan": svc.last_plan,
                "last_calendar": svc.last_calendar, "shadow": shadow_report(svc.s)}

    return app
