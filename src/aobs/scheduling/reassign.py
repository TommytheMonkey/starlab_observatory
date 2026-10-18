"""Recordings left on a receiver that is being removed or disabled, or already has been.

Before a receiver goes away the UI lists its upcoming recordings and asks, for each one, which receiver should
record it instead (or to cancel it). Receivers can also disappear without that question being asked (removed from
observatory.toml, disabled through the API); their recordings are "stranded" and the station page shows them
with the same choices. Nothing is ever moved without a person deciding: the replacement receiver is only
pre-selected.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select

from ..db.schema import inv_events, inv_receivers
from .rules import rule_receivers
from .store import SchedulerError

# statuses of recordings that would still record (skipped / preempted ones need no decision)
WAITING = ("scheduled", "predicted")
LIMIT = 200


def upcoming(s: Any, rid: str) -> list[dict[str, Any]]:
    """Recordings booked on this receiver that have not started yet."""
    now = s.clock()
    return [j for j in s.jobs(receiver=rid, status=list(WAITING), start=now, limit=LIMIT)
            if j["receiver_id"] == rid and dt.datetime.fromisoformat(j["record_end"]) > now and j["source"] != "legacy"]


def _identity(s: Any, rid: str) -> dict[str, Any]:
    """Serial, port and driver of a receiver, from the inventory or (once deleted) its last inventory event."""
    with s.engine.connect() as conn:
        row = conn.execute(select(inv_receivers).where(inv_receivers.c.id == rid)).mappings().first()
        if row is not None:
            return {"serial": row["serial"], "usb_path": row["usb_path"] or "", "host": row["host"], "driver": row["driver"]}
        ev = conn.execute(select(inv_events.c.detail).where(inv_events.c.receiver_id == rid)
                          .order_by(inv_events.c.id.desc()).limit(10)).all()
    for (detail,) in ev:
        d = detail or {}
        if d.get("serial") or d.get("usb_path"):
            return {"serial": d.get("serial") or "", "usb_path": d.get("usb_path") or "", "host": d.get("host") or "local",
                    "driver": d.get("driver") or ""}
    return {}


def replacement(s: Any, rid: str) -> dict[str, str] | None:
    """The receiver that took this one's place: the same hardware under a new ID, or new hardware on its USB port."""
    me = _identity(s, rid)
    if not me:
        return None
    others = [rx for rx in s.receivers.values() if rx.id != rid]
    if me.get("serial"):
        for rx in others:
            if rx.serial == me["serial"] and (not me.get("driver") or rx.driver == me["driver"]):
                return {"receiver_id": rx.id, "why": f"same hardware (serial {rx.serial})"}
    if me.get("usb_path"):
        for rx in others:
            if rx.usb_path == me["usb_path"] and (rx.host or "local") == (me.get("host") or "local"):
                return {"receiver_id": rx.id, "why": f"plugged into the same USB port ({rx.usb_path})"}
    return None


def _candidates(s: Any, job: dict[str, Any], gone: str, repl: dict[str, str] | None) -> list[str]:
    """Receivers to offer, best first: the replacement, the rule's backups, the same kind of hardware, the rest."""
    order: list[str] = []
    if repl:
        order.append(repl["receiver_id"])
    if job.get("rule_id"):
        try:
            order += rule_receivers(s.rules.get(job["rule_id"], include_deleted=True))
        except Exception:  # noqa: BLE001 - a deleted or broken rule only loses its preference
            pass
    driver = ((job.get("spec") or {}).get("receiver") or {}).get("driver")
    order += sorted((rx.id for rx in s.receivers.values() if rx.driver == driver), key=str)
    order += sorted(s.receivers, key=str)
    seen: list[str] = []
    for r in order:
        if r != gone and r in s.receivers and r not in seen:
            seen.append(r)
    return seen


def _brief(j: dict[str, Any]) -> dict[str, Any]:
    return {"job_id": j["job_id"], "name": j["target"] or j["job_name"], "status": j["status"], "source": j["source"],
            "rule_id": j.get("rule_id"), "automatic": bool(j.get("rule_id")) or j["source"] == "auto",
            "record_start": j["record_start"], "record_end": j["record_end"]}


def plan(s: Any, rid: str) -> dict[str, Any]:
    """The upcoming recordings on `rid` and, for each, which other receivers could take it (checked like a real
    move, without changing anything) and which one to pre-select."""
    repl = replacement(s, rid)
    jobs = []
    for j in upcoming(s, rid):
        options = []
        for r in _candidates(s, j, rid, repl):
            try:
                res = s.modify(j["job_id"], {"receiver_id": r}, "reassign-check", dry_run=True)
                options.append({"receiver_id": r, "name": s.receivers[r].name, "ok": True,
                                "warnings": res.get("warnings") or []})
            except SchedulerError as exc:
                options.append({"receiver_id": r, "name": s.receivers[r].name, "ok": False,
                                "reason": str(exc).removeprefix("Not changed: ")})
        ok = [o["receiver_id"] for o in options if o["ok"]]
        jobs.append({**_brief(j), "options": options, "suggested": ok[0] if ok else None})
    return {"receiver_id": rid, "present": rid in s.receivers, "replacement": repl, "jobs": jobs}


def stranded(s: Any) -> list[dict[str, Any]]:
    """Upcoming recordings whose receiver no longer exists or is disabled, grouped by that receiver."""
    now = s.clock()
    gone = sorted({j["receiver_id"] for j in s.jobs(status=list(WAITING), start=now, limit=1000)
                   if j["receiver_id"] not in s.receivers and j["source"] != "legacy"})
    return [p for p in (plan(s, rid) for rid in gone) if p["jobs"]]


def stranded_counts(s: Any) -> dict[str, int]:
    """Receiver -> number of stranded recordings, cheap enough for every page refresh (no move checks)."""
    out: dict[str, int] = {}
    for j in s.jobs(status=list(WAITING), start=s.clock(), limit=1000):
        if j["receiver_id"] not in s.receivers and j["source"] != "legacy":
            out[j["receiver_id"]] = out.get(j["receiver_id"], 0) + 1
    return out


def apply(s: Any, decisions: list[dict[str, Any]], actor: str) -> dict[str, Any]:
    """Carry out the person's choices: {"job_id", "action": "move" | "cancel", "receiver_id"} each.

    Every decision is applied on its own, so one that can't be done (the receiver became busy, the recording
    starts too soon) is reported without undoing the others."""
    results = []
    for d in decisions[:LIMIT]:
        jid, act = str(d.get("job_id") or ""), d.get("action")
        try:
            job = s.get(jid)
            if job is None:
                raise SchedulerError(f"no job {jid}", 404)
            was = job["receiver_id"]
            if act == "move":
                to = str(d.get("receiver_id") or "")
                if to not in s.receivers:
                    raise SchedulerError(f"no receiver {to or '(none chosen)'}", 400)
                s.modify(jid, {"receiver_id": to, "note": f"moved from {was}, which was removed"}, actor)
                results.append({"job_id": jid, "ok": True, "action": "move", "from": was, "to": to})
            elif act == "cancel":
                s.cancel(jid, f"cancelled by {actor}: its receiver {was} was removed", actor)
                results.append({"job_id": jid, "ok": True, "action": "cancel", "from": was})
            else:
                raise SchedulerError(f"unknown action {act!r} (move or cancel)", 400)
        except SchedulerError as exc:
            results.append({"job_id": jid, "ok": False, "action": act, "error": str(exc)})
    return {"ok": all(r["ok"] for r in results), "results": results}
