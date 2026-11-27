"""Position sanity checks, flight statistics, and the CSV / GeoJSON / KML exports. Pure functions over rows."""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from typing import Any
from xml.sax.saxutils import escape

from . import geo

UTC = dt.timezone.utc
GPS_EPOCH_MIN = dt.datetime(2020, 1, 1, tzinfo=UTC)


def _t(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def validate(c: dict[str, Any], rx_utc: dt.datetime, last_ok: dict[str, Any] | None,
             last_rejected: dict[str, Any] | None, max_speed_mps: float = 150.0, min_alt_m: float = -500.0,
             max_alt_m: float = 60_000.0) -> tuple[bool, str | None]:
    """(valid, reason). A Position without coordinates, at 0,0, out of range, deliberately withheld (precision 0),
    with an impossible altitude or GPS time, or an impossible jump from the last good fix is rejected, never drawn.

    A jump is forgiven when the previous fix was also rejected as a jump and this one agrees with it: then it was the
    reference fix that was wrong (a cold-start fix far away), and the track restarts here."""
    lat, lon = c.get("lat"), c.get("lon")
    if lat is None or lon is None:
        return False, "no coordinates"
    if c.get("precision_bits") == 0:
        return False, "position withheld (precision 0)"
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:
        return False, "0,0 (no fix)"
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False, "coordinates out of range"
    alt = c.get("alt_m")
    if alt is not None and not min_alt_m <= alt <= max_alt_m:
        return False, f"altitude {alt} m out of range"
    gps = c.get("gps_time")
    if gps:
        g = dt.datetime.fromtimestamp(gps, UTC)
        if g < GPS_EPOCH_MIN or g > rx_utc + dt.timedelta(hours=1):
            return False, f"GPS time {g.isoformat()} is impossible"
    if last_ok:
        dt_s = (rx_utc - _t(last_ok["rx_utc"])).total_seconds()
        if c.get("gps_time") and last_ok.get("gps_utc"):
            dt_s = (dt.datetime.fromtimestamp(c["gps_time"], UTC) - _t(last_ok["gps_utc"])).total_seconds()
        d = geo.distance_m(last_ok["lat"], last_ok["lon"], lat, lon)
        dz = abs((alt or 0) - (last_ok.get("alt_m") or 0))
        if d > 200 or dz > 200:                    # GPS noise at rest is far below this
            speed = max(d, dz) / max(dt_s, 1.0)
            if speed > max_speed_mps:
                if last_rejected and str(last_rejected.get("reject_reason", "")).startswith("jump"):
                    d2 = geo.distance_m(last_rejected["lat"], last_rejected["lon"], lat, lon)
                    dt2 = max((rx_utc - _t(last_rejected["rx_utc"])).total_seconds(), 1.0)
                    if d2 / dt2 <= max_speed_mps:
                        return True, None
                return False, f"jump of {d / 1000:.1f} km in {dt_s:.0f} s from the last good fix"
    return True, None


def stats(positions: list[dict[str, Any]], station: dict[str, Any] | None = None) -> dict[str, Any]:
    """Figures over valid fixes, oldest first. Absent data stays None, never zero."""
    if not positions:
        return {"fixes": 0, "max_alt_m": None, "distance_m": None, "duration_s": None, "first": None, "last": None,
                "ascent_rate_mps": None}
    alts = [p["alt_m"] for p in positions if p.get("alt_m") is not None]
    first, last = positions[0], positions[-1]
    out = {"fixes": len(positions), "max_alt_m": max(alts) if alts else None,
           "distance_m": geo.path_length_m((p["lat"], p["lon"]) for p in positions),
           "duration_s": (_t(last["rx_utc"]) - _t(first["rx_utc"])).total_seconds(), "first": first, "last": last,
           "ascent_rate_mps": None}
    # vertical rate over the last ~2 minutes of fixes
    recent = [p for p in positions if p.get("alt_m") is not None
              and (_t(last["rx_utc"]) - _t(p["rx_utc"])).total_seconds() <= 180]
    if len(recent) >= 2:
        span = (_t(recent[-1]["rx_utc"]) - _t(recent[0]["rx_utc"])).total_seconds()
        if span >= 20:
            out["ascent_rate_mps"] = (recent[-1]["alt_m"] - recent[0]["alt_m"]) / span
    if station and station.get("lat") is not None and last:
        out["from_station"] = look(station, last)
    return out


def look(station: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    s_alt = station.get("alt_m") or 0.0
    alt = p.get("alt_m") if p.get("alt_m") is not None else s_alt
    brg = geo.bearing_deg(station["lat"], station["lon"], p["lat"], p["lon"])
    return {"distance_m": geo.distance_m(station["lat"], station["lon"], p["lat"], p["lon"]),
            "bearing_deg": brg, "compass": geo.compass(brg),
            "elevation_deg": geo.elevation_deg(station["lat"], station["lon"], s_alt, p["lat"], p["lon"], alt),
            "slant_m": geo.slant_range_m(station["lat"], station["lon"], s_alt, p["lat"], p["lon"], alt),
            "station": station.get("name") or station.get("eui")}


# ------------------------------------------------------------------ exports
COLUMNS = ("rx_utc", "gps_utc", "lat", "lon", "alt_m", "speed_mps", "heading_deg", "sats", "pdop", "precision_bits")


def to_csv(positions: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for p in positions:
        w.writerow(["" if p.get(c) is None else p[c] for c in COLUMNS])
    return buf.getvalue()


def to_geojson(name: str, positions: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    coords = [[p["lon"], p["lat"], p["alt_m"] if p.get("alt_m") is not None else 0] for p in positions]
    feats: list[dict[str, Any]] = []
    if len(coords) >= 2:
        feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                      "properties": {"name": name, **meta, "times": [p["rx_utc"] for p in positions]}})
    for p in positions:
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"],
                                                                                      p.get("alt_m") or 0]},
                      "properties": {k: p.get(k) for k in COLUMNS if k not in ("lat", "lon")}})
    return json.dumps({"type": "FeatureCollection", "name": name, "features": feats}, indent=1)


def to_kml(name: str, positions: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    def coord(p: dict[str, Any]) -> str:
        return f"{p['lon']:.7f},{p['lat']:.7f},{(p.get('alt_m') or 0):.1f}"
    desc = escape(", ".join(f"{k}: {v}" for k, v in meta.items() if v is not None))
    when = "\n".join(f"<when>{escape(p['gps_utc'] or p['rx_utc'])}</when>" for p in positions)
    gx = "\n".join(f"<gx:coord>{coord(p).replace(',', ' ')}</gx:coord>" for p in positions)
    line = " ".join(coord(p) for p in positions)
    last = positions[-1] if positions else None
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">
<Document><name>{escape(name)}</name><description>{desc}</description>
<Style id="path"><LineStyle><color>ff2f6fdb</color><width>3</width></LineStyle></Style>
<Placemark><name>{escape(name)} track</name><styleUrl>#path</styleUrl>
<gx:Track><altitudeMode>absolute</altitudeMode>
{when}
{gx}
</gx:Track></Placemark>
<Placemark><name>{escape(name)} path</name><styleUrl>#path</styleUrl>
<LineString><extrude>1</extrude><altitudeMode>absolute</altitudeMode><coordinates>{line}</coordinates></LineString>
</Placemark>
{f'<Placemark><name>Last fix {escape(last["rx_utc"])}</name><Point><altitudeMode>absolute</altitudeMode><coordinates>{coord(last)}</coordinates></Point></Placemark>' if last else ''}
</Document></kml>
"""
