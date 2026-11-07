"""Which receiver a new recording should use, so the recording form and bulk booking start with a sensible tick.

The ranking, best first:
  1. receivers whose antenna (from the RF layout's min/max MHz) covers the frequency, narrowest range first;
  2. the receiver that most recently COMPLETED a recording of the same satellite;
  3. receivers with a reservation overlapping the setup-to-teardown window are left out entirely;
  4. otherwise, the receiver used most recently.

Only a suggestion: the person can tick anything else before booking, and the normal job checks still apply.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from sqlalchemy import select

from ..db.schema import sched_jobs
from .rflayout import SWITCH_ID, full_chain
from .rules import owned_elsewhere

USED = ("recording", "recorded", "processing", "completed", "failed", "partial")   # the receiver really ran
HISTORY_ROWS = 500


def _mhz(hz: float) -> str:
    return f"{hz / 1e6:.4f}".rstrip("0").rstrip(".") + " MHz"


def _range(lo: float | None, hi: float | None) -> str:
    if lo is not None and hi is not None:
        return f"{lo:g}–{hi:g} MHz"
    return f"from {lo:g} MHz" if lo is not None else f"up to {hi:g} MHz"


def _antennas(layout: dict[str, Any], rid: str) -> list[dict[str, Any]]:
    """The antennas this receiver can listen on now: its own, a manual switch's selected one, or every ANTSW-01 port."""
    comps = layout.get("components") or {}
    chain = full_chain(layout, rid)
    head = comps.get(chain[0]) if chain else None
    if head is None:
        return []
    if head["kind"] == "antenna":
        return [head]
    if head["kind"] == "switch":
        ports = head.get("ports") or {}
        ids = list(ports.values()) if head["id"] == SWITCH_ID else [ports.get(str(head.get("selected_port")))]
        return [comps[a] for a in ids if a and a in comps and comps[a].get("kind") == "antenna"]
    return []


def antenna_fit(layout: dict[str, Any] | None, rid: str, freq_hz: float) -> dict[str, Any] | None:
    """The narrowest working antenna on this receiver whose range covers freq_hz, or None. An antenna without
    any range entered, or marked broken, never counts as covering."""
    if not layout or rid not in (layout.get("receivers") or {}):
        return None
    mhz = freq_hz / 1e6
    best = None
    for a in _antennas(layout, rid):
        lo, hi = a.get("min_mhz"), a.get("max_mhz")
        if (lo is None and hi is None) or a.get("status") == "broken":
            continue
        if (lo is not None and mhz < lo) or (hi is not None and mhz > hi):
            continue
        width = (hi - lo) if lo is not None and hi is not None else math.inf
        if best is None or width < best["width_mhz"]:
            best = {"id": a["id"], "name": a.get("name") or a["id"], "min_mhz": lo, "max_mhz": hi, "width_mhz": width,
                    "switched": head_is_switch(layout, rid)}
    return best


def head_is_switch(layout: dict[str, Any], rid: str) -> bool:
    chain = full_chain(layout, rid)
    return bool(chain) and chain[0] == SWITCH_ID


def _norad(spec: dict[str, Any]) -> int | None:
    v = ((spec or {}).get("metadata") or {}).get("norad") or ((spec or {}).get("target") or {}).get("norad_id")
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


def _history(s: Any, norad: int | None) -> tuple[dict[str, dt.datetime], dict[str, dt.datetime]]:
    """Per receiver: when it last completed this satellite, and when it was last used for anything."""
    sat: dict[str, dt.datetime] = {}
    used: dict[str, dt.datetime] = {}
    now = s.clock()
    q = (select(sched_jobs.c.receiver_id, sched_jobs.c.status, sched_jobs.c.source, sched_jobs.c.record_start,
                sched_jobs.c.record_end, sched_jobs.c.spec)
         .where(sched_jobs.c.status.in_(USED), sched_jobs.c.record_start <= now)
         .order_by(sched_jobs.c.record_start.desc()).limit(HISTORY_ROWS))
    with s.engine.connect() as conn:
        rows = conn.execute(q).mappings().all()
    for r in rows:
        rid = r["receiver_id"]
        start = r["record_start"]
        if start is not None and start.tzinfo is None:
            start = start.replace(tzinfo=dt.timezone.utc)
        if rid and start is not None and rid not in used:
            used[rid] = start
        done = r["status"] == "completed" or (r["status"] == "recorded" and r["source"] == "legacy")
        if norad and done and rid and rid not in sat and _norad(r["spec"]) == norad:
            end = r["record_end"] or start
            sat[rid] = end if end.tzinfo else end.replace(tzinfo=dt.timezone.utc)
    return sat, used


