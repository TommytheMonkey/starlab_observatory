"""The Scheduler page's data: calendar and history listings, the recording form, arbitrary recordings.

Times are UTC everywhere in the database and the API; the page shows them in the observatory's display
time zone (Settings → Observatory). The recording form takes local wall-clock times plus that zone, and
the normal job validator turns them into UTC.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from ..satellites import predict
from .hardware import BYTES_PER_SAMPLE, apply_gain_profile, default_radio, gain_profile_problems
from .rules import gain_profiles, owned_elsewhere
from .store import EDITABLE, LIFECYCLE_STATES, SchedulerError, statuses_for

FORM_KEYS = ("name", "norad", "receiver_id", "receiver_ids", "antenna", "start", "end", "timezone", "frequency_hz", "sample_rate_hz",
             "gain_profile", "gain_mode", "gain_db", "gain_elements", "format", "decode", "pipeline", "pre_roll_s",
             "post_roll_s", "notes", "allow_duplicate", "pass", "downlink")
# A pass booked from the prediction starts recording this long before AOS (0°), so the first samples cover the
# horizon even if the elements or the clock are a little off. pre_roll_s only holds the receiver; it records nothing.
PRE_AOS_S = 60


def _doc(s: Any, name: str) -> dict[str, Any]:
    return ((s.config_store.current(name) or {}).get("data")) or {}


def display_tz(s: Any) -> str:
    return _doc(s, "observatory").get("timezone") or getattr(s.config, "display_tz", "America/New_York") or "America/New_York"


def overview(s: Any, svc: Any = None) -> dict[str, Any]:
    """Everything the Scheduler page needs once: mode, receivers, satellites, decoders, last planning cycle."""
    sch = _doc(s, "scheduling")
    rx_out = []
    for rx in s.receivers.values():
        rx_out.append({"id": rx.id, "driver": rx.driver, "formats": list(rx.formats()), "schedulable": rx.schedulable,
                       "readiness": rx.readiness, "simulated": rx.simulated, "switched": rx.switched,
                       "owned_elsewhere": owned_elsewhere(rx), "mode": rx.mode,
                       "gain_profiles": [{k: p.get(k) for k in ("name", "gain_mode", "gain_db", "gain_elements")}
                                         for p in gain_profiles(s, rx.id)],
                       "antennas": [p["antenna"] for p in s.switch_ports.values()] if rx.switched else []})
    from ..satellites.passes import PassService
    sats = PassService(s).satellites()
    return {"mode": sch.get("mode") or "manual", "horizon_hours": sch.get("horizon_hours") or 48,
            "timezone": display_tz(s), "now": s.clock().isoformat(), "receivers": rx_out,
            "catalog": sats["catalog"], "elements": sats["elements"], "refresh_online": sats["refresh_online"],
            "last_refresh": sats.get("last_refresh"), "pipelines": sorted(s.pipelines),
            "catalog_pipelines": {str(n): c["pipeline"] for n, c in predict.CATALOG.items() if c.get("pipeline")},
            "states": list(LIFECYCLE_STATES), "last_reconcile": getattr(getattr(svc, "rules", None), "last", None) or {},
            "location_set": PassService(s).location() is not None}


def _brief(j: dict[str, Any], rules: dict[str, str]) -> dict[str, Any]:
    spec = j.get("spec") or {}
    m = spec.get("metadata") or {}
    t = spec.get("target") or {}
    radio = spec.get("radio") or {}
    out = {k: j.get(k) for k in ("job_id", "job_name", "source", "receiver_id", "status", "status_reason", "lifecycle",
                                  "executes", "record_start", "record_end", "reservation_start", "reservation_end",
                                  "target", "rule_id", "override", "requested_by", "estimated_bytes", "preempted_by")}
    out.update(rule_name=rules.get(j.get("rule_id") or "", m.get("rule_name")), norad=m.get("norad") or t.get("norad_id"),
               max_elevation_deg=m.get("max_elevation_deg"), aos=m.get("aos"), los=m.get("los"),
               aos_azimuth_deg=m.get("aos_azimuth_deg"), los_azimuth_deg=m.get("los_azimuth_deg"),
               frequency_hz=radio.get("target_frequency_hz") or radio.get("center_frequency_hz"),
               sample_rate_hz=radio.get("sample_rate_hz"), format=(spec.get("recording") or {}).get("format"),
               decode=bool((spec.get("satdump") or {}).get("enabled")), simulated=bool(m.get("simulated")),
               warnings=m.get("warnings") or [], editable=j.get("status") in EDITABLE and j.get("source") != "legacy"
               and (bool(j.get("executes")) or bool(j.get("rule_id"))))
    res = j.get("result") or {}
    cap = res.get("capture") or {}
    if cap:
        out["validation"] = cap.get("validation")
        out["bytes"] = cap.get("bytes")
    return out


def calendar(s: Any, start: dt.datetime, end: dt.datetime, satellite: str | None = None, receiver: str | None = None,
             states: list[str] | None = None, limit: int = 1000) -> dict[str, Any]:
    status = statuses_for(states) if states else None
    rows = s.jobs(receiver=receiver or None, status=status, start=start, end=end, target=satellite or None,
                  limit=min(limit, 2000))
    if satellite and satellite.isdigit():           # a NORAD number matches the job's satellite, not its name
        rows = s.jobs(receiver=receiver or None, status=status, start=start, end=end, limit=min(limit, 2000))
        rows = [r for r in rows if str(((r.get("spec") or {}).get("metadata") or {}).get("norad")
                                       or ((r.get("spec") or {}).get("target") or {}).get("norad_id") or "") == satellite]
    names = {r["rule_id"]: r["name"] for r in s.rules.list(include_deleted=True)}
    return {"start": start.isoformat(), "end": end.isoformat(), "timezone": display_tz(s),
            "jobs": [_brief(r, names) for r in rows]}


def job_detail(s: Any, job_id: str) -> dict[str, Any]:
    j = s.get(job_id)
    if j is None:
        raise SchedulerError(f"no job {job_id}", 404)
    names = {r["rule_id"]: r["name"] for r in s.rules.list(include_deleted=True)}
    return {**_brief(j, names), "events": j.get("events", []), "spec": j.get("spec"), "paths": j.get("paths"),
            "result": j.get("result"), "execution": execution_view(s, j)}


def execution_view(s: Any, j: dict[str, Any]) -> dict[str, Any] | None:
    """Start checks, timestamped diagnostics, where it ran and the recorder log tail (brief 7)."""
    ex = j.get("execution")
    if not ex:
        return None
    from .diagnose import log_tail
    log = log_tail(str((j.get("paths") or {}).get("log") or ""), 3000) if j["status"] not in ("scheduled",) else ""
    handle = ex.get("handle") or {}
    return {"backend": ex.get("backend"), "host": handle.get("host"), "pid": handle.get("pid"),
            "checks": ex.get("checks") or [], "diagnostics": ex.get("diagnostics") or [], "log_tail": log}


# ------------------------------------------------------------------ the recording form
def form_for_pass(s: Any, norad: int, aos: str, receiver_id: str | None, downlink: str | None = None) -> dict[str, Any]:
    """A recording form prefilled from a predicted pass on one of its downlinks (default: the first active one);
    every field can be changed before booking. The suggested receiver is the one whose antenna covers that
    downlink's frequency (rxsuggest)."""
    from ..satellites.passes import PassService
    found = PassService(s).predicted(72)
    want = dt.datetime.fromisoformat(aos)
    p = next((x for x in found["passes"] if x["norad"] == int(norad) and (not downlink or x.get("downlink") == downlink)
              and abs((dt.datetime.fromisoformat(x["aos"]) - want).total_seconds()) < 90), None)
    if p is None:
        raise SchedulerError("that pass (or that downlink) is not in the current prediction (it may have started "
                             "already, or the downlink is off in Settings → Satellites); reload", 409)
    cat = predict.CATALOG.get(int(norad)) or {}
    group = predict.downlink_group(int(norad), p.get("downlink")) or [] if cat else []
    dl = group[0] if group else {}                  # the lowest: the capture is tuned rate/8 below it
    freq, bw = dl.get("hz"), dl.get("bandwidth_hz")
    pipes = [d["pipeline"] for d in group if d.get("pipeline")]
    several = len(cat.get("downlinks") or []) > 1
    from .rxsuggest import antenna_fit, suggest
    start = dt.datetime.fromisoformat(p["aos"]) - dt.timedelta(seconds=PRE_AOS_S)
    hint = suggest(s, freq, int(norad), start, dt.datetime.fromisoformat(p["los"]), 30, 30, p["name"])
    rid = receiver_id or (hint["receiver_ids"] or [None])[0] or \
        next((r.id for r in s.receivers.values() if r.schedulable and not owned_elsewhere(r)), next(iter(s.receivers), None))
    rx = s.receivers.get(rid) if rid else None
    antenna = hint["antenna"]
    if receiver_id:                                 # chosen by hand: a switched receiver still needs its port antenna
        layout = ((getattr(s, "rf", None) and s.rf.current) or {}).get("layout")
        fit = antenna_fit(layout, receiver_id, freq) if freq else None
        antenna = fit["id"] if fit and fit["switched"] else None
    label = dl["id"] if len(group) == 1 else f"{int(dl['hz'] // 1_000_000)}mhz-x{len(group)}" if group else ""
    form: dict[str, Any] = {"name": f"{re.sub(r'[^a-z0-9]+', '-', p['name'].lower()).strip('-')}-"
                                    f"{label + '-' if several and label else ''}"
                                    f"{dt.datetime.fromisoformat(p['aos']):%Y%m%d-%H%M}",
                            "downlink": predict.group_key(group) if group else None,
                            "downlink_mode": "; ".join(f"{d['hz'] / 1e6:.3f} MHz {d['mode']}" for d in group)
                            if len(group) > 1 else dl.get("mode"),
                            "downlinks_info": [{k: d[k] for k in ("id", "hz", "mode", "pipeline")} for d in group],
                            "norad": int(norad), "receiver_id": rid,
                            "receiver_ids": [rid] if receiver_id or not hint["receiver_ids"] else hint["receiver_ids"],
                            "antenna": antenna, "suggest": hint,
                            "start": start.isoformat(), "end": p["los"], "timezone": "UTC",
                            "frequency_hz": freq, "sample_rate_hz": None, "gain_profile": None,
                            # a downlink with a decoder decodes by default, like the automatic rules: METEOR LRPT
                            # (SatDump), ISS APRS (Direwolf) and SSTV (Starlab's own, light) are what it is recorded for
                            "format": None, "decode": bool(pipes), "pipeline": pipes[0] if pipes else None,
                            "pre_roll_s": 30, "post_roll_s": 30, "notes": "", "pass": {k: p.get(k) for k in (
                                "name", "aos", "los", "max_elevation_deg", "max_at", "aos_azimuth_deg",
                                "los_azimuth_deg", "duration_s", "elements_epoch", "elements_source")},
                            "booked": p.get("booked") or []}
    if rx is not None and freq:
        try:
            radio = default_radio(rx, int(freq), group_rate(rx, group), bandwidth_hint_hz=bw, antenna=antenna)
            form.update(sample_rate_hz=radio["sample_rate_hz"], format=rx.formats()[0], gain_mode=radio.get("gain_mode"),
                        gain_db=radio.get("gain_db"), gain_elements=radio.get("gain_elements"))
        except ValueError:
            pass
    return form


