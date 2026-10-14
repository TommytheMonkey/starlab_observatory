"""Satellite operations sync cycle: forecast -> database -> readiness -> outcomes."""

from __future__ import annotations

import datetime as dt
import logging
import pathlib
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, and_, select

from ..config import Config
from ..db.schema import decoded_artifacts, pass_status_history, readiness_checks, recordings, satellite_passes
from ..legacy import Legacy
from ..logjson import log_event
from ..timeutil import utc_now
from . import DECODED, FINAL_STATUSES, RECORDED, WARNING
from .forecast import Forecast, PlannedPass, build_forecast
from .outcomes import Outcome, reconcile
from .readiness import Readiness, SystemProbe, evaluate, gather

LOG = logging.getLogger("aobs.satops")
MATCH_WINDOW = dt.timedelta(minutes=15)
# Re-predictions jitter by tens of milliseconds. Geometry is only rewritten when some time
# moves by more than this, so the stored pass (and its calendar event) stays stable.
GEOMETRY_TOLERANCE = dt.timedelta(seconds=5)
TIME_FIELDS = ("aos", "los", "threshold_rise", "threshold_set", "culmination",
               "record_start", "record_end", "capture_start", "capture_end")
GEOMETRY_FIELDS = TIME_FIELDS + ("max_elevation_deg", "capture_segments")


def _geometry_moved(row: Any, values: dict[str, Any]) -> bool:
    for name in TIME_FIELDS:
        old, new = row[name], values[name]
        if (old is None) != (new is None):
            return True
        if old is not None and abs(old - new) > GEOMETRY_TOLERANCE:
            return True
    return abs(float(row["max_elevation_deg"]) - float(values["max_elevation_deg"])) > 0.5


