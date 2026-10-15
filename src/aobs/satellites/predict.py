"""Built-in pass prediction (Skyfield), for installations without the original station software.

Orbital elements come from, newest epoch first:
  1. the local cache under <data>/tle/, written by refresh_tles() from the configured source
     (CelesTrak OMM or TLE, or a custom URL): starlab.tle holds TLEs, starlab.omm.json OMM records,
     and fetched.json when and from where each satellite's elements were downloaded;
  2. the sample elements shipped with Starlab (satellites/data/sample.tle), clearly labelled with
     their epoch: good enough to explore the schedule, drifting by minutes after a few weeks.

OMM (CCSDS Orbit Mean-Elements Message, JSON) is preferred where the source offers it: it carries
the same mean elements as a TLE without the 5-digit catalogue-number limit.

Prediction needs no network: Skyfield's built-in timescale is used, not downloaded files.
All times are UTC. Passes are reported only when they start and end inside the window.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

SAMPLE_TLE = pathlib.Path(__file__).with_name("data") / "sample.tle"
CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=TLE"
CELESTRAK_OMM = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=JSON"
STALE_DAYS = 14
SAMPLE_SOURCE = "sample elements shipped with Starlab"
# settings "satellites".elements_source -> [(url template, format)], tried in order per satellite
SOURCES: dict[str, list[tuple[str, str]]] = {
    "celestrak_omm": [(CELESTRAK_OMM, "omm"), (CELESTRAK, "tle")],
    "celestrak_tle": [(CELESTRAK, "tle")],
}

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
    line1: str                         # empty for OMM records
    line2: str
    epoch: dt.datetime
    source: str
    format: str = "tle"                # tle | omm
    omm: dict[str, Any] | None = None
    fetched_at: dt.datetime | None = None   # when Starlab downloaded them (None: shipped sample)

    def satellite(self, ts: Any) -> Any:
        from skyfield.api import EarthSatellite
        if self.omm is not None:
            return EarthSatellite.from_omm(ts, self.omm)
        return EarthSatellite(self.line1, self.line2, self.name, ts)


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


_OMM_NEEDED = ("NORAD_CAT_ID", "EPOCH", "MEAN_MOTION", "ECCENTRICITY", "INCLINATION", "RA_OF_ASC_NODE",
               "ARG_OF_PERICENTER", "MEAN_ANOMALY", "BSTAR")


def _omm_epoch(value: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(str(value).replace("Z", ""))
    return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t.astimezone(dt.timezone.utc)


def parse_omm(text: str, source: str) -> dict[int, Elements]:
    """OMM records as JSON (a list of objects, as CelesTrak's FORMAT=JSON returns). Invalid records are skipped."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    out: dict[int, Elements] = {}
    for rec in data if isinstance(data, list) else [data]:
        if not isinstance(rec, dict) or any(k not in rec for k in _OMM_NEEDED):
            continue
        try:
            norad = int(rec["NORAD_CAT_ID"])
            epoch = _omm_epoch(rec["EPOCH"])
            float(rec["MEAN_MOTION"]), float(rec["ECCENTRICITY"])
        except (TypeError, ValueError):
            continue
        out[norad] = Elements(norad, str(rec.get("OBJECT_NAME") or f"NORAD {norad}").strip(), "", "", epoch, source,
                              "omm", dict(rec))
    return out


def tle_to_omm(e: Elements) -> dict[str, Any]:
    """The OMM fields of a TLE (used for tests and to show both formats the same way)."""
    l1, l2 = e.line1, e.line2

    def expo(field: str) -> float:          # TLE "assumed decimal point" notation, e.g. " 13316-4"
        f = field.strip()
        if not f:
            return 0.0
        sign = -1.0 if f[0] == "-" else 1.0
        f = f.lstrip("+-")
        mant, ex = f[:-2], f[-2:]
        return sign * float("0." + mant) * 10 ** int(ex)
    return {"OBJECT_NAME": e.name, "NORAD_CAT_ID": e.norad, "EPOCH": e.epoch.strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "MEAN_MOTION": float(l2[52:63]), "ECCENTRICITY": float("0." + l2[26:33].strip()),
            "INCLINATION": float(l2[8:16]), "RA_OF_ASC_NODE": float(l2[17:25]), "ARG_OF_PERICENTER": float(l2[34:42]),
            "MEAN_ANOMALY": float(l2[43:51]), "EPHEMERIS_TYPE": 0, "CLASSIFICATION_TYPE": l1[7] or "U",
            "OBJECT_ID": l1[9:17].strip(), "ELEMENT_SET_NO": int(l1[64:68] or 0), "REV_AT_EPOCH": int(l2[63:68] or 0),
            "BSTAR": expo(l1[53:61]), "MEAN_MOTION_DOT": float(l1[33:43]), "MEAN_MOTION_DDOT": expo(l1[44:52])}


def _cache_dir(data_root: pathlib.Path) -> pathlib.Path:
    return data_root / "tle"


def fetch_log(data_root: pathlib.Path | None) -> dict[int, dict[str, Any]]:
    """When and from where each satellite's elements were last downloaded ({norad: {fetched_at, source, format}})."""
    f = _cache_dir(data_root) / "fetched.json" if data_root else None
    if f is None or not f.is_file():
        return {}
    try:
        return {int(k): v for k, v in json.loads(f.read_text()).items()}
    except (OSError, ValueError):
        return {}


def load_elements(data_root: pathlib.Path | None) -> dict[int, Elements]:
    """Sample elements, overridden per satellite by any newer downloaded ones (TLE or OMM)."""
    out = parse_tle(SAMPLE_TLE.read_text(), SAMPLE_SOURCE)
    if data_root is None:
        return out
    log = fetch_log(data_root)
    found: list[Elements] = []
    tle = _cache_dir(data_root) / "starlab.tle"
    if tle.is_file():
        found += parse_tle(tle.read_text(), "downloaded").values()
    omm = _cache_dir(data_root) / "starlab.omm.json"
    if omm.is_file():
        found += parse_omm(omm.read_text(), "downloaded").values()
    for e in found:
        meta = log.get(e.norad) or {}
        when = meta.get("fetched_at")
        src = meta.get("source") if meta.get("format", e.format) == e.format else None
        e = Elements(e.norad, e.name, e.line1, e.line2, e.epoch, f"downloaded from {src or 'CelesTrak'}", e.format,
                     e.omm, dt.datetime.fromisoformat(when) if when and meta.get("format", e.format) == e.format else None)
        cur = out.get(e.norad)
        if cur is None or e.epoch >= cur.epoch:          # a download beats the sample at the same epoch
            out[e.norad] = e
    return out


def source_list(settings: dict[str, Any] | None) -> list[tuple[str, str]]:
    """The (url template, format) list for the satellites settings document."""
    st = settings or {}
    kind = st.get("elements_source") or "celestrak_omm"
    if kind == "custom":
        url = str(st.get("custom_url") or "")
        if "{norad}" not in url:
            raise ValueError("the custom orbital-element address must contain {norad}")
        return [(url, st.get("custom_format") or "tle")]
    return SOURCES.get(kind, SOURCES["celestrak_omm"])


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def refresh_tles(norads: list[int], data_root: pathlib.Path, fetch: Callable[[str], str] | None = None,
                 sources: list[tuple[str, str]] | None = None, now: dt.datetime | None = None) -> dict[str, Any]:
    """Download current elements for these satellites and cache them. Each source is tried in order until one
    answers with valid elements for that satellite; a failure keeps the cached elements."""
    def get(url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "Starlab Observatory"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")
    fetch = fetch or get
    sources = sources or [(CELESTRAK, "tle")]
    now = now or dt.datetime.now(dt.timezone.utc)
    got: dict[int, Elements] = {}
    failed: dict[int, str] = {}
    for n in norads:
        problems = []
        for url, fmt in sources:
            host = urllib.parse.urlsplit(url).netloc or url
            try:
                text = fetch(url.format(norad=int(n)))
                parsed = (parse_omm if fmt == "omm" else parse_tle)(text, f"downloaded from {host}")
                if int(n) not in parsed:
                    raise ValueError("no valid elements in the answer")
                got[int(n)] = parsed[int(n)]
                break
            except Exception as exc:  # noqa: BLE001 - one satellite or source failing must not lose the others
                problems.append(f"{host} ({fmt.upper()}): {str(exc)[:160]}")
        else:
            failed[int(n)] = "; ".join(problems)
    cache = _cache_dir(data_root)
    if got:
        tles = parse_tle((cache / "starlab.tle").read_text(), "x") if (cache / "starlab.tle").is_file() else {}
        omms = parse_omm((cache / "starlab.omm.json").read_text(), "x") if (cache / "starlab.omm.json").is_file() else {}
        log = fetch_log(data_root)
        for n, e in got.items():
            (omms if e.format == "omm" else tles)[n] = e
            (tles if e.format == "omm" else omms).pop(n, None)       # one current record per satellite
            log[n] = {"fetched_at": now.isoformat(), "source": e.source.removeprefix("downloaded from "),
                      "format": e.format, "epoch": e.epoch.isoformat()}
        _write(cache / "starlab.tle", "".join(f"{e.name}\n{e.line1}\n{e.line2}\n" for e in tles.values()))
        _write(cache / "starlab.omm.json", json.dumps([e.omm for e in omms.values()], indent=1))
        _write(cache / "fetched.json", json.dumps({str(k): v for k, v in sorted(log.items())}, indent=1))
    return {"updated": sorted(got), "failed": failed, "at": now.isoformat(),
            "epochs": {n: e.epoch.isoformat() for n, e in got.items()},
            "formats": {n: e.format for n, e in got.items()}}


def last_refresh(data_root: pathlib.Path | None) -> dt.datetime | None:
    times = [dt.datetime.fromisoformat(v["fetched_at"]) for v in fetch_log(data_root).values() if v.get("fetched_at")]
    return max(times) if times else None


def predict_passes(elements: dict[int, Elements], norads: list[int], lat: float, lon: float, elevation_m: float,
                   start: dt.datetime, hours: float, min_elevation_deg: float = 0.0) -> list[dict[str, Any]]:
    from skyfield.api import load, wgs84

    ts = load.timescale(builtin=True)
    observer = wgs84.latlon(lat, lon, elevation_m=elevation_m)
    t0, t1 = ts.from_datetime(start), ts.from_datetime(start + dt.timedelta(hours=hours))
    out = []
    for n in norads:
        e = elements.get(int(n))
        if e is None:
            continue
        sat = e.satellite(ts)
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
                                "aos_ts": aos.timestamp(), "elements_epoch": e.epoch.isoformat(),
                                "elements_source": e.source, "elements_format": e.format})
                rise = peak = None
    return sorted(out, key=lambda p: p["aos"])


def element_status(elements: dict[int, Elements], norads: list[int], now: dt.datetime,
                   stale_days: float = STALE_DAYS) -> list[dict[str, Any]]:
    out = []
    for n in norads:
        e = elements.get(int(n))
        if e is None:
            out.append({"norad": int(n), "available": False, "stale": True,
                        "warning": "no orbital elements for this satellite: its passes cannot be predicted"})
            continue
        age = (now - e.epoch).total_seconds() / 86400
        stale = age > stale_days
        out.append({"norad": e.norad, "name": e.name, "available": True, "epoch": e.epoch.isoformat(),
                    "age_days": round(age, 1), "source": e.source, "format": e.format, "stale": stale,
                    "fetched_at": e.fetched_at.isoformat() if e.fetched_at else None,
                    "warning": (f"elements are {age:.0f} days old (limit {stale_days:g}); pass times may be off by "
                                "minutes. Refresh them when online.") if stale else None})
    return out


_NORAD = re.compile(r"^\d{1,9}$")
