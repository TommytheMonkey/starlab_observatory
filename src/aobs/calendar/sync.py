"""Idempotent calendar synchronization.

Idempotency layers:
  1. Deterministic event ids derived from the pass id (insert of an existing id -> 409 -> update).
  2. calendar_events table stores the last body hash; unchanged bodies are not re-sent.
  3. Every managed event carries extendedProperties.private.aobs_pass_id for audit/recovery.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import Engine, and_, or_, select

from ..config import Config
from ..db.schema import calendar_events, decoded_artifacts, satellite_passes
from ..logjson import log_event
from ..timeutil import utc_now
from .google_api import CalendarError
from .render import body_hash, event_id, render_event

LOG = logging.getLogger("aobs.calendar")


class Client(Protocol):
    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None: ...
    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]: ...
    def update_event(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class Action:
    kind: str           # create | update | noop
    pass_id: str
    event_id: str
    body: dict[str, Any]
    hash: str
    status: str


def plan(config: Config, engine: Engine, calendar_id: str, now: dt.datetime | None = None,
         keep_past: dt.timedelta = dt.timedelta(hours=24)) -> list[Action]:
    now = now or utc_now()
    cal = config.section("calendar")
    horizon = now + dt.timedelta(hours=float(config.section("forecast").get("horizon_hours", 72)))
    actions = []
    with engine.connect() as conn:
        rows = conn.execute(
            select(satellite_passes, calendar_events.c.body_hash, calendar_events.c.calendar_id.label("cal_id"))
            .select_from(satellite_passes.outerjoin(calendar_events, calendar_events.c.pass_id == satellite_passes.c.id))
            .where(and_(satellite_passes.c.capture_start <= horizon,
                        satellite_passes.c.capture_end >= now - keep_past))
            .where(or_(satellite_passes.c.in_forecast.is_(True), calendar_events.c.pass_id.is_not(None)))
            .order_by(satellite_passes.c.capture_start)
        ).mappings().all()
        for row in rows:
            decoded = [r.path for r in conn.execute(select(decoded_artifacts.c.path).where(and_(
                decoded_artifacts.c.pass_id == row["id"], decoded_artifacts.c.verified.is_(True))))]
            body = render_event(row, config.display_tz, int(cal.get("event_minutes", 10)),
                                str(cal.get("links_base_url", "")), decoded)
            h = body_hash(body)
            if row["body_hash"] is None or row["cal_id"] != calendar_id:
                kind = "create"
            elif row["body_hash"] != h:
                kind = "update"
            else:
                kind = "noop"
            actions.append(Action(kind, row["id"], event_id(row["id"]), body, h, row["status"]))
    return actions


def apply(engine: Engine, client: Client, calendar_id: str, actions: list[Action], now: dt.datetime | None = None) -> dict[str, int]:
    now = now or utc_now()
    counts = {"create": 0, "update": 0, "noop": 0, "error": 0}
    for a in actions:
        if a.kind == "noop":
            counts["noop"] += 1
            continue
        try:
            if a.kind == "create":
                try:
                    result = client.insert_event(calendar_id, a.body)
                except CalendarError as exc:
                    if exc.status != 409:
                        raise
                    # Id already exists (our earlier insert, or a deleted event): overwrite it.
                    body = {k: v for k, v in a.body.items() if k != "id"}
                    result = client.update_event(calendar_id, a.event_id, body)
            else:
                body = {k: v for k, v in a.body.items() if k != "id"}
                try:
                    result = client.update_event(calendar_id, a.event_id, body)
                except CalendarError as exc:
                    if exc.status != 404:
                        raise
                    result = client.insert_event(calendar_id, a.body)
            _store(engine, a, calendar_id, result.get("etag"), None, now)
            counts[a.kind] += 1
            log_event(LOG, "calendar event synced", action=a.kind, pass_id=a.pass_id, event_id=a.event_id, status=a.status)
        except Exception as exc:  # noqa: BLE001 - one bad event must not stop the others
            counts["error"] += 1
            _store(engine, a, calendar_id, None, str(exc)[:500], now, failed=True)
            log_event(LOG, "calendar sync failed", logging.ERROR, action=a.kind, pass_id=a.pass_id, error=str(exc)[:300])
    return counts


def _store(engine: Engine, a: Action, calendar_id: str, etag: str | None, error: str | None,
           now: dt.datetime, failed: bool = False) -> None:
    with engine.begin() as conn:
        existing = conn.execute(select(calendar_events.c.pass_id, calendar_events.c.body_hash)
                                .where(calendar_events.c.pass_id == a.pass_id)).first()
        values = {"calendar_id": calendar_id, "event_id": a.event_id, "last_error": error, "updated_at": now, "state": "active"}
        if not failed:
            values.update(body_hash=a.hash, remote_etag=etag, last_synced_at=now)
        if existing:
            conn.execute(calendar_events.update().where(calendar_events.c.pass_id == a.pass_id).values(**values))
        elif not failed:
            conn.execute(calendar_events.insert().values(pass_id=a.pass_id, created_at=now, **values))