GROUP_RTL_RATE = 2_048_000      # one capture for several downlinks: room for all of them beside the DC offset


def group_rate(rx: Any, group: list[dict[str, Any]]) -> int | None:
    """The sample rate a capture of several downlinks starts from on this receiver (None: its usual default)."""
    return GROUP_RTL_RATE if len(group) > 1 and rx.gain_model == "rtl" else None


def group_fit(radio: dict[str, Any], group: list[dict[str, Any]]) -> list[str]:
    """Problems recording every downlink of the group in this radio block (empty: they all fit)."""
    from .sstvdec import CHANNEL_HALF_BW_HZ
    rate = int(radio["sample_rate_hz"])
    half = min(rate, int(radio.get("bandwidth_hz") or rate)) / 2 * 0.9
    out = []
    for d in group:
        off = d["hz"] - int(radio["center_frequency_hz"])
        if abs(off) + CHANNEL_HALF_BW_HZ > half:
            out.append(f"{d['hz'] / 1e6:.3f} MHz is {off / 1e3:+.0f} kHz from the center, outside the usable "
                       f"±{half / 1e3:.0f} kHz at {rate / 1e6:g} MS/s")
    return out


def job_from_form(s: Any, f: dict[str, Any]) -> dict[str, Any]:
    """The form as a normal job request; spec.check_job then validates it like every booking."""
    unknown = sorted(set(f) - set(FORM_KEYS) - {"booked", "receiver_ids", "suggest", "downlink_mode", "downlinks_info"})
    if unknown:
        raise SchedulerError(f"unknown form fields {', '.join(unknown)}", 400)
    rx = s.receivers.get(str(f.get("receiver_id") or ""))
    if rx is None:
        raise SchedulerError("choose a receiver", 400)
    try:
        freq = int(f.get("frequency_hz") or 0)
    except (TypeError, ValueError):
        raise SchedulerError("frequency must be a whole number of Hz", 400) from None
    if freq <= 0:
        raise SchedulerError("frequency is required", 400)
    norad = int(f["norad"]) if f.get("norad") else None
    cat = predict.CATALOG.get(norad or 0) or {}
    if norad and f.get("downlink"):
        group = predict.downlink_group(norad, str(f["downlink"]))
        if group is None:
            raise SchedulerError(f"{cat.get('name') or norad} has no downlink {f['downlink']!r}", 400)
    else:                                           # a form without one: the downlink its frequency falls in
        one = predict.downlink_for(norad, freq) if norad else None
        group = [one] if one else []
    rate = f.get("sample_rate_hz")
    try:
        radio = default_radio(rx, freq, int(rate) if rate else group_rate(rx, group),   # the signal width picks the rate
                              bandwidth_hint_hz=max((int(d.get("bandwidth_hz") or 0) for d in group), default=0) or None,
                              antenna=f.get("antenna") or None)
    except (TypeError, ValueError) as exc:
        raise SchedulerError(str(exc), 400) from None
    if len(group) > 1:                              # one capture for several downlinks: every one must be inside it
        bad = group_fit(radio, group)
        if bad:
            raise SchedulerError(f"these downlinks do not fit in one recording on {rx.id}: {'; '.join(bad)}. Choose a "
                                 "higher sample rate, or turn one off in Settings → Satellites", 400)
    if f.get("gain_profile"):
        prof = next((p for p in gain_profiles(s, rx.id) if str(p.get("name", "")).lower()
                     == str(f["gain_profile"]).lower()), None)
        if prof is None:
            raise SchedulerError(f"{rx.id} has no gain profile {f['gain_profile']!r}", 400)
        bad = gain_profile_problems(rx, prof)
        if bad:
            raise SchedulerError(f"gain profile {prof['name']!r}: {'; '.join(bad)}", 400)
        apply_gain_profile(rx, radio, prof)
    else:
        if f.get("gain_mode"):
            radio["gain_mode"] = f["gain_mode"]
        if "gain_db" in f and rx.driver == "rtlsdr":
            radio["gain_db"] = None if f.get("gain_mode") == "auto" else f.get("gain_db")
        if f.get("gain_elements") and rx.driver != "rtlsdr":
            radio["gain_elements"] = dict(f["gain_elements"])
    fmt = f.get("format") or rx.formats()[0]
    if fmt not in BYTES_PER_SAMPLE:
        raise SchedulerError(f"format must be one of {', '.join(BYTES_PER_SAMPLE)}", 400)
    name = str(f.get("name") or "").strip()
    target_name = (f.get("pass") or {}).get("name") or cat.get("name") or name
    root = s.settings.get("default_output_root", "/data/observatory/jobs")
    dl = group[0] if group else None
    # the decode the satdump block names: the form's choice, else the first downlink that has a decoder. Every other
    # decodable downlink of a group is decoded from the same IQ after recording (processing.submit_post_recording).
    first = next((d for d in group if d.get("pipeline")), None)
    pipeline = (f.get("pipeline") or (first or cat).get("pipeline")) if f.get("decode") else None
    decode_hz = first["hz"] if first and pipeline == first["pipeline"] else freq
    if f.get("decode") and not pipeline:
        raise SchedulerError("choose a SatDump pipeline to decode", 400)
    meta: dict[str, Any] = {"booked_from": "predicted pass" if f.get("pass") else "web form",
                            "notes": str(f.get("notes") or "")[:2000]}
    if f.get("pass"):
        p = f["pass"]
        meta.update(norad=norad, aos=p.get("aos"), los=p.get("los"), max_elevation_deg=p.get("max_elevation_deg"),
                    aos_azimuth_deg=p.get("aos_azimuth_deg"), los_azimuth_deg=p.get("los_azimuth_deg"),
                    passes=[{"name": target_name, "norad_id": norad, "rise": p.get("aos"), "set": p.get("los"),
                             "max_elevation_deg": p.get("max_elevation_deg")}])
    if dl is not None:
        meta["downlink"] = {"id": dl["id"], "hz": dl["hz"], "mode": dl["mode"]}
        meta["mode"] = (first or dl)["mode"]        # decoder profiles match on it (decoders.recording_identity)
    if len(group) > 1:
        meta["downlinks"] = [{k: d[k] for k in ("id", "hz", "mode", "pipeline")} for d in group]
    rxb: dict[str, Any] = {"receiver_id": rx.id, "driver": rx.driver, "serial": rx.serial, "exclusive_lock": True}
    if f.get("antenna"):
        rxb["antenna"] = f["antenna"]
    meta["title"] = name
    job_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")[:80]
    if len(job_name) < 3:                     # the job validator needs 3+ characters; never refuse a short title
        job_name = f"{job_name}-recording".strip("-")
    return {
        "job_name": job_name,
        "target": ({"type": "satellite", "name": target_name, "satellite": target_name, "norad_id": norad} if norad
                   else {"type": "frequency", "name": name or f"{freq / 1e6:.3f} MHz"}),
        "schedule": {"start": f.get("start"), "end": f.get("end"), "timezone": f.get("timezone") or display_tz(s),
                     "pre_roll_seconds": int(f.get("pre_roll_s") or 0), "post_roll_seconds": int(f.get("post_roll_s") or 0)},
        "receiver": rxb,
        "radio": radio,
        "recording": {"enabled": True, "engine": rx.engines()[0], "format": fmt,
                      "output_directory": f"{root}/{{job_name}}", "minimum_free_space_gb": 0},
        "satdump": ({"enabled": True, "operation": "decode_after_recording", "pipeline": pipeline,
                     "input_type": "baseband", "baseband_format": fmt, "sample_rate_hz": radio["sample_rate_hz"],
                     "frequency_hz": decode_hz, "output_directory": f"{root}/decoded/{{job_name}}"}
                    if pipeline else {"enabled": False, "operation": "none"}),
        "postprocess": {"generate_waterfall": True, "generate_spectrum": True, "decode": bool(pipeline),
                        "retain_raw_iq": True, "checksum": True, "record_metadata": True},
        "conflict_policy": {"on_receiver_conflict": "reject", "on_storage_warning": "reject",
                            "on_missing_device": "reject"},
        "metadata": meta,
    }


