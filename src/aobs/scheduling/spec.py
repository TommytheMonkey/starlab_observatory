"""Parse and validate a /radio-schedule job into the normalized form the scheduler stores.

Validation never changes a supplied RF/SDR/recording/SatDump value. Optional,
non-RF fields that are absent get documented defaults, and every default that
was applied is listed in `resolved.defaults_applied` so nothing is implicit.
"""

from __future__ import annotations

import copy
import datetime as dt
import glob
import pathlib
import re
import zoneinfo
from dataclasses import dataclass, field
from typing import Any

from .hardware import BYTES_PER_SAMPLE, Receiver, validate_radio

JOB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$")
PLACEHOLDERS = {"target", "receiver", "start_utc", "center_hz", "sample_rate", "format", "job_name", "job_id"}
DEFAULT_TEMPLATE = "{target}_{receiver}_{start_utc}_{center_hz}_{sample_rate}.{format}"
SAFE_PARAM = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
RESERVED_SATDUMP = {"samplerate", "baseband_format", "freq_shift"}


@dataclass
class Checked:
    job: dict[str, Any]                      # normalized job (request values + resolved block)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class Context:
    now: dt.datetime
    receivers: dict[str, Receiver]
    switch_ports: dict[int, dict[str, Any]]
    satdump_pipelines: frozenset[str]
    settings: dict[str, Any]                 # [scheduler]


def satdump_pipeline_ids(directory: str) -> frozenset[str]:
    """Top-level pipeline ids. SatDump's JSON has comments, so read the keys with a regex."""
    ids: set[str] = set()
    for path in glob.glob(str(pathlib.Path(directory) / "*.json")):
        ids.update(re.findall(r'(?m)^    "([A-Za-z0-9_.+-]+)":\s*\{', pathlib.Path(path).read_text(errors="replace")))
    return frozenset(ids)


def _parse_time(value: Any, tz: str | None, label: str, errors: list[str]) -> dt.datetime | None:
    if not isinstance(value, str):
        errors.append(f"schedule.{label} must be an ISO-8601 time string")
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"schedule.{label} {value!r} is not ISO-8601")
        return None
    if t.tzinfo is None:
        if not tz:
            errors.append(f"schedule.{label} has no UTC offset and schedule.timezone is not set")
            return None
        t = t.replace(tzinfo=zoneinfo.ZoneInfo(tz))
    return t.astimezone(dt.timezone.utc)


def _under(path: str, roots: list[str]) -> bool:
    p = pathlib.PurePosixPath(path)
    return p.is_absolute() and ".." not in p.parts and any(p == pathlib.PurePosixPath(r) or
                                                             pathlib.PurePosixPath(r) in p.parents for r in roots)


