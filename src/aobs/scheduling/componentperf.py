"""Component Performance Intelligence: what the test history says about one antenna, filter, amplifier, ...

Sources: every test's immutable signal-path snapshot (so renames/moves/removals never rewrite history),
before/after pairs (a test and its baseline, where this part is in exactly one of the two paths), filter
tests, and the layout's version history (where the part has been connected).

A pair is "comparable" only when it uses the same receiver, the same frequency (or scan range), the same
gain settings, and was recorded within two hours. Observations state how many comparable pairs support
them; a conclusion ("consistently better/worse") needs at least three comparable pairs that all agree by
3 dB or more. Nothing here recommends installing or removing hardware.
Manufacturer specifications entered on the part are shown separately and are never treated as measurements.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

from sqlalchemy import select

from ..db.schema import rf_layouts

MAX_PAIR_GAP_S = 7200
AGREE_DB = 3.0
MIN_PAIRS = 3


def _t(v: Any) -> dt.datetime | None:
    return dt.datetime.fromisoformat(v) if v else None


def _parts(t: dict[str, Any]) -> list[str]:
    return list(t.get("parts") or [])


def _same_gain(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a.get("gain") == b.get("gain")


def pair_check(base: dict[str, Any], test: dict[str, Any]) -> tuple[bool, list[str]]:
    why = []
    if base["receiver_id"] != test["receiver_id"]:
        why.append("different receivers")
    if base["test_type"] != test["test_type"]:
        why.append("different test types")
    if test["test_type"] == "scan":
        if (base.get("frequency_hz"), base.get("stop_hz")) != (test.get("frequency_hz"), test.get("stop_hz")):
            why.append("different scan ranges")
    elif abs((base.get("frequency_hz") or 0) - (test.get("frequency_hz") or 0)) > 500_000:
        why.append("different frequencies")
    if not _same_gain(base, test):
        why.append("different gain")
    ta, tb = _t(base.get("record_start")), _t(test.get("record_start"))
    if ta and tb and abs((tb - ta).total_seconds()) > MAX_PAIR_GAP_S:
        why.append(f"{abs((tb - ta).total_seconds()) / 3600:.1f} h apart")
    return not why, why


def _delta(with_: dict[str, Any], without: dict[str, Any], key: str) -> float | None:
    a, b = with_.get(key), without.get(key)
    return None if a is None or b is None else round(a - b, 2)


def performance(lab: Any, cid: str) -> dict[str, Any]:
    s = lab.s
    layout = (s.rf.current or {}).get("layout") or {} if getattr(s, "rf", None) else {}
    comp = (layout.get("components") or {}).get(cid)
    tests = lab.list(limit=2000)
    byid = {t["test_id"]: t for t in tests}
    mine = [t for t in tests if cid in _parts(t) or t.get("component_id") == cid]
    if comp is None and not mine:
        from .store import SchedulerError
        raise SchedulerError(f"no part {cid} in the layout or in any test", 404)
    name = (comp or {}).get("name") or next((t["part_names"][cid] for t in mine if cid in (t.get("part_names") or {})), cid)
    kind = (comp or {}).get("kind")
    # ---- where it is, where it has been
    where = None
    for rid, r in (layout.get("receivers") or {}).items():
        if cid in (r.get("chain") or []):
            where = {"receiver": rid, "position": r["chain"].index(cid)}
    for c in (layout.get("components") or {}).values():
        for port, aid in (c.get("ports") or {}).items():
            if aid == cid:
                where = {"switch": c["id"], "port": port}
    history = _path_history(s, cid)
    # ---- pairs where the part was added or removed
    pairs = []
    for t in tests:
        b = byid.get(t.get("baseline_test_id") or "")
        if not b or t["test_type"] in ("filter_test", "sat_compare"):
            continue
        in_t, in_b = cid in _parts(t), cid in _parts(b)
        if in_t == in_b:
            continue
        with_, without = (t, b) if in_t else (b, t)
        ok, why = pair_check(b, t)
        p = {"with": with_["test_id"], "without": without["test_id"], "type": t["test_type"], "receiver": t["receiver_id"],
             "frequency_hz": t.get("frequency_hz"), "stop_hz": t.get("stop_hz"), "comparable": ok, "not_comparable_because": why,
             "change": "added" if in_t else "removed", "recorded": t.get("record_start")}
        if t["test_type"] == "scan":
            try:
                c = lab.compare_scans(without["test_id"], with_["test_id"])
                p["regions"] = [{k: r.get(k) for k in ("name", "start_hz", "stop_hz", "role", "median_change_db", "measurable_bins")}
                                for r in c["regions"]]
                p["outside_median_change_db"] = c["outside_regions"]["median_change_db"]
            except Exception as exc:  # noqa: BLE001 - one unreadable scan must not hide the rest
                p["error"] = str(exc)
        else:
            p["deltas"] = {k: _delta(with_, without, k) for k in ("snr_db", "noise_floor_dbfs_per_hz", "channel_power_dbfs",
                                                                  "strong_peaks")}
            cw, co = with_.get("clipped_fraction"), without.get("clipped_fraction")
            p["deltas"]["clipping_percent"] = None if cw is None or co is None else round((cw - co) * 100, 4)
        pairs.append(p)
    filter_tests = [t for t in tests if t["test_type"] == "filter_test" and t.get("component_id") == cid]
    sat = [t for t in mine if t["test_type"] == "satellite"]
    sat_results = []
    for t in sat[:20]:
        try:
            d = lab.detail(t["test_id"])
            sat_results.append({"test_id": t["test_id"], "receiver": t["receiver_id"], "recorded": t.get("record_start"),
                                "highest": ((d.get("satellite") or {}).get("ladder") or {}).get("highest"),
                                "frames": max([x.get("cadu_frames") or 0 for x in (d.get("satellite") or {}).get("decodes", [])] or [0])})
        except Exception:  # noqa: BLE001
            continue
    anomalies = []
    if kind == "amplifier":
        for t in [x for x in mine if x["test_type"] in ("interference", "quick")][:20]:
            try:
                inv = ((lab.detail(t["test_id"]).get("metrics") or {}).get("investigation") or {})
            except Exception:  # noqa: BLE001
                continue
            for a in inv.get("anomalies", []):
                if a["kind"] in ("clipping", "analog_overload"):
                    anomalies.append({"test_id": t["test_id"], "title": a["title"], "evidence": a["evidence"],
                                      "confidence": a["confidence"]})
    freqs = sorted({round((t.get("frequency_hz") or 0) / 1e6, 3) for t in mine
                    if t["test_type"] not in ("scan", "filter_test", "sat_compare") and t.get("frequency_hz")})
    ranges = sorted({(round(t["frequency_hz"] / 1e6, 1), round(t["stop_hz"] / 1e6, 1)) for t in mine if t["test_type"] == "scan" and t.get("stop_hz")})
    spec = None
    if comp and (comp.get("min_mhz") or comp.get("max_mhz") or comp.get("gain_db") is not None):
        spec = {"min_mhz": comp.get("min_mhz"), "max_mhz": comp.get("max_mhz"), "gain_db": comp.get("gain_db"),
                "note": "entered on the part (manufacturer or owner figures); not measured"}
    by_type: dict[str, int] = {}
    for t in mine:
        by_type[t["test_type"]] = by_type.get(t["test_type"], 0) + 1
    out = {"component": {"id": cid, "name": name, "kind": kind, "model": (comp or {}).get("model"),
                         "notes": (comp or {}).get("notes"), "status": (comp or {}).get("status")},
           "in_layout": comp is not None, "location": where, "path_history": history,
           "tests": len(mine), "tests_by_type": by_type, "receivers": sorted({t["receiver_id"] for t in mine}),
           "frequencies_mhz": freqs, "scan_ranges_mhz": ranges,
           "recent": [{k: t.get(k) for k in ("test_id", "test_type", "name", "receiver_id", "record_start", "frequency_hz",
                                              "stop_hz", "snr_db", "noise_floor_dbfs_per_hz", "status")} for t in mine[:12]],
           "pairs": pairs, "filter_tests": [{"test_id": t["test_id"], "recorded": t["created_at"], **(t.get("summary") or {})}
                                            for t in filter_tests],
           "satellite": sat_results, "overload_observations": anomalies, "specifications": spec}
    out["observations"] = observations(out, kind)
    return out


def _path_history(s: Any, cid: str) -> list[dict[str, Any]]:
    with s.engine.connect() as conn:
        rows = conn.execute(select(rf_layouts.c.version, rf_layouts.c.layout, rf_layouts.c.created_at)
                            .order_by(rf_layouts.c.version)).mappings().all()
    spans: list[dict[str, Any]] = []
    for r in rows:
        lay = r["layout"] or {}
        comps = lay.get("components") or {}
        here = None
        for rid, rx in (lay.get("receivers") or {}).items():
            if cid in (rx.get("chain") or []):
                here = (rid, " > ".join(comps.get(c, {}).get("name", c) for c in rx["chain"]) + f" > {rid}")
        for c in comps.values():
            for port, aid in (c.get("ports") or {}).items():
                if aid == cid:
                    here = (f"{c['id']} port {port}", f"{comps.get(cid, {}).get('name', cid)} on {c['id']} port {port}")
        last = spans[-1] if spans else None
        if here and last and last["where"] == here[0] and last["path"] == here[1] and last["until_version"] == r["version"] - 1:
            last["until_version"] = r["version"]
            continue
        if here:
            spans.append({"where": here[0], "path": here[1], "since": r["created_at"].isoformat(),
                          "since_version": r["version"], "until_version": r["version"]})
    return spans


def observations(p: dict[str, Any], kind: str | None) -> list[dict[str, Any]]:
    obs = []

    def add(text: str, evidence: str, confidence: str, comparable: int, differences: list[str] | None = None) -> None:
        obs.append({"text": text, "evidence": evidence, "confidence": confidence, "comparable_tests": comparable,
                    "differences": differences or []})
    pairs = p["pairs"]
    comp = [x for x in pairs if x["comparable"]]
    noncomp = [x for x in pairs if not x["comparable"]]
    diffs = sorted({w for x in noncomp for w in x["not_comparable_because"]})
    if not p["tests"]:
        add("No test has included this part yet.", "test history", "n/a", 0)
        return obs
    if kind == "filter" or p["filter_tests"]:
        ft = [f for f in p["filter_tests"] if f.get("stopband_median_attenuation_db") is not None]
        if ft:
            att = [f["stopband_median_attenuation_db"] for f in ft]
            add(f"{len(ft)} filter test(s) measured a median stopband attenuation of "
                f"{', '.join(f'{a:.1f}' for a in att)} dB (over-the-air comparisons, not insertion loss).",
                "filter tests", _conf(len(ft), all(a >= AGREE_DB for a in att)), len(ft),
                sorted({r for f in ft for r in f.get("confidence_reasons") or []}))
        sp = [x for x in comp if x["type"] == "scan" and x.get("regions")]
        if sp:
            reduced = [x for x in sp if any((r.get("median_change_db") or 0) <= -AGREE_DB and r.get("role", "stop") == "stop"
                                            for r in x["regions"])]
            add(f"In {len(reduced)} of {len(sp)} comparable before/after scan pair(s), power in the marked stop region was "
                f"{AGREE_DB:.0f} dB or more lower with the part in place.", "wideband scan pairs",
                _conf(len(sp), len(reduced) == len(sp)), len(sp), diffs)
    rx = [x for x in comp if x["type"] in ("quick", "interference", "satellite") and (x.get("deltas") or {}).get("snr_db") is not None]
    if rx:
        d = [x["deltas"]["snr_db"] for x in rx]
        if len(rx) >= MIN_PAIRS and all(v >= AGREE_DB for v in d):
            txt = f"In all {len(rx)} comparable pairs the target's SNR was higher with the part (by {min(d):.1f}–{max(d):.1f} dB)."
        elif len(rx) >= MIN_PAIRS and all(v <= -AGREE_DB for v in d):
            txt = f"In all {len(rx)} comparable pairs the target's SNR was lower with the part (by {-max(d):.1f}–{-min(d):.1f} dB)."
        else:
            txt = (f"No consistent change in reception has been established yet: SNR changed by "
                   f"{', '.join(f'{v:+.1f}' for v in d)} dB with the part in {len(rx)} comparable pair(s).")
        add(txt, "before/after signal tests", _conf(len(rx), len(rx) >= MIN_PAIRS and (all(v >= AGREE_DB for v in d) or
                                                                                         all(v <= -AGREE_DB for v in d))),
            len(rx), diffs)
    if kind == "amplifier":
        fl = [x["deltas"]["noise_floor_dbfs_per_hz"] for x in comp if (x.get("deltas") or {}).get("noise_floor_dbfs_per_hz") is not None]
        if fl:
            add(f"With the amplifier the measured noise floor changed by {', '.join(f'{v:+.1f}' for v in fl)} dB "
                f"({len(fl)} comparable pair(s)). A higher floor with an LNA is expected (it amplifies noise too); it does "
                "not measure the amplifier's noise figure.", "before/after signal tests", _conf(len(fl), True), len(fl), diffs)
        if p["overload_observations"]:
            add(f"{len(p['overload_observations'])} clipping / possible-overload finding(s) in tests with this amplifier "
                "in the path. Overload is only ever 'possible' from these indicators.", "interference investigations",
                "low", 0)
    if kind == "antenna" and p["satellite"]:
        ok = [x for x in p["satellite"] if (x.get("frames") or 0) > 0]
        add(f"{len(ok)} of {len(p['satellite'])} satellite quality test(s) through this antenna decoded valid frames.",
            "satellite quality tests", "low" if len(p["satellite"]) < MIN_PAIRS else "medium", len(p["satellite"]),
            ["different passes have different elevations, so these are not like-for-like"])
    if not comp:
        add("There are no comparable before/after pairs for this part yet" + (
            f" ({len(noncomp)} pair(s) differ in: {', '.join(diffs)})" if noncomp else "") + ". Record a baseline, change "
            "only this part, and test again with the same settings.", "pairs", "n/a", 0, diffs)
    elif len(comp) < MIN_PAIRS:
        add(f"Only {len(comp)} comparable pair(s) so far; at least {MIN_PAIRS} that agree are needed before calling any "
            "effect consistent. Nothing here supports installing or removing this part yet.", "pairs", "low", len(comp), diffs)
    return obs


def _conf(n: int, consistent: bool) -> str:
    if n >= 6 and consistent:
        return "high"
    if n >= MIN_PAIRS and consistent:
        return "medium"
    return "low"


def compare_antennas(lab: Any, a: str, b: str) -> dict[str, Any]:
    """Tests through antenna A vs antenna B, grouped by frequency, with the confounders spelled out."""
    tests = [t for t in lab.list(limit=2000) if t["test_type"] in ("quick", "interference", "satellite")]
    ta = [t for t in tests if a in _parts(t)]
    tb = [t for t in tests if b in _parts(t)]
    bands: dict[float, dict[str, Any]] = {}
    for key, lst in (("a", ta), ("b", tb)):
        for t in lst:
            f = round((t.get("frequency_hz") or 0) / 1e6, 0)
            g = bands.setdefault(f, {"frequency_mhz": f, "a": [], "b": []})
            g[key].append({k: t.get(k) for k in ("test_id", "receiver_id", "record_start", "snr_db", "noise_floor_dbfs_per_hz",
                                                  "gain", "test_type")})
    rows = []
    for f, g in sorted(bands.items()):
        if not g["a"] or not g["b"]:
            continue
        conf = set()
        if {x["receiver_id"] for x in g["a"]} != {x["receiver_id"] for x in g["b"]}:
            conf.add("different receivers")
        if {str(x["gain"]) for x in g["a"]} != {str(x["gain"]) for x in g["b"]}:
            conf.add("different gain settings")
        if any(x["test_type"] == "satellite" for x in g["a"] + g["b"]):
            conf.add("different satellite passes (elevation, Doppler)")
        sa = [x["snr_db"] for x in g["a"] if x["snr_db"] is not None]
        sb = [x["snr_db"] for x in g["b"] if x["snr_db"] is not None]
        rows.append({**g, "median_snr_a": statistics.median(sa) if sa else None, "median_snr_b": statistics.median(sb) if sb else None,
                     "confounders": sorted(conf)})
    return {"a": a, "b": b, "bands": rows, "only_a": len(ta), "only_b": len(tb),
            "note": "Antennas tested at different times, on different receivers or with different gain are confounded; "
                    "the medians are context, not a ranking."}


def dashboard(lab: Any) -> dict[str, Any]:
    tests = lab.list(limit=2000)
    byid = {t["test_id"]: t for t in tests}
    baselines = sorted({t["baseline_test_id"] for t in tests if t.get("baseline_test_id")})
    pairs = [{"a": t["baseline_test_id"], "b": t["test_id"], "type": t["test_type"], "receiver": t["receiver_id"],
              "name": t["name"], "recorded": t["record_start"]} for t in tests if t.get("baseline_test_id") in byid
             and t["test_type"] not in ("filter_test",)][:15]
    anomalies = []
    for t in [x for x in tests if x["test_type"] == "interference" and x["status"] == "complete"][:6]:
        try:
            inv = ((lab.detail(t["test_id"]).get("metrics") or {}).get("investigation") or {})
        except Exception:  # noqa: BLE001
            continue
        for a in inv.get("anomalies", []):
            if a["severity"] in ("high", "medium"):
                anomalies.append({"test_id": t["test_id"], "receiver": t["receiver_id"], "recorded": t["record_start"],
                                  "title": a["title"], "severity": a["severity"], "frequency_hz": a.get("frequency_hz")})
    sats = []
    for t in [x for x in tests if x["test_type"] in ("satellite", "sat_compare")][:8]:
        try:
            d = lab.detail(t["test_id"])
        except Exception:  # noqa: BLE001
            continue
        if t["test_type"] == "satellite":
            sd = d.get("satellite") or {}
            sats.append({"test_id": t["test_id"], "type": "satellite", "receiver": t["receiver_id"], "recorded": t["record_start"],
                         "highest": (sd.get("ladder") or {}).get("highest"),
                         "frames": max([x.get("cadu_frames") or 0 for x in sd.get("decodes", [])] or [0])})
        else:
            sats.append({"test_id": t["test_id"], "type": "sat_compare", "recorded": t["record_start"], "name": t["name"]})
    counts: dict[str, int] = {}
    for t in tests:
        for c in set(_parts(t)) | ({t["component_id"]} if t.get("component_id") else set()):
            counts[c] = counts.get(c, 0) + 1
    layout = ((lab.s.rf.current or {}).get("layout") or {}) if getattr(lab.s, "rf", None) else {}
    comps = layout.get("components") or {}
    parts = sorted(({"id": c, "name": comps.get(c, {}).get("name", c), "kind": comps.get(c, {}).get("kind"), "tests": n}
                    for c, n in counts.items()), key=lambda x: -x["tests"])
    return {"recent": tests[:10], "baselines": [byid[b] for b in baselines if b in byid][:10], "comparisons": pairs,
            "anomalies": anomalies[:20], "satellite": sats, "components": parts[:30], "total_tests": len(tests)}
