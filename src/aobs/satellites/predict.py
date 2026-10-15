"""Built-in pass prediction (Skyfield), for installations without the original station software.

Orbital elements come from, in order of preference:
  1. <data>/tle/starlab.tle, written by refresh_tles() from CelesTrak when the user asks for it;
  2. the sample elements shipped with Starlab (satellites/data/sample.tle), clearly labelled with
     their epoch: good enough to explore the schedule, drifting by minutes after a few weeks.

Prediction needs no network: Skyfield's built-in timescale is used, not downloaded files.
All times are UTC. Passes are reported only when they start and end inside the window.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import re
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

SAMPLE_TLE = pathlib.Path(__file__).with_name("data") / "sample.tle"
CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=TLE"
STALE_DAYS = 14

# Satellites Starlab knows how to record. Frequencies are the downlinks the receivers tune to.
CATALOG: dict[int, dict[str, Any]] = {
    57166: {"name": "METEOR-M2 3", "downlink_hz": 137_900_000, "mode": "LRPT (QPSK, 72 kbit/s) weather images",
            "bandwidth_hz": 150_000, "pipeline": "meteor_m2-x_lrpt", "min_elevation_deg": 20},
    59051: {"name": "METEOR-M2 4", "downlink_hz": 137_900_000, "mode": "LRPT (QPSK, 72 kbit/s) weather images",
            "bandwidth_hz": 150_000, "pipeline": "meteor_m2-x_lrpt", "min_elevation_deg": 20},
    25544: {"name": "ISS", "downlink_hz": 145_800_000, "mode": "FM voice and SSTV events (145.800 MHz)",
            "bandwidth_hz": 25_000, "pipeline": None, "min_elevation_deg": 10},
}


@dataclass(frozen=True)
class Elements:
    norad: int
    name: str
    line1: str
    line2: str
    epoch: dt.datetime
    source: str


def _checksum_ok(line: str) -> bool:
    if len(line) < 69 or not line[68].isdigit():
        return False
    total = sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line[:68])
    return total % 10 == int(line[68])


def _epoch(line1: str) -> dt.datetime:
    yy, day = int(line1[18:20]), float(line1[20:32])
    year = 2000 + yy if yy < 57 else 1900 + yy
    return dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=day - 1)


def parse_tle(text: str, source: str) -> dict[int, Elements]:
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out: dict[int, Elements] = {}
    i = 0
    while i < len(lines):
        if lines[i].startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            name, l1, l2, i = f"NORAD {lines[i][2:7].strip()}", lines[i], lines[i + 1], i + 2
        elif i + 2 < len(lines) and lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
            name, l1, l2, i = lines[i].strip(), lines[i + 1], lines[i + 2], i + 3
        else:
            i += 1
            continue
        if _checksum_ok(l1) and _checksum_ok(l2) and l1[2:7] == l2[2:7]:
            norad = int(l1[2:7])
            out[norad] = Elements(norad, name, l1, l2, _epoch(l1), source)
    return out


def load_elements(data_root: pathlib.Path | None) -> dict[int, Elements]:
    """Sample elements, overridden per satellite by any newer downloaded ones."""
    out = parse_tle(SAMPLE_TLE.read_text(), "sample elements shipped with Starlab")
    fresh = (data_root / "tle" / "starlab.tle") if data_root else None
    if fresh is not None and fresh.is_file():
        for norad, e in parse_tle(fresh.read_text(), "downloaded from CelesTrak").items():
            if norad not in out or e.epoch >= out[norad].epoch:
                out[norad] = e
    return out


def refresh_tles(norads: list[int], data_root: pathlib.Path,
                 fetch: Callable[[str], str] | None = None) -> dict[str, Any]:
    """Download current elements for these satellites from CelesTrak. Only on the user's request."""
    def get(url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "Starlab Observatory"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("ascii", errors="replace")
    fetch = fetch or get
    got: dict[int, Elements] = {}
    failed: dict[int, str] = {}
    for n in norads:
        try:
            parsed = parse_tle(fetch(CELESTRAK.format(norad=int(n))), "downloaded from CelesTrak")
            if int(n) not in parsed:
                raise ValueError("no valid elements in the answer")
            got[int(n)] = parsed[int(n)]
        except Exception as exc:  # noqa: BLE001 - one satellite failing must not lose the others
            failed[int(n)] = str(exc)[:200]
    path = data_root / "tle" / "starlab.tle"
    if got:
        old = parse_tle(path.read_text(), "downloaded from CelesTrak") if path.is_file() else {}
        old.update(got)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(f"{e.name}\n{e.line1}\n{e.line2}\n" for e in old.values()))
        os.replace(tmp, path)
    return {"updated": sorted(got), "failed": failed,
            "epochs": {n: e.epoch.isoformat() for n, e in got.items()}}


def predict_passes(elements: dict[int, Elements], norads: list[int], lat: float, lon: float, elevation_m: float,
                   start: dt.datetime, hours: float, min_elevation_deg: float = 0.0) -> list[dict[str, Any]]:
    from skyfield.api import EarthSatellite, load, wgs84

    ts = load.timescale(builtin=True)
    observer = wgs84.latlon(lat, lon, elevation_m=elevation_m)
    t0, t1 = ts.from_datetime(start), ts.from_datetime(start + dt.timedelta(hours=hours))
    out = []
    for n in norads:
        e = elements.get(int(n))
        if e is None:
            continue
        sat = EarthSatellite(e.line1, e.line2, e.name, ts)
        times, events = sat.find_events(observer, t0, t1, altitude_degrees=0.0)
        rise = peak = None
        for t, ev in zip(times, events):
            if ev == 0:
                rise, peak = t, None
            elif ev == 1 and rise is not None:
                if peak is None or (sat - observer).at(t).altaz()[0].degrees > (sat - observer).at(peak).altaz()[0].degrees:
                    peak = t
            elif ev == 2 and rise is not None and peak is not None:
                alt, az_peak, _ = (sat - observer).at(peak).altaz()
                if alt.degrees >= min_elevation_deg:
                    aos, los = rise.utc_datetime(), t.utc_datetime()
                    out.append({"norad": e.norad, "name": CATALOG.get(e.norad, {}).get("name", e.name),
                                "aos": aos.isoformat(), "los": los.isoformat(),
                                "max_elevation_deg": round(alt.degrees, 1), "max_at": peak.utc_datetime().isoformat(),
                                "aos_azimuth_deg": round((sat - observer).at(rise).altaz()[1].degrees, 1),
                                "los_azimuth_deg": round((sat - observer).at(t).altaz()[1].degrees, 1),
                                "duration_s": int((los - aos).total_seconds()),
                                "elements_epoch": e.epoch.isoformat(), "elements_source": e.source})
                rise = peak = None
    return sorted(out, key=lambda p: p["aos"])


def element_status(elements: dict[int, Elements], norads: list[int], now: dt.datetime) -> list[dict[str, Any]]:
    out = []
    for n in norads:
        e = elements.get(int(n))
        if e is None:
            out.append({"norad": int(n), "available": False, "warning": "no orbital elements for this satellite"})
            continue
        age = (now - e.epoch).total_seconds() / 86400
        out.append({"norad": e.norad, "name": e.name, "available": True, "epoch": e.epoch.isoformat(),
                    "age_days": round(age, 1), "source": e.source,
                    "warning": (f"elements are {age:.0f} days old; pass times may be off by minutes. Refresh them "
                                "when online.") if age > STALE_DAYS else None})
    return out


_NORAD = re.compile(r"^\d{1,9}$")
