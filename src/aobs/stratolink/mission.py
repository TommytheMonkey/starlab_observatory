"""The mission engine: launch, flight-phase and landing detection from timestamped GPS fixes, contact and GPS-fix
status, mission statistics and the event timeline. Runs every few seconds in the daemon (`Engine.step`) and keeps its
state in the flights row, so a restart continues where it stopped.

Rules it never breaks:
  * nothing launches unless the mission is ARMED (an operator armed it);
  * every automatic decision needs several real fixes, newer than the staleness limit: silence (loss of RF contact)
    is never evidence of anything, so it never lands, bursts or changes phase a mission - it only makes the phase
    "uncertain";
  * LANDED means "landing suspected": only an operator completes a mission;
  * no position is ever invented: statistics and tracks use received, valid, non-spike fixes only.

Rates use the Theil-Sen estimator (median of pairwise slopes) over a time window, which copes with irregular
reporting intervals and with a minority of bad altitudes. An isolated altitude spike (a fix far from the straight
line between its neighbours, which agree with each other) is dropped before any of this.
"""

from __future__ import annotations

import copy
import datetime as dt
import statistics
from dataclasses import dataclass
from typing import Any

from . import geo
from . import store as _store
from .store import FLYING, RECORDING, Store, iso, parse_t

UTC = dt.timezone.utc

# Starting thresholds by balloon type. Every key can be overridden per mission (flights.params).
PROFILES: dict[str, dict[str, Any]] = {
    "latex": {   # high-altitude latex: fast ascent (~5 m/s), burst, fast descent under parachute
        "label": "High-altitude latex", "baseline_min_fixes": 3, "baseline_mad_m": 30,
        "launch_gain_m": 60, "launch_rate_mps": 1.0, "launch_confirm_fixes": 3,
        "rate_window_s": 300, "float_rate_mps": 0.4, "float_min_s": 900, "float_min_agl_m": 3000,
        "descent_rate_mps": 2.0, "descent_confirm_fixes": 3, "ascent_resume_rate_mps": 1.0,
        "burst": True, "burst_drop_m": 300, "burst_rate_mps": 5.0,
        "landing_rate_mps": 0.5, "landing_speed_mps": 2.0, "landing_min_s": 300, "landing_min_fixes": 3,
        "landing_alt_range_m": 50, "spike_m": 150, "spike_rate_mps": 20, "stale_factor": 3.0, "stale_min_s": 600,
        "low_battery_pct": 20, "low_battery_v": 3.45, "max_speed_mps": 150,
        "milestones_m": [1000, 5000, 10000, 15000, 20000, 25000, 30000, 35000, 40000],
        "predict": {"profile": "standard", "ascent_rate_mps": 5.0, "burst_alt_m": 30000, "descent_rate_mps": 5.0},
    },
    "mylar": {   # foil balloon: slow ascent, floats near its burst/leak altitude, may pop
        "label": "Mylar (foil) balloon", "baseline_min_fixes": 3, "baseline_mad_m": 30,
        "launch_gain_m": 50, "launch_rate_mps": 0.5, "launch_confirm_fixes": 3,
        "rate_window_s": 600, "float_rate_mps": 0.3, "float_min_s": 900, "float_min_agl_m": 1000,
        "descent_rate_mps": 1.0, "descent_confirm_fixes": 3, "ascent_resume_rate_mps": 0.5,
        "burst": True, "burst_drop_m": 500, "burst_rate_mps": 4.0,
        "landing_rate_mps": 0.4, "landing_speed_mps": 2.0, "landing_min_s": 600, "landing_min_fixes": 3,
        "landing_alt_range_m": 50, "spike_m": 150, "spike_rate_mps": 15, "stale_factor": 3.0, "stale_min_s": 900,
        "low_battery_pct": 20, "low_battery_v": 3.45, "max_speed_mps": 150,
        "milestones_m": [1000, 3000, 5000, 7500, 10000, 12500, 15000],
        "predict": {"profile": "float", "ascent_rate_mps": 2.0, "float_alt_m": 8000, "float_hours": 12,
                    "descent_rate_mps": 4.0},
    },
    "pico": {    # pico: very slow ascent, days at float, sinks at night and rises in the day (not a burst)
        "label": "Long-duration pico balloon", "baseline_min_fixes": 3, "baseline_mad_m": 30,
        "launch_gain_m": 50, "launch_rate_mps": 0.3, "launch_confirm_fixes": 3,
        "rate_window_s": 1800, "float_rate_mps": 0.25, "float_min_s": 1800, "float_min_agl_m": 1000,
        "descent_rate_mps": 0.6, "descent_confirm_fixes": 3, "ascent_resume_rate_mps": 0.3,
        "burst": False, "burst_drop_m": 1000, "burst_rate_mps": 5.0,
        "landing_rate_mps": 0.3, "landing_speed_mps": 2.0, "landing_min_s": 1800, "landing_min_fixes": 3,
        "landing_alt_range_m": 60, "spike_m": 200, "spike_rate_mps": 10, "stale_factor": 3.0, "stale_min_s": 3600,
        "low_battery_pct": 15, "low_battery_v": 3.3, "max_speed_mps": 150,
        "milestones_m": [1000, 5000, 8000, 10000, 12000, 14000],
        "predict": {"profile": "float", "ascent_rate_mps": 1.0, "float_alt_m": 12000, "float_hours": 24,
                    "descent_rate_mps": 3.0},
    },
    "tethered": {  # tethered ground test: tens of metres, slow, comes back to the baseline
        "label": "Tethered ground test", "baseline_min_fixes": 3, "baseline_mad_m": 10,
        "launch_gain_m": 10, "launch_rate_mps": 0.1, "launch_confirm_fixes": 3,
        "rate_window_s": 180, "float_rate_mps": 0.1, "float_min_s": 180, "float_min_agl_m": 8,
        "descent_rate_mps": 0.1, "descent_confirm_fixes": 3, "ascent_resume_rate_mps": 0.1,
        "burst": False, "burst_drop_m": 1e9, "burst_rate_mps": 1e9,
        "landing_rate_mps": 0.08, "landing_speed_mps": 1.0, "landing_min_s": 120, "landing_min_fixes": 3,
        "landing_alt_range_m": 8, "landing_near_baseline_m": 8, "spike_m": 40, "spike_rate_mps": 2, "stale_factor": 3.0,
        "stale_min_s": 300, "low_battery_pct": 20, "low_battery_v": 3.45, "max_speed_mps": 30,
        "milestones_m": [], "predict": None,
    },
}
PRE_ARM_LOOKBACK_S = 1800          # ground fixes from just before arming count towards the baseline