def _slug(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-") or "x"


def _same_antenna(given: str, rx: Receiver) -> bool:
    """'QFH' names R01's 'QFH-137' / 'Diamond DP-KE137 QFH'; 'V-DIPOLE' does not."""
    g = given.strip().lower()
    names = [rx.antenna.lower(), rx.signal_path.lower().split(">")[0].strip()]
    return any(g == n or g in n or n in g for n in names if n)


def resolve_receiver(rx_block: dict[str, Any], receivers: dict[str, Receiver], errors: list[str]) -> Receiver | None:
    if rx_block.get("device_index") is not None:
        errors.append("receiver.device_index is not accepted; USB indexes change. Identify the SDR by receiver_id or serial")
    rid, serial = rx_block.get("receiver_id"), rx_block.get("serial")
    args_serial = (rx_block.get("device_args") or {}).get("serial")
    by_id = receivers.get(rid) if rid else None
    if rid and by_id is None:
        errors.append(f"receiver_id {rid!r} is not a known receiver ({', '.join(receivers)})")
        return None
    matches = {r.id for r in receivers.values() if serial and r.serial == serial} | \
              {r.id for r in receivers.values() if args_serial and r.serial == args_serial}
    if serial and not any(r.serial == serial for r in receivers.values()):
        errors.append(f"serial {serial!r} is not a known physical receiver")
        return None
    if by_id and matches and matches != {by_id.id}:
        errors.append(f"receiver_id {rid} and serial {serial or args_serial} name different physical SDRs")
        return None
    if by_id:
        return by_id
    if len(matches) == 1:
        return receivers[matches.pop()]
    errors.append("receiver.receiver_id or receiver.serial is required")
    return None


def check_job(raw: dict[str, Any], ctx: Context) -> Checked:
    job = copy.deepcopy(raw)
    c = Checked(job=job)
    e, w = c.errors, c.warnings
    defaults: list[str] = []
    s = ctx.settings

    name = job.get("job_name")
    if not isinstance(name, str) or not JOB_NAME.match(name):
        e.append("job_name is required: 3-80 characters, letters, digits, . _ -")

    # ---------------------------------------------------------------- target
    target = job.get("target")
    if not isinstance(target, dict):
        e.append("target is required")
        target = job["target"] = {}
    ttype = target.get("type")
    if ttype not in ("satellite", "frequency", "manual"):
        e.append("target.type must be satellite, frequency or manual")
    if ttype == "satellite" and not (target.get("satellite") or target.get("name") or target.get("norad_id")):
        e.append("a satellite target needs target.satellite, target.name or target.norad_id")
    tname = target.get("name") or target.get("satellite") or (f"NORAD-{target['norad_id']}" if target.get("norad_id") else None)
    if not tname and ttype != "satellite":
        e.append("target.name is required")

    # ---------------------------------------------------------------- schedule
    sch = job.get("schedule")
    if not isinstance(sch, dict):
        e.append("schedule is required")
        sch = job["schedule"] = {}
    tz = sch.get("timezone")
    if tz:
        try:
            zoneinfo.ZoneInfo(tz)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            e.append(f"schedule.timezone {tz!r} is not a valid IANA zone")
            tz = None
    start = _parse_time(sch.get("start"), tz, "start", e) if "start" in sch else None
    if "start" not in sch:
        e.append("schedule.start is required")
    end = _parse_time(sch["end"], tz, "end", e) if sch.get("end") is not None else None
    dur = sch.get("duration_seconds")
    if end is None and dur is None and sch.get("end") is None:
        e.append("schedule.end or schedule.duration_seconds is required")
    if dur is not None and (not isinstance(dur, int) or dur <= 0):
        e.append("schedule.duration_seconds must be a positive integer")
        dur = None
    if start and dur is not None:
        if end is not None and abs((end - start).total_seconds() - dur) > 1:
            e.append(f"schedule.end and duration_seconds disagree ({(end - start).total_seconds():.0f} s vs {dur} s)")
        end = end or start + dt.timedelta(seconds=dur)
    rolls = {}
    for key in ("pre_roll_seconds", "post_roll_seconds"):
        v = sch.get(key)
        if v is None:
            sch[key] = v = 0
            defaults.append(f"schedule.{key}=0")
        if not isinstance(v, int) or not 0 <= v <= 900:
            e.append(f"schedule.{key} must be an integer 0-900")
            v = 0
        rolls[key] = v
    res_start = res_end = None
    if start and end:
        if end <= start:
            e.append("schedule.end must be after schedule.start")
        elif (end - start).total_seconds() > int(s.get("max_duration_seconds", 14400)):
            e.append(f"recording is longer than the {int(s.get('max_duration_seconds', 14400))} s limit")
        res_start = start - dt.timedelta(seconds=rolls["pre_roll_seconds"])
        res_end = end + dt.timedelta(seconds=rolls["post_roll_seconds"])
        if res_end <= res_start:
            e.append("reservation_end must be after reservation_start")
        is_test = bool((job.get("metadata") or {}).get("rf_test"))   # signal-path tests start soon after the click
        lead = dt.timedelta(seconds=int(s.get("test_min_lead_seconds", 20) if is_test else s.get("min_lead_seconds", 120)))
        if res_start < ctx.now + lead:
            e.append(f"reservation starts {res_start.isoformat()}, less than {lead.seconds} s from now; "
                     "move the start later")
        if start > ctx.now + dt.timedelta(days=int(s.get("max_horizon_days", 30))):
            e.append(f"start is more than {s.get('max_horizon_days', 30)} days ahead")

    # ---------------------------------------------------------------- receiver
    rxb = job.get("receiver")
    if not isinstance(rxb, dict):
        e.append("receiver is required")
        rxb = job["receiver"] = {}
    rx = resolve_receiver(rxb, ctx.receivers, e)
    port = None
    if rx and not rx.schedulable and not (job.get("metadata") or {}).get("rf_test"):
        e.append(f"{rx.id} has not passed a hardware test yet ({rx.readiness}); only tests can run on it until a "
                 "test captures real IQ samples (Receivers → Verify)")
    if rx:
        if rxb.get("driver") not in (None, rx.driver):
            e.append(f"receiver.driver {rxb.get('driver')!r} does not match {rx.id}, which is {rx.driver}")
        if rxb.get("driver") is None:
            e.append(f"receiver.driver is required ({rx.id} is {rx.driver})")
        extra = set(rxb.get("device_args") or {}) - {"serial"}
        if extra:
            e.append(f"receiver.device_args {sorted(extra)} are not supported; only serial")
        if rxb.get("exclusive_lock") is False:
            e.append("receiver.exclusive_lock=false is not allowed; every job locks its physical SDR")
        rxb.setdefault("exclusive_lock", True)
        ant = rxb.get("antenna")
        if rx.switched:
            byname = {str(p["antenna"]).lower(): n for n, p in ctx.switch_ports.items()}
            if not ant:
                e.append(f"{rx.id} is fed by ANTSW-01; receiver.antenna must name a port antenna: "
                         f"{', '.join(p['antenna'] for p in ctx.switch_ports.values())}")
            elif str(ant).lower() not in byname:
                e.append(f"antenna {ant!r} is not on ANTSW-01 ({', '.join(p['antenna'] for p in ctx.switch_ports.values())})")
            else:
                port = byname[str(ant).lower()]
        elif rx.antenna == "UNSPECIFIED":
            w.append(f"{rx.id}'s antenna is not documented in the observatory config; recorded as requested ({ant})")
        elif not ant:
            rxb["antenna"] = rx.antenna
            defaults.append(f"receiver.antenna={rx.antenna} (hard-wired)")
        elif not _same_antenna(str(ant), rx):
            e.append(f"{rx.id} is hard-wired to {rx.antenna} ({rx.signal_path}); it cannot use antenna {ant!r}")
        if rxb.get("signal_path") and rxb["signal_path"] != rx.signal_path:
            w.append(f"signal_path given as {rxb['signal_path']!r}; the configured path is {rx.signal_path!r}")

    # ---------------------------------------------------------------- radio + recording
    radio = job.get("radio")
    if not isinstance(radio, dict):
        e.append("radio is required")
        radio = job["radio"] = {}
    rec = job.get("recording")
    if not isinstance(rec, dict):
        e.append("recording is required")
        rec = job["recording"] = {}
    if rec.get("enabled") is not True:
        e.append("recording.enabled must be true (live-only jobs are not supported)")
    engine, fmt = rec.get("engine"), rec.get("format")
    if not engine:
        e.append("recording.engine is required")
    if not fmt:
        e.append("recording.format is required")
    if "ppm_correction" not in radio:
        radio["ppm_correction"] = 0
        defaults.append("radio.ppm_correction=0")
    for flag in ("direct_sampling", "offset_tuning", "doppler_tracking", "iq_correction", "dc_offset_correction"):
        if flag not in radio:
            radio[flag] = False
            defaults.append(f"radio.{flag}=false")
    if rx:
        validate_radio(rx, radio, str(engine).lower() if engine else "", str(fmt).lower() if fmt else "", e, w)
    if radio.get("target_frequency_hz") is None and ttype in ("satellite", "frequency"):
        e.append("radio.target_frequency_hz is required for satellite and frequency targets")
    if job.get("steps") is not None and rx:
        _check_steps(job["steps"], rx, radio, str(engine).lower() if engine else "", str(fmt).lower() if fmt else "", e)

    out_dir = rec.get("output_directory")
    roots = list(s.get("allowed_output_roots", ["/data"]))
    if not out_dir:
        out_dir = rec["output_directory"] = f"{s.get('default_output_root', '/data/observatory/jobs')}/{{job_name}}"
        defaults.append(f"recording.output_directory={out_dir}")
    if not _under(str(out_dir).replace("{job_name}", "x").replace("{job_id}", "x"), roots):
        e.append(f"recording.output_directory must be an absolute path under {roots}")
    tmpl = rec.get("filename_template") or DEFAULT_TEMPLATE
    if not rec.get("filename_template"):
        rec["filename_template"] = tmpl
        defaults.append("recording.filename_template (default)")
    unknown = set(re.findall(r"\{([^}]*)\}", tmpl)) - PLACEHOLDERS
    if unknown or "/" in tmpl:
        e.append(f"filename_template may use only {sorted(PLACEHOLDERS)} and no '/'")
    if rec.get("split_seconds") is not None:
        e.append("recording.split_seconds is not supported yet; set it to null")
    est = None
    if start and end and isinstance(radio.get("sample_rate_hz"), int) and fmt in BYTES_PER_SAMPLE:
        seconds = (end - start).total_seconds()
        est = int(radio["sample_rate_hz"] * BYTES_PER_SAMPLE[fmt] * seconds)
        mx = rec.get("max_file_size_gb")
        if mx is not None and est > float(mx) * 1e9:
            e.append(f"estimated size {est / 1e9:.2f} GB exceeds max_file_size_gb {mx}")
    min_free = rec.get("minimum_free_space_gb")
    if min_free is None:
        rec["minimum_free_space_gb"] = min_free = 0
        defaults.append("recording.minimum_free_space_gb=0 (the global reserve still applies)")

    # ---------------------------------------------------------------- SatDump
    sd = job.get("satdump")
    if sd is None:
        sd = job["satdump"] = {"enabled": False, "operation": "none"}
        defaults.append("satdump.enabled=false")
    freq_shift = None
    if sd.get("enabled"):
        op = sd.get("operation")
        if op == "live":
            e.append("satdump.operation live is not supported here; record raw IQ and use decode_after_recording")
        elif op not in ("decode_after_recording", "none"):
            e.append("satdump.operation must be none or decode_after_recording")
        if op == "decode_after_recording":
            for key in ("pipeline", "input_type", "baseband_format", "sample_rate_hz", "frequency_hz", "output_directory"):
                if sd.get(key) is None:
                    e.append(f"satdump.{key} is required")
            if sd.get("pipeline") and sd["pipeline"] not in ctx.satdump_pipelines:
                e.append(f"SatDump pipeline {sd['pipeline']!r} is not installed")
            if sd.get("input_type") not in (None, "baseband"):
                e.append("satdump.input_type must be baseband (the recorder writes raw IQ)")
            if sd.get("baseband_format") and sd["baseband_format"] != fmt:
                e.append(f"satdump.baseband_format {sd['baseband_format']} does not match the recording format {fmt}; "
                         "there is no conversion stage")
            if sd.get("sample_rate_hz") is not None and sd["sample_rate_hz"] != radio.get("sample_rate_hz"):
                e.append(f"satdump.sample_rate_hz {sd['sample_rate_hz']} differs from the recorded sample rate "
                         f"{radio.get('sample_rate_hz')}")
            f, center, rate = sd.get("frequency_hz"), radio.get("center_frequency_hz"), radio.get("sample_rate_hz")
            if isinstance(f, int) and isinstance(center, int) and isinstance(rate, int):
                if abs(f - center) >= rate / 2 * 0.9:
                    e.append(f"satdump.frequency_hz {f} is outside the recorded passband")
                freq_shift = center - f   # SatDump shifts the baseband by this many Hz to put the signal at DC
            if sd.get("output_directory") and not _under(
                    str(sd["output_directory"]).replace("{job_name}", "x").replace("{job_id}", "x"), roots):
                e.append(f"satdump.output_directory must be under {roots}")
            for k, v in (sd.get("additional_parameters") or {}).items():
                if not SAFE_PARAM.match(k) or k in RESERVED_SATDUMP or not isinstance(v, (bool, int, float, str)) \
                        or (isinstance(v, str) and not re.match(r"^[A-Za-z0-9._:+-]{0,64}$", v)):
                    e.append(f"satdump.additional_parameters.{k} is not allowed")
    pp = job.get("postprocess") or {}
    job["postprocess"] = pp
    for key, default in (("generate_waterfall", False), ("generate_spectrum", False), ("generate_audio", False),
                         ("decode", bool(sd.get("enabled") and sd.get("operation") == "decode_after_recording")),
                         ("retain_raw_iq", True), ("checksum", True), ("record_metadata", True)):
        if key not in pp:
            pp[key] = default
            defaults.append(f"postprocess.{key}={str(default).lower()}")
    if pp.get("generate_audio"):
        e.append("postprocess.generate_audio is not supported yet")
    if pp.get("decode") and not (sd.get("enabled") and sd.get("operation") == "decode_after_recording"):
        e.append("postprocess.decode=true needs satdump.enabled with operation decode_after_recording")
    if pp.get("retain_raw_iq") is False and not pp.get("decode"):
        e.append("retain_raw_iq=false without a decode would leave nothing")
    if pp.get("record_metadata") is False:
        e.append("postprocess.record_metadata=false is not allowed; every recording gets a JSON sidecar")

    cp = job.get("conflict_policy") or {}
    job["conflict_policy"] = cp
    if "on_automatic_job" not in cp:
        cp["on_automatic_job"] = "preempt"      # "reject": never displace an automatic pass (signal-path tests)
    elif cp["on_automatic_job"] not in ("preempt", "reject"):
        e.append("conflict_policy.on_automatic_job must be preempt or reject")
    for key, allowed in (("on_receiver_conflict", ("reject",)), ("on_storage_warning", ("reject", "warn")),
                         ("on_missing_device", ("reject",))):
        if key not in cp:
            cp[key] = "reject"
            defaults.append(f"conflict_policy.{key}=reject")
        elif cp[key] not in allowed:
            e.append(f"conflict_policy.{key} must be one of {list(allowed)}")
    job.setdefault("metadata", {})

    job["resolved"] = {
        "receiver_id": rx.id if rx else None,
        "device_serial": rx.serial if rx else None,
        "driver": rx.driver if rx else None,
        "switch_port": port,
        "signal_path": rx.signal_path if rx else None,
        "record_start": start.isoformat() if start else None,
        "record_end": end.isoformat() if end else None,
        "reservation_start": res_start.isoformat() if res_start else None,
        "reservation_end": res_end.isoformat() if res_end else None,
        "recording_seconds": int((end - start).total_seconds()) if start and end else None,
        "estimated_bytes": est,
        "minimum_free_bytes": int(float(min_free or 0) * 1e9),
        "satdump_freq_shift_hz": freq_shift,
        "target_name": tname,
        "defaults_applied": defaults,
    }
    return c


def output_paths(job: dict[str, Any], job_id: str) -> dict[str, str]:
    r, rec = job["resolved"], job["recording"]
    start = dt.datetime.fromisoformat(r["record_start"])
    values = {"target": _slug(r["target_name"]), "receiver": _slug(r["receiver_id"]),
              "start_utc": start.strftime("%Y%m%dT%H%M%SZ"), "center_hz": str(job["radio"]["center_frequency_hz"]),
              "sample_rate": str(job["radio"]["sample_rate_hz"]), "format": rec["format"],
              "job_name": _slug(job["job_name"]), "job_id": job_id}
    directory = str(rec["output_directory"]).replace("{job_name}", values["job_name"]).replace("{job_id}", job_id)
    name = rec["filename_template"].format(**values)
    raw = str(pathlib.PurePosixPath(directory) / name)
    out = {"directory": directory, "raw": raw, "sidecar": raw + ".json", "log": raw + ".log"}
    sd = job.get("satdump") or {}
    if sd.get("enabled") and sd.get("operation") == "decode_after_recording":
        out["decode"] = str(sd["output_directory"]).replace("{job_name}", values["job_name"]).replace("{job_id}", job_id)
    return out


def human_size(n: int | None) -> str:
    if n is None:
        return "unknown"
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


MAX_STEPS = 4000


def _check_steps(sw: Any, rx: Receiver, radio: dict[str, Any], engine: str, fmt: str, e: list[str]) -> None:
    """A stepped job (wideband scan / gain sweep): every step's frequency and gain must be valid for the
    receiver on its own, exactly as a single recording with those settings would be."""
    if not isinstance(sw, dict) or not isinstance(sw.get("steps"), list) or not sw["steps"]:
        e.append("steps.steps must be a non-empty list")
        return
    reps = sw.get("repeats", 1)
    if not isinstance(reps, int) or not 1 <= reps <= 50:
        e.append("steps.repeats must be 1-50")
        return
    if len(sw["steps"]) * reps > MAX_STEPS:
        e.append(f"at most {MAX_STEPS} steps per job")
        return
    if not 0 <= float(sw.get("settle_s", 0.06)) <= 2:
        e.append("steps.settle_s must be 0-2 s")
    seen: set[str] = set()
    for i, st in enumerate(sw["steps"]):
        if not isinstance(st, dict) or not isinstance(st.get("center_hz"), int):
            e.append(f"steps[{i}].center_hz must be an integer")
            return
        if not 0.005 <= float(st.get("dwell_s", 0)) <= 20:
            e.append(f"steps[{i}].dwell_s must be 0.005-20 s")
            return
        r = dict(radio, center_frequency_hz=st["center_hz"], target_frequency_hz=None)
        g = st.get("gain")
        if rx.driver == "rtlsdr":
            r.update(gain_mode="auto" if g in (None, "auto") else "manual", gain_db=None if g in (None, "auto") else g)
        else:
            r.update(gain_elements=g or radio.get("gain_elements"))
        errs: list[str] = []
        validate_radio(rx, r, engine, fmt, errs, [])
        for x in errs:
            msg = f"step {i} ({st['center_hz'] / 1e6:.3f} MHz): {x}"
            if x not in seen:
                e.append(msg)
                seen.add(x)
