"""Integrations set up from the web page (setup wizard step 8 and Settings): Google Calendar, plus the shared
integrations script that the wizard and Settings both load. Slack keeps its own /v1/slack endpoints (brief 9).
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Callable

from fastapi import Depends, FastAPI
from fastapi.responses import Response

from ..coordination.broker import BrokerError
from ..logjson import log_event

LOG = logging.getLogger("aobs.calendar")
SCRIPT = pathlib.Path(__file__).resolve().parent / "integrations.js"


def register(app: FastAPI, sched: Callable[[], Any], ui_auth: Callable[..., str]) -> None:
    from ..calendar import setup as cs

    def run(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except cs.CalendarSetupError as exc:
            raise BrokerError(str(exc), exc.status) from None

    def calendar_status() -> dict[str, Any]:
        svc = sched()
        return cs.status(svc.s.config, svc)

    @app.get("/ui/integrations.js")
    def integrations_script() -> Response:          # read per request, like the pages, so UI edits need no restart
        return Response(SCRIPT.read_text(), media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/v1/integrations")
    def integrations_overview(who: str = Depends(ui_auth)) -> dict[str, Any]:
        svc = sched()
        cal = cs.status(svc.s.config, svc)
        sl = getattr(svc, "slack", None)
        slack = sl.overview() if sl is not None else {"enabled": False, "running": False}
        return {"calendar": {"enabled": cal["enabled"] and cal["publish"], "running": cal["running"],
                             "calendar_id": cal["calendar_id"]},
                "slack": {"enabled": bool(slack.get("enabled")), "running": bool(slack.get("running"))}}

    @app.get("/v1/integrations/calendar")
    def calendar_get(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return calendar_status()

    @app.put("/v1/integrations/calendar/key")
    def calendar_key(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        info = run(lambda: cs.store_key(sched().s.config, (body or {}).get("key_json")))
        log_event(LOG, "calendar key uploaded", by=who, client_email=info.get("client_email"))
        return calendar_status()

    @app.delete("/v1/integrations/calendar/key")
    def calendar_key_delete(who: str = Depends(ui_auth)) -> dict[str, Any]:
        run(lambda: cs.remove_key(sched().s.config))
        log_event(LOG, "calendar key removed", by=who)
        return calendar_status()

    @app.post("/v1/integrations/calendar/test")
    def calendar_test(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        return cs.test_connection(sched().s.config, str((body or {}).get("calendar_id") or ""))

    @app.post("/v1/integrations/calendar/enable")
    def calendar_enable(body: dict[str, Any], who: str = Depends(ui_auth)) -> dict[str, Any]:
        res = cs.turn_on(sched().s, str((body or {}).get("calendar_id") or ""), who)
        return {**res, "status": calendar_status()}

    @app.post("/v1/integrations/calendar/disable")
    def calendar_disable(who: str = Depends(ui_auth)) -> dict[str, Any]:
        return {**cs.turn_off(sched().s, who), "status": calendar_status()}