def params_for(flight: dict[str, Any]) -> dict[str, Any]:
    p = copy.deepcopy(PROFILES[flight.get("balloon_type") or "latex"])
    for k, v in (flight.get("params") or {}).items():
        if k == "predict" and isinstance(v, dict) and isinstance(p.get("predict"), dict):
            p["predict"].update(v)
        elif k in p:
            p[k] = v
    return p


# ------------------------------------------------------------------ observations
@dataclass
class Fix:
    t: float                 # epoch seconds: GPS time when plausible, else reception time
    lat: float
    lon: float
    alt: float | None
    speed: float | None
    row: dict[str, Any]


def fixes_from(rows: list[dict[str, Any]]) -> list[Fix]:
    """Valid positions as observations: one per GPS fix (a fix sent twice, or heard twice, counts once), in time
    order. The GPS timestamp is used when it is within 10 minutes of reception (the tracker's own clock is better
    than our reception time, which shifts with relays), else reception time."""
    out: dict[Any, Fix] = {}
    for r in rows:
        rx = parse_t(r["rx_utc"]).timestamp()
        g = parse_t(r["gps_utc"]).timestamp() if r.get("gps_utc") else None
        t = g if g is not None and abs(g - rx) < 600 else rx
        key = r.get("gps_utc") or r["rx_utc"]
        if key in out:
            continue
        out[key] = Fix(t, r["lat"], r["lon"], r.get("alt_m"), r.get("speed_mps"), r)
    return sorted(out.values(), key=lambda f: f.t)


def drop_spikes(fx: list[Fix], spike_m: float, spike_rate_mps: float = 20.0) -> tuple[list[Fix], list[Fix]]:
    """(kept, spikes). A fix is a spike when its altitude is more than spike_m (or 25 % of the neighbours'
    difference) off the time-interpolated line between its neighbours, AND both the jump into it and the jump out of
    it break the local trend (the rate before and after it) by more than spike_rate_mps, in opposite directions.
    A burst apex is reached at the trend's own rate, so it is never a spike; a wild fix in a fast descent is. End
    points are kept: they are judged when the next fix arrives, and no decision ever rests on a single fix."""
    alt = [f for f in fx if f.alt is not None]
    spikes: set[int] = set()

    def slope(a: Fix, b: Fix) -> float:
        return (b.alt - a.alt) / (b.t - a.t)
    for i in range(1, len(alt) - 1):
        a, b, c = alt[i - 1], alt[i], alt[i + 1]
        if not (a.t < b.t < c.t):
            continue
        expect = a.alt + (c.alt - a.alt) * (b.t - a.t) / (c.t - a.t)
        if abs(b.alt - expect) <= max(spike_m, 0.25 * abs(c.alt - a.alt)):
            continue
        r_in, r_out = slope(a, b), slope(b, c)
        before = slope(alt[i - 2], a) if i >= 2 and alt[i - 2].t < a.t and id(alt[i - 2]) not in spikes else 0.0
        after = slope(c, alt[i + 2]) if i + 2 < len(alt) and c.t < alt[i + 2].t else 0.0
        if ((r_in - before) * (r_out - after) < 0 and abs(r_in - before) > spike_rate_mps
                and abs(r_out - after) > spike_rate_mps):
            spikes.add(id(b))
    return [f for f in fx if id(f) not in spikes], [f for f in fx if id(f) in spikes]


def theil_sen(pts: list[tuple[float, float]]) -> float | None:
    slopes = [(pts[j][1] - pts[i][1]) / (pts[j][0] - pts[i][0]) for i in range(len(pts)) for j in range(i + 1, len(pts))
              if pts[j][0] - pts[i][0] >= 1]
    return statistics.median(slopes) if slopes else None