@dataclass
class CycleResult:
    forecast: Forecast
    probe: SystemProbe
    readiness: dict[str, Readiness]
    transitions: list[tuple[str, str | None, str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def _pass_values(p: PlannedPass) -> dict[str, Any]:
    return {
        "norad_id": p.norad_id, "satellite_name": p.satellite_name, "job_id": p.job_id, "revolution": p.revolution,
        "host": p.host, "receiver": p.receiver, "antenna": p.antenna, "center_frequency_hz": p.center_frequency_hz,
        "sample_rate": p.sample_rate, "gain_db": p.gain_db, "aos": p.aos, "los": p.los,
        "threshold_rise": p.threshold_rise, "threshold_set": p.threshold_set, "min_elevation_deg": p.min_elevation_deg,
        "culmination": p.culmination, "max_elevation_deg": p.max_elevation_deg,
        "record_start": p.record_start, "record_end": p.record_end,
        "capture_start": p.capture_start, "capture_end": p.capture_end,
        "capture_segments": [{"start": s.start.isoformat(), "end": s.end.isoformat(), "seconds": s.seconds} for s in p.capture_segments],
        "predicted_quality": p.predicted_quality, "tle_epoch": p.tle_epoch, "predicted_at": p.predicted_at,
        "in_forecast": True, "removed_at": None,
    }


def _set_status(conn: Any, pass_id: str, old: str | None, new: str, reason: str, now: dt.datetime,
                result: CycleResult | None) -> None:
    conn.execute(satellite_passes.update().where(satellite_passes.c.id == pass_id)
                 .values(status=new, status_reason=reason, updated_at=now))
    if old != new:
        conn.execute(pass_status_history.insert().values(pass_id=pass_id, at=now, old_status=old, new_status=new, reason=reason))
        log_event(LOG, "pass status change", pass_id=pass_id, old=old, new=new, reason=reason)
        if result is not None:
            result.transitions.append((pass_id, old, new, reason))


def upsert_forecast(engine: Engine, forecast: Forecast, now: dt.datetime, result: CycleResult | None = None) -> dict[str, str]:
    """Insert/update predicted passes. Returns planned pass_id -> stored pass_id.

    Identity: an existing row of the same satellite whose culmination is within 15 min
    is the same pass (orbital period ~101 min), whatever revolution number we compute now.
    """
    mapping: dict[str, str] = {}
    with engine.begin() as conn:
        for p in forecast.passes:
            row = conn.execute(select(satellite_passes).where(and_(
                satellite_passes.c.norad_id == p.norad_id,
                satellite_passes.c.culmination >= p.culmination - MATCH_WINDOW,
                satellite_passes.c.culmination <= p.culmination + MATCH_WINDOW,
            ))).mappings().first()
            values = _pass_values(p)
            if row is None:
                conn.execute(satellite_passes.insert().values(id=p.pass_id, status="SCHEDULED",
                                                              status_reason="Newly predicted", created_at=now, updated_at=now, **values))
                conn.execute(pass_status_history.insert().values(pass_id=p.pass_id, at=now, old_status=None,
                                                                 new_status="SCHEDULED", reason="Newly predicted"))
                mapping[p.pass_id] = p.pass_id
            else:
                if row["status"] not in FINAL_STATUSES:
                    if not _geometry_moved(row, values):
                        values = {k: v for k, v in values.items() if k not in GEOMETRY_FIELDS}
                    conn.execute(satellite_passes.update().where(satellite_passes.c.id == row["id"]).values(updated_at=now, **values))
                mapping[p.pass_id] = row["id"]

        # Passes still in the future that vanished from the forecast.
        live = set(mapping.values())
        horizon_end = forecast.generated_at + dt.timedelta(hours=forecast.horizon_hours)
        stale = conn.execute(select(satellite_passes.c.id, satellite_passes.c.status).where(and_(
            satellite_passes.c.receiver == forecast.receiver,
            satellite_passes.c.capture_end > now,
            satellite_passes.c.record_start < horizon_end,
            satellite_passes.c.in_forecast.is_(True),
        ))).all()
        for row in stale:
            if row.id in live or row.status in FINAL_STATUSES:
                continue
            conn.execute(satellite_passes.update().where(satellite_passes.c.id == row.id)
                         .values(in_forecast=False, removed_at=now, updated_at=now))
            _set_status(conn, row.id, row.status, WARNING,
                        "Pass no longer appears in the forecast; the recorder will not record it", now, result)
            if result is not None:
                result.removed.append(row.id)
    return mapping


def _record_segments(conn: Any, pass_id: str, outcome: Outcome, now: dt.datetime) -> None:
    for s in outcome.segments:
        values = dict(kind="satellite", pass_id=pass_id, receiver=s.meta.get("receiver"), path=str(s.raw_path),
                      metadata_path=str(s.metadata_path), format=s.meta.get("baseband_format"),
                      center_frequency_hz=s.meta.get("frequency_hz"), sample_rate=s.meta.get("sample_rate"),
                      started_at=s.started_at, finished_at=s.finished_at, bytes=s.raw_bytes,
                      expected_bytes=s.expected_bytes, status=s.status, owner="legacy-recorder",
                      deleted_at=None if s.raw_exists else now, updated_at=now)
        if conn.execute(select(recordings.c.id).where(recordings.c.id == s.base)).first():
            conn.execute(recordings.update().where(recordings.c.id == s.base).values(**values))
        else:
            conn.execute(recordings.insert().values(id=s.base, created_at=now, **values))


def run_cycle(config: Config, legacy: Legacy, engine: Engine, now: dt.datetime | None = None,
              forecast: Forecast | None = None, probe: SystemProbe | None = None) -> CycleResult:
    now = now or utc_now()
    forecast = forecast or build_forecast(config, legacy, now)
    probe = probe or gather(config, legacy)
    readiness = evaluate(config, legacy, forecast, probe, now)
    result = CycleResult(forecast, probe, readiness)
    mapping = upsert_forecast(engine, forecast, now, result)

    oc = config.section("outcome")
    grace = dt.timedelta(minutes=float(oc.get("grace_minutes", 5)))
    min_cov = float(oc.get("min_coverage", 0.9))
    retention = dt.timedelta(hours=float(legacy.recorder.get("retention_hours", 6)))
    rec_dir = pathlib.Path(legacy.recorder["storage_root"]) / forecast.receiver
    lease = probe.leases.get(forecast.receiver) or {}
    lease_owner = lease.get("owner") if lease.get("owner") == f"observatory:passes:{forecast.receiver}" else None
    parts = [p for p in rec_dir.glob("*.cu8.part") if (now.timestamp() - p.stat().st_mtime) < 30]

    with engine.begin() as conn:
        for planned_id, ready in readiness.items():
            stored = mapping.get(planned_id, planned_id)
            conn.execute(readiness_checks.insert().values(pass_id=stored, at=now, verdict=ready.verdict, checks=ready.to_json()["checks"]))
            conn.execute(satellite_passes.update().where(satellite_passes.c.id == stored)
                         .values(readiness=ready.to_json(), readiness_checked_at=now))

        rows = conn.execute(select(satellite_passes).where(and_(
            satellite_passes.c.receiver == forecast.receiver,
            satellite_passes.c.capture_start <= now + dt.timedelta(hours=forecast.horizon_hours),
        ))).mappings().all()
        readiness_by_stored = {mapping.get(pid, pid): r for pid, r in readiness.items()}
        for row in rows:
            status = row["status"]
            if status == DECODED:
                continue
            if status == RECORDED:
                # Promote to DECODED only on a verified decode artifact (written by the decode pipeline).
                if conn.execute(select(decoded_artifacts.c.id).where(and_(
                        decoded_artifacts.c.pass_id == row["id"], decoded_artifacts.c.verified.is_(True)))).first():
                    _set_status(conn, row["id"], status, DECODED, "Verified decode artifact recorded", now, result)
                continue
            if status in FINAL_STATUSES:
                continue
            if now < row["capture_start"]:
                if not row["in_forecast"]:
                    continue  # already WARNING with removal reason
                ready = readiness_by_stored.get(row["id"])
                if ready is not None:
                    _set_status(conn, row["id"], status, ready.verdict, ready.reason, now, result)
                continue
            outcome = reconcile(row["norad_id"], row["threshold_rise"], row["capture_start"], row["capture_end"],
                                rec_dir, now, grace, min_cov, lease_owner, parts, retention)
            if outcome is None:
                continue
            values: dict[str, Any] = {"outcome": outcome.to_json()}
            if outcome.actual_start:
                values.update(actual_start=outcome.actual_start, actual_end=outcome.actual_end, recorded_bytes=outcome.recorded_bytes)
            conn.execute(satellite_passes.update().where(satellite_passes.c.id == row["id"]).values(**values))
            _record_segments(conn, row["id"], outcome, now)
            _set_status(conn, row["id"], status, outcome.status, outcome.reason, now, result)
    return result