def duplicates(s: Any, job: dict[str, Any]) -> list[dict[str, Any]]:
    """Live jobs already recording the same satellite pass on any of the same downlinks (any receiver). The same
    pass on other downlinks (the ISS on 437 and 145 MHz at once) is not a duplicate."""
    from ..satellites.passes import job_downlinks
    norad = (job.get("target") or {}).get("norad_id")
    if not norad:
        return []
    mine = job_downlinks(job, int(norad))
    from . import spec as specmod
    checked = specmod.check_job(job, s.context())
    r = checked.job["resolved"]
    if not r.get("record_start"):
        return []
    a, b = dt.datetime.fromisoformat(r["record_start"]), dt.datetime.fromisoformat(r["record_end"])
    out = []
    for j in s.jobs(status=["scheduled", "preparing", "recording", "recorded", "processing", "completed"], start=a, end=b):
        spec = j.get("spec") or {}
        n = ((spec.get("metadata") or {}).get("norad") or (spec.get("target") or {}).get("norad_id"))
        norads = {int(x) for x in ((spec.get("target") or {}).get("norad_ids") or [])} | ({int(n)} if n else set())
        if int(norad) in norads and j["record_start"] < r["record_end"] and r["record_start"] < j["record_end"] \
                and (job_downlinks(spec, int(norad)) & mine or not mine):
            out.append({"job_id": j["job_id"], "receiver_id": j["receiver_id"], "status": j["status"],
                        "lifecycle": j.get("lifecycle")})
    return out