def window(fx: list[Fix], end: float, seconds: float, min_n: int = 3) -> list[Fix]:
    """Fixes with altitude in (end - seconds, end]; widened up to 4x when fewer than min_n."""
    alt = [f for f in fx if f.alt is not None and f.t <= end]
    for mult in (1, 2, 4):
        w = [f for f in alt if f.t > end - seconds * mult]
        if len(w) >= min_n:
            return w[-40:]
    return alt[-min_n:] if len(alt) >= 2 else alt


def rate(fx: list[Fix], end: float, seconds: float) -> dict[str, Any]:
    """Vertical rate at `end` and how much to trust it."""
    w = window(fx, end, seconds)
    if len(w) < 2:
        return {"mps": None, "n": len(w), "span_s": 0, "confidence": "none"}
    r = theil_sen([(f.t, f.alt) for f in w])
    span = w[-1].t - w[0].t
    resid = [abs(f.alt - (w[-1].alt + r * (f.t - w[-1].t))) for f in w] if r is not None else []
    mad = statistics.median(resid) if resid else None
    conf = ("high" if len(w) >= 5 and span >= seconds / 2 and (mad or 0) < 30 else
            "medium" if len(w) >= 3 else "low")
    return {"mps": r, "n": len(w), "span_s": span, "confidence": conf, "residual_m": mad}


def mad(xs: list[float]) -> float:
    m = statistics.median(xs)
    return statistics.median([abs(x - m) for x in xs])


# ------------------------------------------------------------------ detectors
def detect_launch(fx: list[Fix], p: dict[str, Any]) -> dict[str, Any]:
    """Look for the ground segment followed by a sustained climb. Returns {state: launched|waiting, ...}."""
    alt = [f for f in fx if f.alt is not None]
    need = p["baseline_min_fixes"]
    if len(alt) < need:
        return {"state": "waiting", "detail": f"baseline needs {need} fixes ({len(alt)} so far)"}
    conf_n = p["launch_confirm_fixes"]
    for s in range(need, len(alt) - conf_n + 1):
        ground = alt[max(0, s - 10):s]
        base = statistics.median([f.alt for f in ground])
        if mad([f.alt for f in ground]) > p["baseline_mad_m"]:
            continue
        run = alt[s:]
        if not all(f.alt > base + p["launch_gain_m"] for f in run[:conf_n]):
            continue
        if not all(f.alt > base + p["launch_gain_m"] / 2 for f in run):    # it came back down: not a launch
            continue
        r = theil_sen([(f.t, f.alt) for f in ground[-2:] + run])
        if r is None or r < p["launch_rate_mps"]:
            continue
        last_ground, first_up = ground[-1], run[0]
        climb = theil_sen([(f.t, f.alt) for f in run]) if len(run) >= 2 else None
        climb = climb if climb and climb > 0 else r
        t_est = first_up.t - (first_up.alt - base) / climb
        t_est = min(max(t_est, last_ground.t), first_up.t)
        return {"state": "launched", "t": t_est, "rate_mps": climb, "baseline_alt_m": base,
                "lat": statistics.median([f.lat for f in ground]), "lon": statistics.median([f.lon for f in ground]),
                "ground_fixes": len(ground), "confirm_fixes": len(run),
                "detail": f"{len(run)} fixes above the {len(ground)}-fix baseline {base:.0f} m, climbing {r:.2f} m/s"}
    base = statistics.median([f.alt for f in alt[-10:]])
    return {"state": "waiting", "baseline_alt_m": base, "baseline_fixes": min(len(alt), 10),
            "detail": f"on the ground at ~{base:.0f} m ({min(len(alt), 10)} fixes); waiting for a climb of "
                      f"{p['launch_gain_m']} m at {p['launch_rate_mps']} m/s over {conf_n} fixes"}


def ground_speed(fx: list[Fix], end: float, seconds: float, max_mps: float) -> float | None:
    """Median horizontal speed: reported GPS speeds where present, else from consecutive fixes."""
    w = [f for f in fx if end - seconds < f.t <= end]
    rep = [f.speed for f in w if f.speed is not None and 0 <= f.speed <= max_mps]
    if rep:
        return statistics.median(rep)
    seg = [geo.distance_m(a.lat, a.lon, b.lat, b.lon) / (b.t - a.t) for a, b in zip(w, w[1:]) if b.t - a.t >= 10]
    seg = [v for v in seg if v <= max_mps]
    return statistics.median(seg) if seg else None


