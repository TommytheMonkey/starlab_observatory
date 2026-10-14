"""Render a satellite pass into a Google Calendar event body (pure, deterministic)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any, Mapping

from ..timeutil import fmt_local, iso

STATUS_COLORS = {  # Google Calendar event colorId
    "SCHEDULED": "8", "READY": "10", "WARNING": "6", "RECORDING": "5",
    "RECORDED": "2", "DECODED": "9", "FAILED": "11", "MISSED": "11",
}
_EVENT_ID_OK = re.compile(r"^[a-v0-9]{5,1024}$")


def event_id(pass_id: str) -> str:
    """Deterministic Google event id (base32hex alphabet: a-v, 0-9). '57166-r17115' -> 'aobs57166r17115'."""
    eid = "aobs" + pass_id.lower().replace("-", "")
    if not _EVENT_ID_OK.match(eid):
        eid = "aobs" + hashlib.sha1(pass_id.encode()).hexdigest()  # hex is a subset of base32hex
    return eid


def _sec(value: dt.datetime) -> dt.datetime:
    """Whole seconds: predictions jitter by milliseconds between runs; the body must not."""
    return (value + dt.timedelta(microseconds=500_000)).replace(microsecond=0)


def _t(value: dt.datetime | None, tz: str) -> str:
    if value is None:
        return "not computed"
    value = _sec(value)
    return f"{fmt_local(value, tz, '%Y-%m-%d %H:%M:%S %Z')}  ({iso(value)})"


def render_event(p: Mapping[str, Any], tz: str, event_minutes: int = 10, links_base_url: str = "",
                 decoded: list[str] | None = None) -> dict[str, Any]:
    status = str(p["status"])
    start = _sec(p["capture_start"])
    end = start + dt.timedelta(minutes=event_minutes)
    cap_s = int((_sec(p["capture_end"]) - start).total_seconds())
    readiness = p.get("readiness") or {}
    outcome = p.get("outcome") or {}

    lines = [
        f"STATUS: {status}",
        f"Reason: {p.get('status_reason') or '-'}",
        "",
        f"Satellite: {p['satellite_name']}",
        f"NORAD ID: {p['norad_id']}",
        f"Revolution: {p.get('revolution')}",
        "",
        f"Predicted AOS (0°): {_t(p.get('aos'), tz)}",
        f"Predicted LOS (0°): {_t(p.get('los'), tz)}",
        f"Culmination: {_t(p['culmination'], tz)}",
        f"Maximum elevation: {p['max_elevation_deg']:.1f}°",
        f"Predicted quality: {p.get('predicted_quality')} (heuristic from max elevation)",
        f"Recorder threshold: {p['min_elevation_deg']:.0f}° (rise {fmt_local(_sec(p['threshold_rise']), tz, '%H:%M:%S')}, "
        f"set {fmt_local(_sec(p['threshold_set']), tz, '%H:%M:%S')})",
        "",
        f"Scheduled recording start: {_t(p['capture_start'], tz)}",
        f"Scheduled recording end:   {_t(p['capture_end'], tz)}",
        f"Full capture window: {cap_s // 60} min {cap_s % 60:02d} s"
        + (f"  (this {event_minutes}-minute appointment shows only the first {event_minutes} minutes)"
           if cap_s > event_minutes * 60 else ""),
    ]
    segs = p.get("capture_segments") or []
    if len(segs) > 1:
        lines.append(f"Planned rtl_sdr segments: {', '.join(str(s['seconds']) + ' s' for s in segs)}")
    lines += [
        "",
        f"Recording computer: {p['host']}",
        f"SDR receiver: {p['receiver']} (serial {p['receiver']})",
        f"Antenna: {p.get('antenna')}",
        f"Center frequency: {p['center_frequency_hz'] / 1e6:.3f} MHz (recording center, not the transmitter frequency)",
        f"Sample rate: {p['sample_rate'] / 1e6:.3f} MS/s",
        "",
        f"Recording readiness: {readiness.get('verdict', 'not yet checked')}",
    ]
    # Only stable facts go into the body (volatile numbers would cause an update every cycle).
    checks = readiness.get("checks", [])
    if checks:
        passed = [c["name"] for c in checks if c["status"] == "PASS"]
        lines.append(f"  Checks passed ({len(passed)}/{len(checks)}): {', '.join(passed) or 'none'}")
        for c in checks:
            if c["status"] in ("FAIL", "WARN"):
                lines.append(f"  [{c['status']}] {c['name']}")
            elif c["status"] == "NA":
                lines.append(f"  [N/A] {c['name']} (verified when this becomes the recorder's next pass)")
    lines.append(f"  Storage: {'OK' if any(c['name'] == 'storage' and c['status'] == 'PASS' for c in checks) else 'see status'}"
                 f" · Recording service: {'active' if any(c['name'] == 'recording_service_active' and c['status'] == 'PASS' for c in checks) else 'NOT verified active'}")
    if outcome:
        lines += ["", "OUTCOME:", f"  {outcome.get('status')}: {outcome.get('reason')}"]
        if outcome.get("actual_start"):
            lines.append(f"  Actual capture: {outcome['actual_start']} → {outcome['actual_end']}, {outcome.get('recorded_bytes', 0) / 1e9:.2f} GB")
        for s in outcome.get("segments", []):
            lines.append(f"  {p['host']}:{s['raw_path']}" + ("" if s.get("raw_exists", True) else " (removed by retention)"))
    for path in decoded or []:
        lines.append(f"  Decoded: {path}")
    if links_base_url:
        lines += ["", f"Details: {links_base_url.rstrip('/')}/satellites/passes/{p['id']}"]
    lines += ["", f"Pass id: {p['id']} · managed by Autonomous Observatory (do not edit; changes are overwritten)"]

    title_el = f"max {p['max_elevation_deg']:.0f}°"
    return {
        "id": event_id(str(p["id"])),
        "summary": f"🛰 {p['satellite_name']} · {status} · {title_el}",
        "description": "\n".join(lines),
        "location": f"{p['host']} · {p['receiver']} · Davidson, NC",
        "start": {"dateTime": iso(start)},
        "end": {"dateTime": iso(end)},
        "colorId": STATUS_COLORS.get(status, "8"),
        "transparency": "transparent",
        "reminders": {"useDefault": False, "overrides": []},
        "status": "confirmed",
        "extendedProperties": {"private": {"aobs_pass_id": str(p["id"]), "aobs_status": status, "aobs_schema": "1"}},
    }


def body_hash(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
