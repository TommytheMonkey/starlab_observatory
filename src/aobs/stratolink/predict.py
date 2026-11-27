"""Flight prediction with the Tawhiri predictor (SondeHub's public instance by default: GFS 0.5-degree wind forecasts,
https://api.v2.sondehub.org/tawhiri). Optional and only on request: each run sends the starting position, altitude
and rates to that service, and nothing else.

What is predicted depends on the mission state:
  before launch     from the launch site (the planned site, else the latest fix) with the profile's rates
  ASCENT            from the latest fix, with the measured ascent rate (when trustworthy) and the burst altitude
  FLOAT             drift at the current altitude for float_hours; no landing (it depends on when the float ends)
  DESCENT           from the latest fix straight to the ground, with the measured descent rate converted to the
                    sea-level rate Tawhiri expects
  LANDED, COMPLETE  nothing: it is on the ground

Uncertainty: the same prediction is re-run with the burst altitude +-10 %, ascent rate +-15 % and descent rate
+-20 %; the spread of those landing points is the "landing area". It shows sensitivity to the flight parameters
only, NOT the error of the wind forecast itself, and the page says so. Nothing is predicted without a position.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from . import geo
from .store import FLYING, iso, parse_t

UTC = dt.timezone.utc
DEFAULT_URL = "https://api.v2.sondehub.org/tawhiri"


class PredictionUnavailable(Exception):
    pass


def isa_density(h_m: float) -> float:
    """International Standard Atmosphere density (kg/m3), troposphere and lower stratosphere."""
    h = max(0.0, min(h_m, 47000.0))
    if h <= 11000:
        t = 288.15 - 0.0065 * h
        p = 101325 * (t / 288.15) ** 5.2559
    elif h <= 20000:
        t = 216.65
        p = 22632.1 * math.exp(-0.00015769 * (h - 11000))
    else:
        t = 216.65 + 0.001 * (h - 20000)
        p = 5474.89 * (216.65 / t) ** 34.1632
    return p / (287.05 * t)


def sea_level_descent(observed_mps: float, alt_m: float) -> float:
    """Descent speed scales with 1/sqrt(density): the sea-level equivalent of a speed measured at altitude."""
    return abs(observed_mps) * math.sqrt(isa_density(alt_m) / isa_density(0))


def basis_for(flight: dict[str, Any], analysis: dict[str, Any], params: dict[str, Any],
              override: dict[str, Any] | None = None, now: dt.datetime | None = None) -> dict[str, Any]:
    """The inputs a prediction will use, and the assumptions behind them. Raises PredictionUnavailable."""
    now = now or dt.datetime.now(UTC)
    o = override or {}
    pr = params.get("predict")
    if not pr:
        raise PredictionUnavailable(f"no prediction for a {params['label'].lower()}")
    state = flight.get("state")
    if state in ("LANDED", "COMPLETE", "ABORTED"):
        raise PredictionUnavailable(f"the mission is {state}: nothing to predict")
    fix = analysis.get("last_fix")
    stats = analysis.get("stats") or {}
    assumptions: list[str] = []
    b: dict[str, Any] = {"profile": pr["profile"], "ascent_rate_mps": pr.get("ascent_rate_mps"),
                         "burst_alt_m": pr.get("burst_alt_m"), "descent_rate_mps": pr.get("descent_rate_mps"),
                         "float_alt_m": pr.get("float_alt_m"), "float_hours": pr.get("float_hours"), "state": state}
    for k in ("ascent_rate_mps", "burst_alt_m", "descent_rate_mps", "float_alt_m", "float_hours"):
        if o.get(k) is not None:
            b[k] = float(o[k])
            assumptions.append(f"{k} {b[k]} set for this prediction")
    if state in FLYING:
        if not fix:
            raise PredictionUnavailable("no valid GPS fix to predict from")
        age = (now - parse_t(fix["rx_utc"])).total_seconds()
        if age > 3600:
            raise PredictionUnavailable(f"the latest fix is {age / 60:.0f} min old: too old to predict from")
        b.update(lat=fix["lat"], lon=fix["lon"], alt_m=fix.get("alt_m") or 0.0, start=fix.get("gps_utc") or fix["rx_utc"])
        assumptions.append(f"starts at the latest valid fix ({fix['rx_utc']}, {age:.0f} s old)")
        v, conf = stats.get("vertical_mps"), stats.get("vertical_confidence")
        if state == "ASCENT":
            if v is not None and v > 0.2 and conf in ("medium", "high") and o.get("ascent_rate_mps") is None:
                b["ascent_rate_mps"] = round(v, 2)
                assumptions.append(f"ascent rate {v:.2f} m/s as measured ({conf} confidence)")
            else:
                assumptions.append(f"ascent rate {b['ascent_rate_mps']} m/s from the profile (no reliable measurement)")
            if b["profile"] == "standard" and b["burst_alt_m"] and b["alt_m"] >= b["burst_alt_m"]:
                b["burst_alt_m"] = b["alt_m"] + 500
                assumptions.append("already above the planned burst altitude: assuming burst within 500 m")
        elif state == "FLOAT":
            b["profile"] = "float"
            b["float_alt_m"] = b["alt_m"]
            assumptions.append(f"floats at the current {b['alt_m']:.0f} m for {b['float_hours'] or 6} h; "
                               "the landing depends on when the float ends and is not predicted")
        elif state == "DESCENT":
            b["profile"] = "descent"
            if v is not None and v < -0.5 and conf in ("medium", "high") and o.get("descent_rate_mps") is None:
                b["descent_rate_mps"] = round(sea_level_descent(v, b["alt_m"]), 2)
                assumptions.append(f"descent {abs(v):.1f} m/s measured at {b['alt_m']:.0f} m = "
                                   f"{b['descent_rate_mps']:.1f} m/s at sea level (ISA density)")
            else:
                assumptions.append(f"sea-level descent rate {b['descent_rate_mps']} m/s from the profile")
    else:
        lat = o.get("lat") if o.get("lat") is not None else (fix or {}).get("lat")
        lon = o.get("lon") if o.get("lon") is not None else (fix or {}).get("lon")
        if lat is None or lon is None:
            raise PredictionUnavailable("no launch site: give one, or wait for a GPS fix")
        alt = o.get("alt_m") if o.get("alt_m") is not None else (fix or {}).get("alt_m") or 0.0
        start = o.get("launch_utc") or iso(now + dt.timedelta(minutes=10))
        b.update(lat=float(lat), lon=float(lon), alt_m=float(alt), start=start)
        assumptions.append("launch " + ("at the given site" if o.get("lat") is not None else "at the latest fix")
                           + f", {start}; rates and burst altitude from the profile")
    if b["profile"] == "standard" and not b.get("burst_alt_m"):
        raise PredictionUnavailable("a burst altitude is needed")
    b["assumptions"] = assumptions
    return b


def _query(b: dict[str, Any]) -> dict[str, Any]:
    q: dict[str, Any] = {"launch_latitude": round(b["lat"], 5), "launch_longitude": round(b["lon"] % 360, 5),
                         "launch_datetime": parse_t(b["start"]).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "launch_altitude": round(max(0.0, b["alt_m"]), 1)}
    if b["profile"] == "standard":
        q.update(profile="standard_profile", ascent_rate=b["ascent_rate_mps"], burst_altitude=b["burst_alt_m"],
                 descent_rate=b["descent_rate_mps"])
    elif b["profile"] == "descent":       # burst right away from the current altitude
        q.update(profile="standard_profile", ascent_rate=10, burst_altitude=round(b["alt_m"] + 1, 1),
                 descent_rate=b["descent_rate_mps"])
    else:
        stop = parse_t(b["start"]) + dt.timedelta(hours=float(b.get("float_hours") or 6))
        q.update(profile="float_profile", ascent_rate=b["ascent_rate_mps"] or 1.0,
                 float_altitude=max(b["float_alt_m"] or b["alt_m"], b["alt_m"]),
                 stop_datetime=stop.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return q


def _run(url: str, q: dict[str, Any], opener: Callable[..., Any], timeout: float) -> dict[str, Any]:
    full = f"{url}?{urllib.parse.urlencode(q)}"
    try:
        with opener(urllib.request.Request(full, headers={"User-Agent": "Starlab-Stratolink/1"}), timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("description")
        except Exception:  # noqa: BLE001
            msg = None
        raise PredictionUnavailable(f"predictor refused: {msg or f'HTTP {e.code}'}") from None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        raise PredictionUnavailable(f"predictor unreachable: {exc}") from None
    if data.get("error"):
        raise PredictionUnavailable(f"predictor error: {data['error'].get('description', data['error'])}")
    stages = []
    for st in data.get("prediction") or []:
        pts = [[p["latitude"], ((p["longitude"] + 180) % 360) - 180, p["altitude"], p["datetime"]]
               for p in st.get("trajectory") or []]
        stages.append({"stage": st.get("stage"), "points": pts})
    if not stages or not stages[-1]["points"]:
        raise PredictionUnavailable("the predictor returned no trajectory")
    return {"stages": stages, "dataset": (data.get("request") or {}).get("dataset"),
            "warnings": data.get("warnings") or {}}


def predict(b: dict[str, Any], url: str = DEFAULT_URL, opener: Callable[..., Any] = urllib.request.urlopen,
            timeout: float = 25.0, variants: bool = True) -> dict[str, Any]:
    main = _run(url, _query(b), opener, timeout)
    lands = b["profile"] != "float"
    end = main["stages"][-1]["points"][-1]
    out: dict[str, Any] = {"stages": main["stages"], "dataset": main["dataset"], "warnings": main["warnings"],
                           "landing": {"lat": end[0], "lon": end[1], "alt_m": end[2], "t": end[3]} if lands else None,
                           "variants": [], "spread_m": None, "assumptions": list(b["assumptions"]),
                           "uncertainty": ["Wind-forecast error is not included: real landings can be tens of km from "
                                           "a prediction, more for long flights."]}
    if lands and variants:
        tries = []
        if b["profile"] == "standard" and b.get("burst_alt_m"):
            tries += [("burst -10 %", {"burst_alt_m": max(b["alt_m"] + 100, b["burst_alt_m"] * 0.9)}),
                      ("burst +10 %", {"burst_alt_m": b["burst_alt_m"] * 1.1})]
            if b.get("state") != "ASCENT" or b.get("ascent_rate_mps"):
                tries += [("ascent -15 %", {"ascent_rate_mps": b["ascent_rate_mps"] * 0.85}),
                          ("ascent +15 %", {"ascent_rate_mps": b["ascent_rate_mps"] * 1.15})]
        tries += [("descent -20 %", {"descent_rate_mps": b["descent_rate_mps"] * 0.8}),
                  ("descent +20 %", {"descent_rate_mps": b["descent_rate_mps"] * 1.2})]
        for label, change in tries:
            try:
                r = _run(url, _query({**b, **change}), opener, timeout)
            except PredictionUnavailable as exc:
                out["variants"].append({"label": label, "error": str(exc)})
                continue
            e = r["stages"][-1]["points"][-1]
            out["variants"].append({"label": label, "lat": e[0], "lon": e[1], "t": e[3]})
        got = [v for v in out["variants"] if "lat" in v]
        if got:
            out["spread_m"] = max(geo.distance_m(end[0], end[1], v["lat"], v["lon"]) for v in got)
            out["uncertainty"].insert(0, f"Landing area: {len(got)} parameter variants land within "
                                         f"{out['spread_m'] / 1000:.1f} km of the central prediction.")
    if not lands:
        out["uncertainty"].insert(0, "Float drift only: no landing predicted.")
    return out