def suggest(s: Any, frequency_hz: float | None, norad: int | None, start: dt.datetime | None, end: dt.datetime | None,
            pre_roll_s: float = 30, post_roll_s: float = 30, sat_name: str | None = None) -> dict[str, Any]:
    """Rank the bookable receivers for one recording. Returns the pick (receiver_ids, at most one), the antenna
    to select on a switched receiver, a one-line reason per pick, and why any receiver was left out."""
    layout = ((getattr(s, "rf", None) and s.rf.current) or {}).get("layout")
    cands = [rx for rx in s.receivers.values() if (rx.schedulable or rx.simulated) and not owned_elsewhere(rx)]
    excluded: dict[str, str] = {}
    if start is not None and end is not None:
        a = start - dt.timedelta(seconds=max(0.0, float(pre_roll_s or 0)))
        b = end + dt.timedelta(seconds=max(0.0, float(post_roll_s or 0)))
        with s.engine.connect() as conn:
            for rx in cands:
                hit = s.conflicts(conn, rx.serial, a, b)
                if hit:
                    j = hit[0]
                    excluded[rx.id] = f"busy: {j['job_name'] or j['job_id']} is booked in that window"
    sat_last, used_last = _history(s, norad)
    label = sat_name or (f"NORAD {norad}" if norad else "")
    ranked = []
    for rx in cands:
        if rx.id in excluded:
            continue
        fit = antenna_fit(layout, rx.id, frequency_hz) if frequency_hz else None
        ranked.append({"receiver_id": rx.id, "fit": fit, "sat_at": sat_last.get(rx.id), "used_at": used_last.get(rx.id)})

    def key(c: dict[str, Any]) -> tuple[Any, ...]:
        never = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        return (c["fit"] is None, c["fit"]["width_mhz"] if c["fit"] else math.inf,
                -(c["sat_at"] or never).timestamp(), -(c["used_at"] or never).timestamp(), c["receiver_id"])
    ranked.sort(key=key)

    out: list[dict[str, Any]] = []
    for c in ranked:
        fit, basis, at = c["fit"], "first", None
        if fit:
            basis = "antenna"
            text = f"{fit['name']} ({_range(fit['min_mhz'], fit['max_mhz'])}) covers {_mhz(frequency_hz)}"
            wider = [x for x in ranked if x["fit"] and x is not c]
            if wider and all(x["fit"]["width_mhz"] > fit["width_mhz"] for x in wider):
                text += ", the narrowest antenna that does"
            if c["sat_at"]:
                text += f"; last completed {label}"
                at = c["sat_at"].isoformat()
        elif c["sat_at"]:
            basis, at = "satellite", c["sat_at"].isoformat()
            text = f"Last completed a {label} recording"
        elif c["used_at"]:
            basis, at = "recent", c["used_at"].isoformat()
            text = "Most recently used receiver"
        else:
            text = "First free receiver (no antenna range or history to go on)"
        out.append({"receiver_id": c["receiver_id"], "basis": basis, "reason": text, "at": at,
                    "antenna": fit["id"] if fit and fit["switched"] else None})
    pick = out[:1]
    return {"receiver_ids": [p["receiver_id"] for p in pick], "antenna": pick[0]["antenna"] if pick else None,
            "reasons": {p["receiver_id"]: {"reason": p["reason"], "at": p["at"], "basis": p["basis"]} for p in pick},
            "ranked": out, "excluded": excluded}
