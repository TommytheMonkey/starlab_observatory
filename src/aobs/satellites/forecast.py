"""Rolling satellite forecast built on the recorder's own predictor."""

from __future__ import annotations

import datetime as dt
import math
import pathlib
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..legacy import Legacy, predict, recorder_horizon_hours, recorder_jobs, refresh_tle
from ..timeutil import UTC, utc_now


@dataclass(frozen=True)
class Segment:
    start: dt.datetime
    end: dt.datetime

    @property
    def seconds(self) -> int:
        return math.ceil((self.end - self.start).total_seconds())


@dataclass(frozen=True)
class PlannedPass:
    pass_id: str
    norad_id: int
    satellite_name: str
    job_id: str
    revolution: int
    host: str
    receiver: str
    antenna: str
    center_frequency_hz: int
    sample_rate: int
    gain_db: float
    aos: dt.datetime | None
    los: dt.datetime | None
    threshold_rise: dt.datetime
    threshold_set: dt.datetime
    min_elevation_deg: float
    culmination: dt.datetime
    max_elevation_deg: float
    record_start: dt.datetime
    record_end: dt.datetime
    capture_start: dt.datetime
    capture_end: dt.datetime
    capture_segments: list[Segment]
    merged_with: list[str]
    predicted_quality: str
    tle_epoch: dt.datetime
    predicted_at: dt.datetime

    @property
    def record_seconds(self) -> int:
        return math.ceil((self.record_end - self.record_start).total_seconds())

    @property
    def capture_seconds(self) -> int:
        return math.ceil((self.capture_end - self.capture_start).total_seconds())

    @property
    def expected_bytes(self) -> int:
        # cu8: 2 bytes per complex sample
        return self.sample_rate * 2 * self.capture_seconds


@dataclass
class Forecast:
    generated_at: dt.datetime
    horizon_hours: float
    recorder_horizon_hours: float
    receiver: str
    tle_path: pathlib.Path
    tle_epochs: dict[int, dt.datetime]
    passes: list[PlannedPass] = field(default_factory=list)


def quality_label(max_el: float) -> str:
    """Heuristic from peak elevation only (no link budget). Labelled as such in output."""
    if max_el >= 60:
        return "excellent"
    if max_el >= 40:
        return "good"
    if max_el >= 25:
        return "fair"
    return "poor"


def plan_segments(start: dt.datetime, end: dt.datetime, segment_seconds: int, gap_seconds: float = 2.0) -> list[Segment]:
    """Reproduce record.py run_passes: capture min(segment, remaining) repeatedly until the
    merged window ends. gap_seconds approximates rtl_sdr restart overhead between segments."""
    segments = []
    cursor = start
    while cursor < end:
        seconds = min(segment_seconds, math.ceil((end - cursor).total_seconds()))
        seg_end = cursor + dt.timedelta(seconds=seconds)
        segments.append(Segment(cursor, seg_end))
        cursor = seg_end + dt.timedelta(seconds=gap_seconds)
    return segments


def revolution_at(satellite: Any, when: dt.datetime) -> int:
    """Revolution number (ascending-node count) at `when`, from the TLE's rev number."""
    model = satellite.model
    epoch = satellite.epoch.utc_datetime().replace(tzinfo=UTC)
    minutes = (when - epoch).total_seconds() / 60.0
    phase0 = ((model.argpo + model.mo) % (2 * math.pi)) / (2 * math.pi)
    return int(model.revnum + math.floor(phase0 + minutes * model.no_kozai / (2 * math.pi)))


def horizon_crossings(satellite: Any, location: Any, ts: Any, culmination: dt.datetime) -> tuple[dt.datetime | None, dt.datetime | None]:
    t0 = ts.from_datetime(culmination - dt.timedelta(minutes=20))
    t1 = ts.from_datetime(culmination + dt.timedelta(minutes=20))
    times, events = satellite.find_events(location, t0, t1, altitude_degrees=0.0)
    aos = los = None
    for t, e in zip(times, events):
        value = t.utc_datetime().replace(tzinfo=UTC)
        if int(e) == 0 and value <= culmination:
            aos = value
        elif int(e) == 2 and value >= culmination and los is None:
            los = value
    return aos, los


def build_forecast(config: Config, legacy: Legacy, now: dt.datetime | None = None) -> Forecast:
    from skyfield.api import load

    fc = config.section("forecast")
    receiver = str(fc["receiver"])
    horizon = float(fc.get("horizon_hours", 72))
    jobs = recorder_jobs(legacy, receiver)
    rec_root = pathlib.Path(legacy.recorder["storage_root"])
    seeds = [rec_root / "pass-cache" / receiver / "observatory.tle",
             pathlib.Path(legacy.station["storage"]["root"]) / "tle" / "observatory.tle"]
    tle_path = refresh_tle(legacy, pathlib.Path(config.raw["paths"]["tle_cache"]), jobs, seeds)

    windows = predict(legacy, tle_path, jobs, horizon)
    merged = legacy.pass_schedule.merge_windows(windows)

    sky = legacy.scheduler.load_skyfield_satellites(tle_path, jobs)
    location = legacy.scheduler.build_location(legacy.station)
    ts = load.timescale()
    profile = legacy.recorder["receivers"][receiver]
    antenna = str(profile.get("antenna", ""))
    for ant in config.raw.get("antennas", {}).values():
        if ant.get("hardwired_receiver") == receiver:
            antenna = f"{ant['description']} (recorder label: {antenna})"
    segment_seconds = int(legacy.recorder["segment_seconds"])
    generated = now or utc_now()
    tle_epochs = {n: s.epoch.utc_datetime().replace(tzinfo=UTC) for n, s in sky.items()}

    passes: list[PlannedPass] = []
    for w in windows:
        group = next(g for g in merged if g["start"] <= w.record_start <= g["end"])
        satellite = sky[w.job.norad_id]
        culm = w.culminate or (w.rise + (w.set - w.rise) / 2)
        rev = revolution_at(satellite, culm)
        aos, los = horizon_crossings(satellite, location, ts, culm)
        others = [s["name"] for s in group["satellites"] if not (s["norad_id"] == w.job.norad_id and s["rise"] == w.rise.isoformat())]
        passes.append(PlannedPass(
            pass_id=f"{w.job.norad_id}-r{rev}",
            norad_id=w.job.norad_id,
            satellite_name=w.job.name,
            job_id=w.job.id,
            revolution=rev,
            host=config.host,
            receiver=receiver,
            antenna=antenna,
            center_frequency_hz=int(profile["frequency_hz"]),
            sample_rate=int(profile["sample_rate"]),
            gain_db=float(profile.get("gain_db", 0)),
            aos=aos,
            los=los,
            threshold_rise=w.rise,
            threshold_set=w.set,
            min_elevation_deg=w.job.min_elevation_degrees,
            culmination=culm,
            max_elevation_deg=float(w.max_elevation_degrees or 0.0),
            record_start=w.record_start,
            record_end=w.record_end,
            capture_start=group["start"],
            capture_end=group["end"],
            capture_segments=plan_segments(group["start"], group["end"], segment_seconds),
            merged_with=others,
            predicted_quality=quality_label(float(w.max_elevation_degrees or 0.0)),
            tle_epoch=tle_epochs[w.job.norad_id],
            predicted_at=generated,
        ))
    return Forecast(
        generated_at=generated,
        horizon_hours=horizon,
        recorder_horizon_hours=recorder_horizon_hours(legacy),
        receiver=receiver,
        tle_path=tle_path,
        tle_epochs=tle_epochs,
        passes=sorted(passes, key=lambda p: p.record_start),
    )