def form_for_receiver(s: Any, form: dict[str, Any], rid: str) -> dict[str, Any]:
    """One receiver's copy of a booking made for several: settings that only fit some hardware are dropped, so
    each receiver falls back to its own default (format, gain profile, manual RTL gain, switch antenna)."""
    rx = s.receivers.get(rid)
    if rx is None:
        raise SchedulerError(f"unknown receiver {rid}", 400)
    f = {k: v for k, v in form.items() if k != "receiver_ids"}
    f["receiver_id"] = rid
    if f.get("format") and f["format"] not in rx.formats():
        f.pop("format")
    if f.get("gain_profile") and not any(str(g.get("name", "")).lower() == str(f["gain_profile"]).lower()
                                         for g in gain_profiles(s, rid)):
        f.pop("gain_profile")
    if rx.driver != "rtlsdr":
        f.pop("gain_db", None)
        if f.get("gain_mode") == "manual":
            f.pop("gain_mode")
    else:
        f.pop("gain_elements", None)
    if not rx.switched:
        f.pop("antenna", None)
    return f


def book(s: Any, form: dict[str, Any], actor: str, dry_run: bool = False) -> dict[str, Any]:
    """Book a recording on one receiver, or the same recording on several (`receiver_ids`), all or nothing."""
    ids = [str(x) for x in (form.get("receiver_ids") or []) if str(x)]
    ids = list(dict.fromkeys(ids))
    multi = len(ids) > 1 or "receiver_ids" in form
    if not ids:
        if multi:
            raise SchedulerError("tick at least one receiver", 400)
        ids = [str(form.get("receiver_id") or "")]
    forms = [form_for_receiver(s, form, rid) for rid in ids] if multi else [form]
    jobs = [job_from_form(s, f) for f in forms]
    dup = duplicates(s, jobs[0])
    if dup and not form.get("allow_duplicate"):
        where = ", ".join(f"{d['receiver_id']} ({d['job_id']}, {d['lifecycle']})" for d in dup)
        raise SchedulerError(f"this pass is already being recorded: {where}. Tick “Record it again” to book "
                             "another recording of it anyway", 409)
    res = s.submit(jobs, "all_or_nothing", f"web:{actor}", dry_run=dry_run)
    failed = [(f["receiver_id"], r) for f, r in zip(forms, res["results"]) if not r.get("ok")]
    if failed:
        why = "; ".join(f"{rid}: " + "; ".join(r.get("errors") or ["refused"]) for rid, r in failed)
        raise SchedulerError(("Not booked" + (" on any receiver" if len(jobs) > 1 else "") + ": ") + why, 409)
    out = []
    for f, r in zip(forms, res["results"]):
        rj = r["job"]["resolved"]
        out.append({"receiver_id": f["receiver_id"], "job_id": r.get("job_id"), "warnings": r.get("warnings", []),
                    "preempts": r.get("preempts", []), "record_start": rj.get("record_start"),
                    "record_end": rj.get("record_end"), "estimated_bytes": rj.get("estimated_bytes")})
    first = out[0]
    return {"ok": True, "dry_run": dry_run, "job_id": first["job_id"], "jobs": out,
            "warnings": [f"{j['receiver_id']}: {w}" if len(out) > 1 else w for j in out for w in j["warnings"]],
            "preempts": [x for j in out for x in j["preempts"]], "duplicates": dup,
            "record_start": first["record_start"], "record_end": first["record_end"],
            "estimated_bytes": sum(j["estimated_bytes"] or 0 for j in out)}
