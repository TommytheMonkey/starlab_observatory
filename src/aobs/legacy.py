"""Read-only adapter over the existing Observatory recorder and scheduler.

The satellite recorder (/opt/observatory-recorder/record.py) decides what is
recorded via PassSchedule, which calls observatory_scheduler.predict_passes()
with /etc/observatory-sdr/config.json. We import that exact code so our
forecast is the recorder's forecast; only look_ahead_hours is overridden, on a
deep copy of the configuration.
"""

from __future__ import annotations

import copy
import functools
import importlib
import json
import pathlib
import re
import subprocess
import sys
import datetime as dt
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from .config import Config
from .timeutil import parse_utc


@dataclass(frozen=True)
class Legacy:
    scheduler: ModuleType
    pass_schedule: ModuleType
    coordinator: ModuleType
    station: dict[str, Any]
    recorder: dict[str, Any]


def _import_from(directory: str, name: str) -> ModuleType:
    if directory not in sys.path:
        sys.path.insert(0, directory)
    return importlib.import_module(name)


def load_legacy(config: Config) -> Legacy:
    leg = config.section("legacy")
    with open(leg["station_config"], encoding="utf-8") as fh:
        station = json.load(fh)
    with open(leg["recorder_config"], encoding="utf-8") as fh:
        recorder = json.load(fh)
    return Legacy(
        scheduler=_import_from(leg["scheduler_dir"], "observatory_scheduler"),
        pass_schedule=_import_from(leg["recorder_dir"], "pass_schedule"),
        coordinator=_import_from(leg["scheduler_dir"], "sdr_coordinator"),
        station=station,
        recorder=recorder,
    )


def recorder_jobs(legacy: Legacy, receiver: str) -> list[Any]:
    """Mirror PassSchedule.__init__ job selection exactly.

    Note: PassSchedule ignores the station job's `enabled` flag; it selects by
    receiver + recorder pass_targets. We reproduce that so the forecast matches
    what the recorder will actually do.
    """
    wanted = {int(n) for n in legacy.recorder["receivers"][receiver]["pass_targets"]}
    jobs = []
    for raw in legacy.station.get("jobs", []):
        if raw.get("receiver") != receiver or int(raw["norad_id"]) not in wanted:
            continue
        jobs.append(legacy.scheduler.Job(
            id=str(raw["id"]), name=str(raw["name"]), kind=str(raw["kind"]),
            norad_id=int(raw["norad_id"]), receiver=receiver,
            capability=str(raw["capability"]), enabled=True,
            min_elevation_degrees=float(raw.get("min_elevation_degrees", 20)),
            raw=dict(raw),
        ))
    if {j.norad_id for j in jobs} != wanted:
        raise RuntimeError(f"Recorder targets {sorted(wanted)} not all present in station jobs for {receiver}")
    return jobs


def predict(legacy: Legacy, tle_path: pathlib.Path, jobs: list[Any], horizon_hours: float) -> list[Any]:
    cfg = copy.deepcopy(legacy.station)
    cfg["passes"]["look_ahead_hours"] = float(horizon_hours)
    return legacy.scheduler.predict_passes(cfg, tle_path, jobs)


def recorder_horizon_hours(legacy: Legacy) -> float:
    return float(legacy.station["passes"].get("look_ahead_hours", 12))


def refresh_tle(legacy: Legacy, cache_dir: pathlib.Path, jobs: list[Any], seeds: list[pathlib.Path]) -> pathlib.Path:
    """Use the legacy TLE fetcher (same URLs, refresh and staleness policy) into our own cache.

    The cache is seeded (mtime preserved) from the recorder's pass cache so we start
    from the identical orbital elements the recorder is using.
    """
    import shutil

    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / "observatory.tle"
    if not target.exists():
        for seed in seeds:
            if seed.is_file():
                shutil.copy2(seed, target)
                break
    return legacy.scheduler.refresh_tle(legacy.station, {"tle": cache_dir}, jobs)


# ----------------------------------------------------------- journal parsing
_WAIT = re.compile(r"(?P<rx>OBS-R\d+): Waiting for (?P<names>.+?); recording starts (?P<start>\S+)")
_REC = re.compile(r"Recording (?P<rx>OBS-R\d+) at ")
_TS = re.compile(r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ ")


@dataclass(frozen=True)
class RecorderIntent:
    receiver: str
    satellites: str
    record_start: dt.datetime
    logged_at_local: str


@functools.lru_cache(maxsize=4)
def _journal(unit: str, lines: int, _bucket: int) -> str:
    result = subprocess.run(
        ["journalctl", "-u", unit, "-n", str(lines), "-o", "cat", "--no-pager"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    return result.stdout


def journal_text(unit: str, lines: int = 400) -> str:
    import time
    return _journal(unit, lines, int(time.monotonic() // 30))


def latest_recorder_intent(unit: str, text: str | None = None) -> RecorderIntent | None:
    """The recorder logs 'Waiting for X; recording starts T' whenever its next window changes
    (and every 5 minutes after a forecast refresh). The last such line is its current plan."""
    text = journal_text(unit) if text is None else text
    intent = None
    for line in text.splitlines():
        m = _WAIT.search(line)
        if m:
            ts = _TS.match(line)
            intent = RecorderIntent(m["rx"], m["names"], parse_utc(m["start"]), ts["ts"] if ts else "")
        elif _REC.search(line):
            intent = None  # a capture started after the last intent; plan consumed
    return intent