def suggest_phase(state: str, fx: list[Fix], p: dict[str, Any], base: float | None, max_alt: float | None
                  ) -> tuple[str | None, str, dict[str, Any]]:
    """(new state or None, reason, evidence) for a launched mission, from the latest window of fixes."""
    alt = [f for f in fx if f.alt is not None]
    if len(alt) < 3:
        return None, "fewer than 3 fixes", {}
    end = alt[-1].t
    r = rate(alt, end, p["rate_window_s"])
    ev = {"rate": r}
    if r["mps"] is None or r["confidence"] in ("none", "low"):
        return None, "not enough fixes for a rate", ev
    v = r["mps"]
    agl = alt[-1].alt - base if base is not None else None
    # turning points (burst, starting to climb again) are judged on the last few fixes, so a long window full of
    # the previous phase cannot hide them; the window rate is for level flight and landing
    k = p["descent_confirm_fixes"]

    def turn(seq: list[Fix]) -> tuple[float | None, bool, bool]:
        tail = seq[-(k + 1):]
        if len(tail) < 3:
            return None, False, False
        r_ = theil_sen([(f.t, f.alt) for f in tail])
        down = all(b.alt < a.alt + 5 for a, b in zip(tail, tail[1:])) and tail[-1].alt < tail[0].alt
        up = all(b.alt > a.alt - 5 for a, b in zip(tail, tail[1:])) and tail[-1].alt > tail[0].alt
        return r_, down, up
    # a turn must hold with and without the newest fix: the newest one cannot be checked for a spike yet (it has
    # no successor), and no decision may rest on a single fix
    tr, falling, rising = turn(alt)
    tr0, falling0, rising0 = turn(alt[:-1])
    if tr is not None and tr0 is not None:
        falling = falling and falling0 and tr0 <= -p["descent_rate_mps"]
        rising = rising and rising0 and tr0 >= p["ascent_resume_rate_mps"]
    else:
        falling = rising = False
    ev["tail_rate_mps"] = tr
    if state in ("ASCENT", "FLOAT", "LANDED") and tr is not None and tr <= -p["descent_rate_mps"] and falling:
        burst = (state in ("ASCENT", "FLOAT") and p.get("burst") and max_alt is not None
                 and max_alt - alt[-1].alt >= p["burst_drop_m"] and tr <= -p["burst_rate_mps"])
        ev["burst"] = bool(burst)
        return "DESCENT", f"descending {tr:.2f} m/s over the last {k + 1} fixes", ev
    if state in ("FLOAT", "DESCENT", "LANDED") and tr is not None and tr >= p["ascent_resume_rate_mps"] and rising:
        return "ASCENT", f"climbing {tr:.2f} m/s over the last {k + 1} fixes", ev
    w = window(alt, end, p["float_min_s"], min_n=4)
    if (state in ("ASCENT", "DESCENT") and len(w) >= 4 and w[-1].t - w[0].t >= p["float_min_s"] * 0.9
            and agl is not None and agl >= p["float_min_agl_m"]):
        fr = theil_sen([(f.t, f.alt) for f in w])
        spread = max(f.alt for f in w) - min(f.alt for f in w)
        level_limit = max(4 * p["spike_m"] / 3, p["float_rate_mps"] * (w[-1].t - w[0].t))
        # level means level: a small overall spread and no trend now, not a median slope that averages a climb and
        # a fall (a window straddling a burst apex has a median slope near zero)
        if fr is not None and abs(fr) <= p["float_rate_mps"] and spread <= level_limit and abs(v) <= p["float_rate_mps"]:
            return "FLOAT", f"level ({fr:+.2f} m/s) for {(w[-1].t - w[0].t) / 60:.0f} min at {agl:.0f} m above " \
                            f"launch", ev
    if state == "DESCENT":
        lw = [f for f in alt if f.t > end - p["landing_min_s"]]
        if len(lw) >= p["landing_min_fixes"] and lw[-1].t - lw[0].t >= p["landing_min_s"] * 0.8:
            lr = theil_sen([(f.t, f.alt) for f in lw]) or 0.0
            spread = max(f.alt for f in lw) - min(f.alt for f in lw)
            gs = ground_speed(fx, end, p["landing_min_s"], p["max_speed_mps"])
            near = p.get("landing_near_baseline_m")
            if (abs(lr) <= p["landing_rate_mps"] and spread <= p["landing_alt_range_m"]
                    and (gs is None or gs <= p["landing_speed_mps"])
                    and (near is None or base is None or abs(alt[-1].alt - base) <= near)):
                ev["landing"] = {"fixes": len(lw), "span_s": lw[-1].t - lw[0].t, "rate_mps": lr, "spread_m": spread,
                                 "ground_speed_mps": gs}
                return "LANDED", f"stationary: {lr:+.2f} m/s, {spread:.0f} m altitude spread, ground speed " \
                                 f"{'unknown' if gs is None else f'{gs:.1f} m/s'} over {len(lw)} fixes / " \
                                 f"{(lw[-1].t - lw[0].t) / 60:.0f} min", ev
    return None, f"{v:+.2f} m/s ({r['confidence']} confidence)", ev


# ------------------------------------------------------------------ statistics
def statistics_for(fx: list[Fix], p: dict[str, Any], launch: dict[str, Any] | None) -> dict[str, Any]:
    alt = [f for f in fx if f.alt is not None]
    out: dict[str, Any] = {"fixes": len(fx), "max_alt_m": None, "max_alt_t": None, "max_ascent_mps": None,
                           "max_descent_mps": None, "max_ground_speed_mps": None, "distance_m": None,
                           "downrange_m": None, "alt_above_launch_m": None, "vertical_mps": None,
                           "vertical_confidence": "none", "ground_speed_mps": None}
    if not fx:
        return out
    out["distance_m"] = geo.path_length_m((f.lat, f.lon) for f in fx)
    if alt:
        top = max(alt, key=lambda f: f.alt)
        out["max_alt_m"], out["max_alt_t"] = top.alt, top.t
        rates = [rate(alt, f.t, p["rate_window_s"]) for f in alt[2:]]
        good = [r["mps"] for r in rates if r["mps"] is not None and r["confidence"] in ("medium", "high")]
        if good:
            out["max_ascent_mps"] = max(0.0, max(good))
            out["max_descent_mps"] = min(0.0, min(good))
        now_r = rate(alt, alt[-1].t, p["rate_window_s"])
        out["vertical_mps"], out["vertical_confidence"] = now_r["mps"], now_r["confidence"]
    speeds = [f.speed for f in fx if f.speed is not None and 0 <= f.speed <= p["max_speed_mps"]]
    seg = [geo.distance_m(a.lat, a.lon, b.lat, b.lon) / (b.t - a.t) for a, b in zip(fx, fx[1:]) if b.t - a.t >= 20]
    seg = [v for v in seg if v <= p["max_speed_mps"]]
    if speeds or seg:
        out["max_ground_speed_mps"] = max(speeds or seg)
    last = fx[-1]
    out["ground_speed_mps"] = last.speed if last.speed is not None else (seg[-1] if seg else None)
    if launch and launch.get("lat") is not None:
        out["downrange_m"] = geo.distance_m(launch["lat"], launch["lon"], last.lat, last.lon)
        if launch.get("alt_m") is not None and last.alt is not None:
            out["alt_above_launch_m"] = last.alt - launch["alt_m"]
    return out


def freshness(age_s: float | None, interval_s: float | None, p: dict[str, Any]) -> dict[str, Any]:
    if age_s is None:
        return {"state": "none", "stale_after_s": None}
    stale_after = max(p["stale_min_s"], p["stale_factor"] * interval_s) if interval_s else p["stale_min_s"]
    aging_after = stale_after / 2
    return {"state": "fresh" if age_s < aging_after else "aging" if age_s < stale_after else "stale",
            "stale_after_s": stale_after, "age_s": age_s}


def observed_interval(fx: list[Fix]) -> float | None:
    last = fx[-13:]
    gaps = [b.t - a.t for a, b in zip(last, last[1:]) if b.t - a.t > 1]
    return statistics.median(gaps) if len(gaps) >= 2 else None


# ------------------------------------------------------------------ the engine
class Engine:
    def __init__(self, store: Store, loss_warn_s: float = 180, loss_lost_s: float = 600):
        self.store, self.loss_warn_s, self.loss_lost_s = store, loss_warn_s, loss_lost_s

    def data(self, f: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
        node = f["tracker_node"]
        armed = parse_t(f["armed_utc"]) if f.get("armed_utc") else now
        since = iso(armed - dt.timedelta(seconds=PRE_ARM_LOOKBACK_S))
        until = f.get("completed_utc") or iso(now)
        rows = self.store.positions(node, since, until)
        last_rx = self.store.one("SELECT MAX(rx_utc) AS t FROM rx WHERE sender = ? AND rx_utc <= ?", (node, until))
        batt = self.store.q("SELECT rx_utc, battery_level, voltage FROM telemetry WHERE node = ? AND kind = 'device' "
                            "AND rx_utc >= ? AND rx_utc <= ? ORDER BY rx_utc", (node, since, until))
        return {"rows": rows, "last_rx": (last_rx or {}).get("t"), "battery": batt}

    def analyze(self, f: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
        """Everything the page shows about a mission, computed from stored fixes. Changes nothing."""
        now = now or _store.now()
        p = params_for(f)
        d = self.data(f, now)
        allfx = fixes_from(d["rows"])
        fx, spikes = drop_spikes(allfx, p["spike_m"], p["spike_rate_mps"])
        launch = None
        if f.get("launch_utc"):
            launch = {"t": parse_t(f["launch_utc"]).timestamp(), "lat": f.get("launch_lat"), "lon": f.get("launch_lon"),
                      "alt_m": f.get("launch_alt_m"), "method": f.get("launch_method")}
        flight_fx = [x for x in fx if launch is None or x.t >= launch["t"] - 60] if launch else fx
        stats = statistics_for(flight_fx if launch else fx, p, launch)
        interval = observed_interval(allfx)
        last_fix = allfx[-1] if allfx else None
        fix_age = now.timestamp() - parse_t(last_fix.row["rx_utc"]).timestamp() if last_fix else None
        contact_age = (now - parse_t(d["last_rx"])).total_seconds() if d["last_rx"] else None
        warn = f.get("loss_warn_s") or self.loss_warn_s
        lost = f.get("loss_lost_s") or self.loss_lost_s
        contact = "never" if contact_age is None else "ok" if contact_age < warn else "late" if contact_age < lost \
            else "lost"
        fresh = freshness(fix_age, interval, p)
        gps_lost_after = max(fresh["stale_after_s"] or p["stale_min_s"], 2 * (interval or 0))
        gps = ("none" if last_fix is None else
               "lost" if contact in ("ok", "late") and fix_age is not None and fix_age > gps_lost_after else
               "ok" if fix_age is not None and fix_age <= gps_lost_after else "unknown")
        state = f.get("state") or "PREPARATION"
        phase_conf = "n/a"
        if state in FLYING:
            phase_conf = "uncertain" if fresh["state"] == "stale" or not fx else \
                ("low" if stats["vertical_confidence"] in ("none", "low") else stats["vertical_confidence"])
        launch_check = detect_launch(fx, p) if state == "ARMED" else None
        b = d["battery"][-1] if d["battery"] else None
        return {"flight_id": f["id"], "state": state, "balloon_type": f.get("balloon_type"), "params": p,
                "at": iso(now), "fixes": len(allfx), "spikes": [x.row["id"] for x in spikes],
                "last_fix": last_fix.row if last_fix else None, "last_fix_age_s": fix_age,
                "last_rx": d["last_rx"], "contact_age_s": contact_age, "contact": contact,
                "contact_thresholds": {"late_s": warn, "lost_s": lost},
                "gps": gps, "gps_lost_after_s": gps_lost_after, "freshness": fresh, "observed_interval_s": interval,
                "phase_confidence": phase_conf, "launch": launch, "launch_check": launch_check, "stats": stats,
                "battery": b,
                "met_s": now.timestamp() - launch["t"] if launch else None,
                "armed_s": (now - parse_t(f["armed_utc"])).total_seconds() if f.get("armed_utc") else None}

    def step(self, fid: int, now: dt.datetime | None = None) -> dict[str, Any] | None:
        """Advance one mission: automatic transitions and timeline events from new evidence."""
        now = now or _store.now()
        f = self.store.flight(fid)
        if not f or f.get("state") not in RECORDING:
            return None
        a = self.analyze(f, now)
        p = a["params"]
        eng = dict(f.get("engine") or {})
        st = self.store
        # ---- contact and GPS status (independent of phase; events on change only)
        prev = eng.get("contact")
        if a["contact"] != prev:
            if a["contact"] == "lost" and prev in ("ok", "late"):
                st.add_event(fid, "contact_lost", "RF contact lost", auto=True, at=now,
                             detail=f"no packet for {a['contact_age_s']:.0f} s (last {a['last_rx']}); last valid fix "
                                    f"kept: {_fmt_fix(a['last_fix'])}")
            elif a["contact"] == "ok" and prev in ("lost",):
                st.add_event(fid, "contact_restored", "RF contact restored", auto=True, at=now,
                             detail=f"packet received {a['last_rx']}")
            eng["contact"] = a["contact"]
        if a["gps"] != eng.get("gps"):
            if a["gps"] == "lost" and eng.get("gps") == "ok":
                st.add_event(fid, "gps_lost", "GPS fix lost", auto=True, at=now,
                             detail=f"packets arriving but no valid fix for {a['last_fix_age_s']:.0f} s")
            elif a["gps"] == "ok" and eng.get("gps") == "lost":
                st.add_event(fid, "gps_restored", "GPS fix restored", auto=True, at=now,
                             detail=_fmt_fix(a["last_fix"]))
            eng["gps"] = a["gps"]
        # ---- battery
        b = a["battery"]
        if b:
            low = ((b["battery_level"] is not None and b["battery_level"] <= p["low_battery_pct"]
                    and b["battery_level"] != 101) or (b["voltage"] is not None and 0 < b["voltage"] <= p["low_battery_v"]))
            if low and not eng.get("low_battery"):
                st.add_event(fid, "low_battery", "Low battery", auto=True, at=parse_t(b["rx_utc"]),
                             detail=f"{b['battery_level']} %, {b['voltage']} V")
                eng["low_battery"] = True
            elif eng.get("low_battery") and b["battery_level"] is not None and \
                    b["battery_level"] > p["low_battery_pct"] + 5 and b["battery_level"] != 101:
                eng["low_battery"] = False
        state = a["state"]
        new_fix = a["last_fix"] is not None and a["last_fix"]["id"] != eng.get("last_fix_id")
        fresh = a["freshness"]["state"] != "stale"
        f2 = f
        if new_fix and fresh:
            eng["last_fix_id"] = a["last_fix"]["id"]
            p_fx, _ = drop_spikes(fixes_from(self.data(f, now)["rows"]), p["spike_m"], p["spike_rate_mps"])
            if state == "ARMED":
                lc = a["launch_check"] or {}
                if lc.get("state") == "launched":
                    t = dt.datetime.fromtimestamp(lc["t"], UTC)
                    f2 = st.transition(fid, "ASCENT", lc["detail"], auto=True, at=now, launch_utc=iso(t),
                                       launch_method="detected", launch_lat=lc["lat"], launch_lon=lc["lon"],
                                       launch_alt_m=lc["baseline_alt_m"])
                    st.add_event(fid, "launch", "Launch detected (T+00:00:00)", auto=True, at=t,
                                 detail=f"{lc['detail']}; estimated launch {iso(t)} at {lc['lat']:.5f}, "
                                        f"{lc['lon']:.5f}, {lc['baseline_alt_m']:.0f} m", data=lc, dedupe="launch")
            elif state in FLYING:
                launch_t = parse_t(f["launch_utc"]).timestamp() if f.get("launch_utc") else None
                fl = [x for x in p_fx if launch_t is None or x.t >= launch_t - 60]
                to, why, ev = suggest_phase(state, fl, p, f.get("launch_alt_m"), a["stats"]["max_alt_m"])
                if to and to != state:
                    if state == "ASCENT" or (state == "FLOAT" and to == "DESCENT"):
                        m = a["stats"]
                        if m["max_alt_m"] is not None:
                            st.add_event(fid, "max_altitude", f"Maximum altitude {m['max_alt_m']:,.0f} m "
                                         f"({m['max_alt_m'] * 3.28084:,.0f} ft)", auto=True,
                                         at=dt.datetime.fromtimestamp(m["max_alt_t"], UTC),
                                         dedupe=f"max:{round(m['max_alt_m'])}")
                    if to == "DESCENT" and ev.get("burst"):
                        eng["burst_logged"] = True
                        m = a["stats"]
                        st.add_event(fid, "burst", "Possible balloon burst", auto=True,
                                     at=dt.datetime.fromtimestamp(m["max_alt_t"], UTC),
                                     detail=f"peak {m['max_alt_m']:,.0f} m, now descending "
                                            f"{ev['tail_rate_mps']:.1f} m/s", dedupe="burst")
                    extra: dict[str, Any] = {}
                    if to == "LANDED":
                        lf = fl[-1]
                        extra = {"landed_utc": iso(dt.datetime.fromtimestamp(lf.t, UTC)), "landed_lat": lf.lat,
                                 "landed_lon": lf.lon, "landed_alt_m": lf.alt}
                    elif state == "LANDED":
                        extra = {"landed_utc": None, "landed_lat": None, "landed_lon": None, "landed_alt_m": None}
                    f2 = st.transition(fid, to, why, auto=True, at=now, **extra)
                    eng["descent_from"] = state if to == "DESCENT" else None
                    title = {"FLOAT": "Float detected", "DESCENT": "Descent detected", "ASCENT": "Ascent detected",
                             "LANDED": "Possible landing: awaiting confirmation"}[to]
                    if state == "LANDED":
                        title = f"Moving again: landing not confirmed ({to.lower()})"
                    st.add_event(fid, to.lower() if state != "LANDED" else "false_landing", title, auto=True, at=now,
                                 detail=why + (f"; at {_fmt_fix(fl[-1].row)}" if to == "LANDED" else ""), data=ev)
                elif (state == "DESCENT" and eng.get("descent_from") in ("ASCENT", "FLOAT") and p.get("burst")
                      and not eng.get("burst_logged")):
                    # a burst is often detected as a descent before the drop is large: check again as it falls
                    m = a["stats"]
                    tr = ev.get("tail_rate_mps")
                    if (m["max_alt_m"] is not None and fl and fl[-1].alt is not None and tr is not None
                            and m["max_alt_m"] - fl[-1].alt >= p["burst_drop_m"] and tr <= -p["burst_rate_mps"]):
                        st.add_event(fid, "burst", "Possible balloon burst", auto=True,
                                     at=dt.datetime.fromtimestamp(m["max_alt_t"], UTC),
                                     detail=f"peak {m['max_alt_m']:,.0f} m, now {fl[-1].alt:,.0f} m descending "
                                            f"{tr:.1f} m/s", dedupe="burst")
                        eng["burst_logged"] = True
                # altitude milestones (above sea level), once each
                top = a["stats"]["max_alt_m"]
                for m in p["milestones_m"]:
                    if top is not None and top >= m:
                        first = next((x for x in fl if x.alt is not None and x.alt >= m), None)
                        if first:
                            st.add_event(fid, "milestone", f"Passed {m:,} m ({m * 3.28084:,.0f} ft)", auto=True,
                                         at=dt.datetime.fromtimestamp(first.t, UTC), dedupe=f"milestone:{m}")
        eng["phase_confidence"] = a["phase_confidence"]
        eng["evaluated_utc"] = iso(now)
        st.set_engine(fid, eng)
        a["state"] = (f2 or f).get("state")
        return a


def _fmt_fix(row: dict[str, Any] | None) -> str:
    if not row:
        return "none"
    return f"{row['lat']:.5f}, {row['lon']:.5f}, {row.get('alt_m')} m at {row['rx_utc']}"


# ------------------------------------------------------------------ operator actions
def arm(store: Store, fid: int, by: str, reason: str = "armed by operator") -> dict[str, Any]:
    f = store.transition(fid, "ARMED", reason, auto=False, by=by)
    store.add_event(fid, "armed", "Mission armed: telemetry recording", auto=False, by=by, detail=reason)
    return f


def launch_now(store: Store, fid: int, by: str, reason: str = "launched by operator",
               now: dt.datetime | None = None) -> dict[str, Any]:
    """Manual launch: T+0 now, at the latest valid fix (if there is one; otherwise the launch point stays unknown)."""
    now = now or _store.now()
    f = store.flight(fid)
    if not f or f.get("state") != "ARMED":
        raise ValueError("only an ARMED mission can be launched")
    last = store.one("SELECT * FROM positions WHERE node = ? AND valid = 1 AND rx_utc <= ? ORDER BY rx_utc DESC "
                     "LIMIT 1", (f["tracker_node"], iso(now)))
    fields = {"launch_utc": iso(now), "launch_method": "manual"}
    if last and (now - parse_t(last["rx_utc"])).total_seconds() < 1800:
        fields.update(launch_lat=last["lat"], launch_lon=last["lon"], launch_alt_m=last["alt_m"])
    f = store.transition(fid, "ASCENT", reason, auto=False, by=by, at=now, **fields)
    store.add_event(fid, "launch", "Launch (manual, T+00:00:00)", auto=False, by=by, at=now,
                    detail=reason + ("; launch point: last fix " + _fmt_fix(last) if "launch_lat" in fields
                                     else "; no recent fix: launch point unknown"), dedupe="launch")
    return f


def confirm_landing(store: Store, fid: int, by: str, note: str = "") -> dict[str, Any]:
    f = store.flight(fid)
    if not f or f.get("state") != "LANDED":
        raise ValueError("only a mission whose landing is suspected (LANDED) can have it confirmed")
    store.add_event(fid, "landing_confirmed", "Landing confirmed", auto=False, by=by,
                    detail=note or f"at {f.get('landed_lat')}, {f.get('landed_lon')}")
    return complete_with_summary(store, fid, by, "landing confirmed" + (f": {note}" if note else ""))


def complete(store: Store, fid: int, by: str, reason: str) -> dict[str, Any]:
    f = store.transition(fid, "COMPLETE", reason, auto=False, by=by)
    store.add_event(fid, "completed", "Mission completed", auto=False, by=by, detail=reason)
    return f


def note(store: Store, fid: int, by: str, text: str, at: dt.datetime | None = None) -> int:
    text = (text or "").strip()
    if not text or len(text) > 2000:
        raise ValueError("a note is 1-2000 characters")
    if not store.flight(fid):
        raise ValueError("no such mission")
    return store.add_event(fid, "note", "Operator note", auto=False, by=by, detail=text, at=at)


# ------------------------------------------------------------------ archive
def summary(store: Store, fid: int, now: dt.datetime | None = None) -> dict[str, Any]:
    """The mission archive: statistics, timings, the timeline, telemetry and reception summaries, the last
    prediction against where it actually came down. Built from stored data only."""
    f = store.flight(fid)
    if not f:
        raise ValueError("no such mission")
    a = Engine(store).analyze(f, now)
    node = f["tracker_node"]
    lo = f.get("armed_utc") or f.get("started_utc") or f["created_utc"]
    hi = f.get("completed_utc") or f.get("ended_utc") or iso(now or _store.now())
    tel = store.one("SELECT COUNT(*) AS n, MIN(NULLIF(battery_level, 101)) AS batt_min, MAX(NULLIF(battery_level, "
                    "101)) AS batt_max, MIN(voltage) AS v_min, MAX(voltage) AS v_max, MIN(temperature) AS temp_min, "
                    "MAX(temperature) AS temp_max FROM telemetry WHERE node = ? AND rx_utc >= ? AND rx_utc <= ?",
                    (node, lo, hi)) or {}
    rx = store.q("SELECT r.eui, g.name, COUNT(*) AS receptions, MAX(r.rssi) AS best_rssi, AVG(r.rssi) AS mean_rssi, "
                 "MAX(r.snr) AS best_snr, MIN(r.rx_utc) AS first, MAX(r.rx_utc) AS last FROM rx r LEFT JOIN gateways g "
                 "ON g.eui = r.eui WHERE r.sender = ? AND r.rx_utc >= ? AND r.rx_utc <= ? GROUP BY r.eui",
                 (node, lo, hi))
    counts = store.counts(node, lo, hi)
    preds = store.predictions(fid, 50)
    last_pred = next((p for p in preds if p["status"] == "ok" and (p["result"] or {}).get("landing")), None)
    pred_err = None
    if last_pred and f.get("landed_lat") is not None:
        L = last_pred["result"]["landing"]
        pred_err = {"prediction_id": last_pred["id"], "made_utc": last_pred["created_utc"],
                    "miss_m": geo.distance_m(L["lat"], L["lon"], f["landed_lat"], f["landed_lon"])}
    t = {k: f.get(k) for k in ("created_utc", "armed_utc", "launch_utc", "landed_utc", "completed_utc")}

    def span(x: str | None, y: str | None) -> float | None:
        return (parse_t(y) - parse_t(x)).total_seconds() if x and y else None
    out = {"mission": {k: f.get(k) for k in ("id", "name", "mode", "balloon_type", "state", "notes",
                                             "launch_method", "launch_lat", "launch_lon", "launch_alt_m",
                                             "landed_lat", "landed_lon", "landed_alt_m")},
           "tracker": store.tracker(node), "times": t,
           "durations_s": {"armed_to_launch": span(t["armed_utc"], t["launch_utc"]),
                           "flight": span(t["launch_utc"], t["landed_utc"] or t["completed_utc"]),
                           "recording": span(t["armed_utc"], t["completed_utc"])},
           "stats": a["stats"], "observed_interval_s": a["observed_interval_s"], "fixes": a["fixes"],
           "spikes_dropped": len(a["spikes"]),
           "rejected_fixes": (store.one("SELECT COUNT(*) AS n FROM positions WHERE node = ? AND valid = 0 AND rx_utc >= ? "
                                        "AND rx_utc <= ?", (node, lo, hi)) or {}).get("n", 0),
           "packets": counts, "telemetry": tel, "receptions_by_station": rx,
           "events": store.events(fid), "prediction_vs_landing": pred_err, "generated_utc": iso(now or
                                                                                               _store.now())}
    return out


def complete_with_summary(store: Store, fid: int, by: str, reason: str) -> dict[str, Any]:
    f = complete(store, fid, by, reason)
    store.set_engine(fid, f.get("engine") or {}, summary=json_dumps(summary(store, fid)))
    return store.flight(fid) or {}


def json_dumps(v: Any) -> str:
    import json
    return json.dumps(v, default=str, separators=(",", ":"))
