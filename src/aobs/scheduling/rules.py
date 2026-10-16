"""Automation rules and the rule planner (brief 6).

A rule says which satellites to record, how high a pass must peak, on which receivers (preferred, then
backups, in order) and with what settings. RulePlanner.reconcile() turns enabled rules into recording jobs
in the one schedule (sched_jobs, source "auto", rule_id set), so every booking rule of the scheduler still
applies: physical-device locks, storage reserve, verified receivers only, manual jobs preempting automatic
ones, running recordings never touched.

Reconciliation runs every planning cycle, after a rule is saved and after orbital elements are refreshed:

  * passes are predicted with the built-in Skyfield predictor at the observatory location;
  * a pass is matched to its existing job by rule + satellite + overlapping AOS-LOS, so a refresh that
    moves AOS by a few seconds updates that job instead of adding a second one;
  * a job changed by hand (`override` set) or cancelled by a user is never changed or recreated;
  * jobs that start within PROTECT_S, or are preparing/recording/processing/finished, are never changed;
  * the same satellite pass is booked once, by the highest-priority rule that covers it;
  * when the preferred receiver is busy, absent, unverified or cannot use the settings, the backups are
    tried in order; a higher-priority rule may displace a lower-priority rule's job that has not started,
    which then tries its own backups;
  * a pass no rule receiver can take is kept as a `skipped` job with each receiver's reason, so it is
    visible and is booked by a later cycle when a receiver becomes free;
  * passes that are no longer predicted, and jobs of disabled or deleted rules, become `skipped` (never
    deleted). The planner never sets `cancelled`: that status always means a person cancelled it.

With Settings → Scheduling = manual, enabled rules produce `predicted` jobs: shown in the calendar,
holding no receiver, never executed. Receivers whose automatic passes are still planned from
observatory.toml ([scheduler.receivers.*.auto]) or recorded by a legacy service are never used by rules,
so the existing automatic recording is unchanged until that receiver is migrated.
"""

from __future__ import annotations

import datetime as dt
import logging
import pathlib
import re
import secrets
from typing import Any

from sqlalchemy import and_, or_, select

from ..db.schema import sched_jobs, sched_rule_versions, sched_rules
from ..logjson import log_event
from ..satellites import predict
from . import spec as specmod
from .hardware import BYTES_PER_SAMPLE, Receiver, apply_gain_profile, auto_radio, default_radio, gain_profile_problems
from .store import ACTIVE, Placement, SchedulerError, new_job_id

LOG = logging.getLogger("aobs.rules")

PROTECT_S = 300                 # jobs starting sooner than this are never changed by the planner
RAW_IQ_POLICIES = ("default", "keep", "after_decode", "hours")
DONE = ("recorded", "processing", "completed", "failed", "partial", "missed")

DEFAULTS: dict[str, Any] = {
    "name": "", "enabled": False, "satellites": [], "min_max_elevation_deg": 20.0,
    "receiver_id": None, "backup_receivers": [], "antenna": None,
    "frequency_hz": None, "sample_rate_hz": None, "gain_profile": None, "format": None,
    "pre_aos_s": 30, "post_los_s": 30, "decode": False, "pipeline": None,
    "raw_iq_policy": "default", "raw_iq_hours": None, "priority": 50, "output_root": None, "notes": "",
}


class RuleError(SchedulerError):
    def __init__(self, message: str, status: int = 400, errors: list[str] | None = None):
        super().__init__(message, status)
        self.errors = errors or [message]


def _int(v: Any, name: str, lo: int, hi: int, errors: list[str]) -> int | None:
    if v is None or v == "":
        return None
    try:
        if isinstance(v, bool):
            raise ValueError
        f = float(v)
        if not f.is_integer():
            raise ValueError
        n = int(f)
    except (TypeError, ValueError):
        errors.append(f"{name} must be a whole number")
        return None
    if not lo <= n <= hi:
        errors.append(f"{name} must be between {lo} and {hi}")
    return n


def validate_rule(raw: Any, s: Any) -> tuple[dict[str, Any], list[str]]:
    """Normalized rule and errors. Unknown keys are errors, so a typo is never silently dropped."""
    if not isinstance(raw, dict):
        return dict(DEFAULTS), ["a rule must be an object"]
    errors = [f"unknown field {k!r}" for k in raw if k not in DEFAULTS]
    r = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    r["name"] = str(r["name"] or "").strip()
    if not 1 <= len(r["name"]) <= 80:
        errors.append("name is required (at most 80 characters)")
    if not isinstance(r["enabled"], bool):
        errors.append("enabled must be true or false")
    sats = r["satellites"]
    if not isinstance(sats, list) or not sats:
        errors.append("choose at least one satellite")
        sats = []
    norads = []
    for x in sats:
        n = _int(x, "satellite NORAD number", 1, 999_999_999, errors)
        if n is not None and n not in norads:
            norads.append(n)
    if len(norads) > 50:
        errors.append("at most 50 satellites per rule")
    r["satellites"] = norads
    try:
        r["min_max_elevation_deg"] = float(r["min_max_elevation_deg"])
        if not 0 <= r["min_max_elevation_deg"] <= 90:
            errors.append("minimum maximum elevation must be 0-90°")
    except (TypeError, ValueError):
        errors.append("minimum maximum elevation must be a number of degrees")
    receivers = s.receivers
    if not r["receiver_id"]:
        errors.append("choose a preferred receiver")
    elif r["receiver_id"] not in receivers:
        errors.append(f"receiver {r['receiver_id']!r} does not exist")
    backups = r["backup_receivers"] if isinstance(r["backup_receivers"], list) else []
    if not isinstance(r["backup_receivers"], list):
        errors.append("backup receivers must be a list")
    clean: list[str] = []
    for b in backups:
        if b == r["receiver_id"] or b in clean:
            errors.append(f"{b} is listed twice (preferred and backup receivers must differ)")
        elif b not in receivers:
            errors.append(f"backup receiver {b!r} does not exist")
        else:
            clean.append(b)
    r["backup_receivers"] = clean
    r["antenna"] = (str(r["antenna"]).strip() or None) if r["antenna"] else None
    r["frequency_hz"] = _int(r["frequency_hz"], "frequency (Hz)", 1_000, 20_000_000_000, errors)
    r["sample_rate_hz"] = _int(r["sample_rate_hz"], "sample rate (Hz)", 1_000, 100_000_000, errors)
    r["gain_profile"] = (str(r["gain_profile"]).strip() or None) if r["gain_profile"] else None
    r["format"] = (str(r["format"]).strip().lower() or None) if r["format"] else None
    if r["format"] and r["format"] not in BYTES_PER_SAMPLE:
        errors.append(f"recording format must be one of {', '.join(BYTES_PER_SAMPLE)}")
    for key, label in (("pre_aos_s", "pre-AOS margin"), ("post_los_s", "post-LOS margin")):
        v = _int(r[key], f"{label} (s)", 0, 900, errors)
        r[key] = 0 if v is None else v
    if not isinstance(r["decode"], bool):
        errors.append("decode must be true or false")
    r["pipeline"] = (str(r["pipeline"]).strip() or None) if r["pipeline"] else None
    if r["decode"]:
        for n in norads:
            if not (r["pipeline"] or profile_pipeline(s, n) or (predict.CATALOG.get(n) or {}).get("pipeline")):
                errors.append(f"automatic decoding: Starlab has no decoder for NORAD {n}; choose a SatDump pipeline")
                break
        pipe = r["pipeline"]
        if pipe and pipe not in s.pipelines:
            errors.append(f"SatDump pipeline {pipe!r} is not installed")
    if r["raw_iq_policy"] not in RAW_IQ_POLICIES:
        errors.append(f"raw IQ retention must be one of {', '.join(RAW_IQ_POLICIES)}")
    r["raw_iq_hours"] = _int(r["raw_iq_hours"], "raw IQ retention (hours)", 1, 87_600, errors)
    if r["raw_iq_policy"] == "hours" and r["raw_iq_hours"] is None:
        errors.append("raw IQ retention: give the number of hours")
    if r["raw_iq_policy"] == "after_decode" and not r["decode"]:
        errors.append("raw IQ can only be deleted after decoding when automatic decoding is on")
    pr = _int(r["priority"], "priority", 1, 100, errors)
    r["priority"] = 50 if pr is None else pr
    root = (str(r["output_root"]).strip().rstrip("/") or None) if r["output_root"] else None
    r["output_root"] = root
    if root:
        roots = list(s.settings.get("allowed_output_roots", ["/data"]))
        if not root.startswith("/") or ".." in root.split("/") or not specmod._under(root + "/x", roots):
            errors.append(f"storage destination must be an absolute folder under {roots}")
    if r["frequency_hz"] is None:
        for n in norads:
            if not (predict.CATALOG.get(n) or {}).get("downlink_hz"):
                errors.append(f"Starlab does not know the downlink of NORAD {n}; set the frequency")
                break
    r["notes"] = str(r["notes"] or "")[:2000]
    return r, errors


def profile_pipeline(s: Any, norad: int) -> str | None:
    """The pipeline of the decoder profile for this satellite (brief 8), if one is enabled and installed."""
    store = getattr(s, "profiles", None)
    prof = store.for_satellite(norad) if store is not None else None
    return prof["pipeline"] if prof else None


def raw_iq_hours(rule: dict[str, Any], settings: dict[str, Any]) -> float | None:
    """Hours to keep this rule's raw IQ after recording (None = keep)."""
    pol = rule.get("raw_iq_policy") or "default"
    if pol == "keep" or pol == "after_decode":
        return None
    if pol == "hours":
        return float(rule.get("raw_iq_hours") or 0) or None
    return float(settings.get("auto_retention_hours", 48))


class RuleStore:
    """Persistent rules with a revision each (optimistic concurrency) and every version kept."""

    def __init__(self, s: Any):
        self.s = s

    # ------------------------------------------------------------ queries
    @staticmethod
    def _out(row: Any) -> dict[str, Any]:
        d = dict(row)
        for k in ("created_at", "updated_at", "deleted_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        data = d.pop("data") or {}
        return {**d, **data, "rule_id": d["rule_id"], "revision": d["revision"], "enabled": d["enabled"],
                "name": d["name"]}

    def list(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        q = select(sched_rules)
        if not include_deleted:
            q = q.where(sched_rules.c.deleted_at.is_(None))
        with self.s.engine.connect() as conn:
            rows = conn.execute(q.order_by(sched_rules.c.created_at)).mappings().all()
        return [self._out(r) for r in rows]

    def get(self, rule_id: str, include_deleted: bool = False) -> dict[str, Any]:
        with self.s.engine.connect() as conn:
            row = conn.execute(select(sched_rules).where(sched_rules.c.rule_id == rule_id)).mappings().first()
        if row is None or (row["deleted_at"] is not None and not include_deleted):
            raise RuleError(f"no rule {rule_id}", 404)
        return self._out(row)

    def enabled(self) -> list[dict[str, Any]]:
        return [r for r in self.list() if r["enabled"]]

    def satellites(self) -> set[int]:
        return {int(n) for r in self.enabled() for n in r.get("satellites") or []}

    def history(self, rule_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.s.engine.connect() as conn:
            rows = conn.execute(select(sched_rule_versions).where(sched_rule_versions.c.rule_id == rule_id)
                                .order_by(sched_rule_versions.c.id.desc()).limit(limit)).mappings().all()
        return [{**dict(r), "at": r["at"].isoformat()} for r in rows]

    # ------------------------------------------------------------ changes
    def check(self, data: Any, rule_id: str | None = None) -> dict[str, Any]:
        rule, errors = validate_rule(data, self.s)
        if not errors:
            others = [r for r in self.list() if r["rule_id"] != rule_id]
            if any(r["name"].lower() == rule["name"].lower() for r in others):
                errors.append(f"another rule is already called {rule['name']!r}")
        return {"ok": not errors, "errors": errors, "rule": rule,
                "warnings": receiver_warnings(self.s, rule) if not errors else []}

    def _version(self, conn: Any, rule_id: str, rev: int, action: str, data: dict[str, Any], author: str,
                 now: dt.datetime) -> None:
        conn.execute(sched_rule_versions.insert().values(rule_id=rule_id, revision=rev, action=action, data=data,
                                                         author=author, at=now))

    def create(self, data: Any, author: str, action: str = "created") -> dict[str, Any]:
        res = self.check(data)
        if not res["ok"]:
            raise RuleError("the rule was not saved: " + "; ".join(res["errors"]), 400, res["errors"])
        rule, now = res["rule"], self.s.clock()
        rule_id = "r" + secrets.token_hex(5)
        with self.s.locked(), self.s.engine.begin() as conn:
            conn.execute(sched_rules.insert().values(rule_id=rule_id, name=rule["name"], enabled=rule["enabled"],
                                                     revision=1, data=rule, created_by=author, updated_by=author,
                                                     created_at=now, updated_at=now))
            self._version(conn, rule_id, 1, action, rule, author, now)
        log_event(LOG, "rule created", rule_id=rule_id, name=rule["name"], enabled=rule["enabled"], by=author)
        return {**self.get(rule_id), "warnings": res["warnings"]}

    def update(self, rule_id: str, data: Any, revision: int | None, author: str) -> dict[str, Any]:
        cur = self.get(rule_id)
        if revision is None or int(revision) != cur["revision"]:
            raise RuleError(f"{cur['name']} was changed elsewhere (now revision {cur['revision']}); reload it and "
                            "make your change again", 409)
        res = self.check(data, rule_id)
        if not res["ok"]:
            raise RuleError("the rule was not saved: " + "; ".join(res["errors"]), 400, res["errors"])
        rule, now = res["rule"], self.s.clock()
        with self.s.locked(), self.s.engine.begin() as conn:
            n = conn.execute(sched_rules.update().where(and_(sched_rules.c.rule_id == rule_id,
                                                             sched_rules.c.revision == cur["revision"]))
                             .values(name=rule["name"], enabled=rule["enabled"], data=rule, revision=cur["revision"] + 1,
                                     updated_by=author, updated_at=now)).rowcount
            if n != 1:
                raise RuleError(f"{cur['name']} was changed elsewhere; reload it", 409)
            self._version(conn, rule_id, cur["revision"] + 1, "updated", rule, author, now)
        log_event(LOG, "rule updated", rule_id=rule_id, name=rule["name"], enabled=rule["enabled"], by=author)
        return {**self.get(rule_id), "warnings": res["warnings"]}

    def delete(self, rule_id: str, revision: int | None, author: str) -> dict[str, Any]:
        cur = self.get(rule_id)
        if revision is None or int(revision) != cur["revision"]:
            raise RuleError(f"{cur['name']} was changed elsewhere (now revision {cur['revision']}); reload it", 409)
        now = self.s.clock()
        data = {k: cur[k] for k in DEFAULTS}
        with self.s.locked(), self.s.engine.begin() as conn:
            conn.execute(sched_rules.update().where(sched_rules.c.rule_id == rule_id)
                         .values(deleted_at=now, enabled=False, revision=cur["revision"] + 1, updated_by=author,
                                 updated_at=now))
            self._version(conn, rule_id, cur["revision"] + 1, "deleted", data, author, now)
        log_event(LOG, "rule deleted", rule_id=rule_id, name=cur["name"], by=author)
        return {"rule_id": rule_id, "deleted": True}

    def import_from_toml(self, author: str = "import (observatory.toml)") -> list[str]:
        """Show each receiver's [scheduler.receivers.*.auto] as a rule, once, disabled.

        The imported rule documents what that receiver records today. It stays disabled, and rules never
        use a receiver whose automatic passes are still planned from the TOML, so nothing changes until the
        receiver is migrated and the rule enabled by hand."""
        made = []
        with self.s.engine.connect() as conn:
            seen = {r.data.get("imported_from") for r in conn.execute(
                select(sched_rule_versions.c.data).where(sched_rule_versions.c.action == "imported"))
                if isinstance(r.data, dict)}
        for rx in self.s.receivers.values():
            a = rx.auto or {}
            if not a.get("enabled") or not a.get("norad_ids") or rx.id in seen:
                continue
            data = {**DEFAULTS, "name": f"{rx.id} automatic passes (from observatory.toml)", "enabled": False,
                    "satellites": [int(n) for n in a["norad_ids"]], "receiver_id": rx.id,
                    "frequency_hz": a.get("target_frequency_hz"), "sample_rate_hz": a.get("sample_rate_hz"),
                    "format": a.get("format"), "decode": bool(a.get("satdump_pipeline")),
                    "pipeline": a.get("satdump_pipeline") or None,
                    "notes": (f"Imported from [scheduler.receivers.{rx.id}.auto]. {rx.id} is still recorded by its "
                              "existing automatic schedule; this rule cannot use it until that is migrated. Gain: the "
                              "receiver's observatory.toml settings (no gain profile chosen).")}
            res = self.check(data)
            if not res["ok"]:
                LOG.warning("not importing %s automatic settings as a rule: %s", rx.id, res["errors"])
                continue
            rule = self.create(res["rule"], author, action="imported")
            with self.s.engine.begin() as conn:     # remember the import (also if the rule is later deleted)
                conn.execute(sched_rule_versions.update().where(and_(
                    sched_rule_versions.c.rule_id == rule["rule_id"], sched_rule_versions.c.revision == 1))
                    .values(data={**res["rule"], "imported_from": rx.id}))
            made.append(rule["rule_id"])
        return made


# ------------------------------------------------------------------ receivers and job content
def rule_receivers(rule: dict[str, Any]) -> list[str]:
    return [x for x in [rule.get("receiver_id"), *(rule.get("backup_receivers") or [])] if x]


def owned_elsewhere(rx: Receiver) -> str | None:
    """Why rules may not book this receiver, if another automatic schedule owns it."""
    if rx.mode in ("legacy", "shadow"):
        return (f"{rx.id} is recorded by {rx.legacy_unit or 'its legacy recorder'} ({rx.mode} mode); rules can use it "
                "after it is migrated to the broker")
    if (rx.auto or {}).get("enabled"):
        return (f"{rx.id}'s automatic passes are planned from observatory.toml "
                f"([scheduler.receivers.{rx.id}.auto]); turn that off to let rules use it")
    return None


def gain_profiles(s: Any, rid: str) -> list[dict[str, Any]]:
    layout = ((getattr(s, "rf", None) and s.rf.current) or {}).get("layout") or {}
    return list(((layout.get("receivers") or {}).get(rid) or {}).get("gain_profiles") or [])


def build_radio(s: Any, rule: dict[str, Any], rx: Receiver, freq: int, problems: list[str]) -> dict[str, Any]:
    rate = rule.get("sample_rate_hz")
    a = rx.auto or {}
    toml_gain = bool(a.get("center_frequency_hz")) and not rule.get("gain_profile")
    if toml_gain:
        try:                                  # the receiver's own automatic settings (gain, bandwidth)
            radio = auto_radio(rx)
            radio.update(target_frequency_hz=int(freq))
            if rate:
                radio["sample_rate_hz"] = int(rate)
            radio["center_frequency_hz"] = int(freq) - int(radio["sample_rate_hz"]) // 8
            for flag in ("iq_correction", "dc_offset_correction"):
                radio[flag] = bool(radio.get(flag))
            return radio
        except (KeyError, TypeError, ValueError):
            pass
    try:
        radio = default_radio(rx, freq, rate)
    except ValueError as exc:
        problems.append(str(exc))
        return {}
    name = rule.get("gain_profile")
    if name:
        prof = next((p for p in gain_profiles(s, rx.id) if str(p.get("name", "")).lower() == name.lower()), None)
        if prof is None:
            problems.append(f"{rx.id} has no gain profile called {name!r} (RF editor → {rx.id} → Gain profiles)")
        else:
            bad = gain_profile_problems(rx, prof)
            if bad:
                problems.append(f"gain profile {name!r} on {rx.id}: {'; '.join(bad)}")
            apply_gain_profile(rx, radio, prof)
    return radio


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "pass"


def build_job(s: Any, rule: dict[str, Any], p: dict[str, Any], rx: Receiver, problems: list[str]) -> dict[str, Any]:
    """The pass as a normal job request (spec.check_job validates it like any other booking)."""
    cat = predict.CATALOG.get(int(p["norad"])) or {}
    freq = int(rule.get("frequency_hz") or cat.get("downlink_hz") or 0)
    radio = build_radio(s, rule, rx, freq, problems)
    fmt = rule.get("format") or (rx.auto or {}).get("format") or rx.formats()[0]
    if fmt not in rx.formats():
        problems.append(f"{rx.id} cannot record {fmt} (it records {', '.join(rx.formats())})")
    aos, los = dt.datetime.fromisoformat(p["aos"]), dt.datetime.fromisoformat(p["los"])
    start = aos - dt.timedelta(seconds=int(rule.get("pre_aos_s") or 0))
    end = los + dt.timedelta(seconds=int(rule.get("post_los_s") or 0))
    root = rule.get("output_root") or s.settings.get("auto_output_root", "/data/observatory/auto")
    pipeline = (rule.get("pipeline") or profile_pipeline(s, int(p["norad"])) or cat.get("pipeline")) \
        if rule.get("decode") else None
    rate = radio.get("sample_rate_hz")
    rxb: dict[str, Any] = {"receiver_id": rx.id, "driver": rx.driver, "serial": rx.serial, "exclusive_lock": True}
    if rule.get("antenna"):
        rxb["antenna"] = rule["antenna"]
    sat_name = p.get("name") or cat.get("name") or f"NORAD {p['norad']}"
    return {
        "job_name": f"{_slug(sat_name)}-{aos:%Y%m%d-%H%M}"[:80],
        "target": {"type": "satellite", "name": sat_name, "satellite": sat_name, "norad_id": int(p["norad"])},
        "schedule": {"start": start.isoformat(), "end": end.isoformat(), "timezone": "UTC",
                     "pre_roll_seconds": 30, "post_roll_seconds": 15},
        "receiver": rxb,
        "radio": radio,
        "recording": {"enabled": True, "engine": rx.engines()[0], "format": fmt,
                      "output_directory": f"{root}/{rx.id}/{{job_name}}", "minimum_free_space_gb": 0},
        "satdump": ({"enabled": True, "operation": "decode_after_recording", "pipeline": pipeline,
                     "input_type": "baseband", "baseband_format": fmt, "sample_rate_hz": rate, "frequency_hz": freq,
                     "output_directory": f"{root}/decoded/{{job_name}}"} if pipeline
                    else {"enabled": False, "operation": "none"}),
        "postprocess": {"generate_waterfall": True, "generate_spectrum": True, "decode": bool(pipeline),
                        "retain_raw_iq": rule.get("raw_iq_policy") != "after_decode", "checksum": True,
                        "record_metadata": True},
        "conflict_policy": {"on_receiver_conflict": "reject", "on_storage_warning": "reject",
                            "on_missing_device": "reject"},
        "metadata": {"booked_from": "rule", "rule_id": rule["rule_id"], "rule_name": rule["name"],
                     "priority": rule["priority"], "norad": int(p["norad"]), "aos": p["aos"], "los": p["los"],
                     "max_elevation_deg": p["max_elevation_deg"], "max_at": p.get("max_at"),
                     "aos_azimuth_deg": p.get("aos_azimuth_deg"), "los_azimuth_deg": p.get("los_azimuth_deg"),
                     "duration_s": p.get("duration_s"), "elements_epoch": p.get("elements_epoch"),
                     "elements_source": p.get("elements_source"),
                     "raw_iq_hours": raw_iq_hours(rule, s.settings),
                     "passes": [{"name": sat_name, "norad_id": int(p["norad"]), "rise": p["aos"], "set": p["los"],
                                 "max_elevation_deg": p["max_elevation_deg"]}]},
    }


def receiver_warnings(s: Any, rule: dict[str, Any]) -> list[str]:
    """What stops each of the rule's receivers from taking its passes, checked on a sample pass now."""
    out = []
    now = s.clock()
    usable = 0
    for rid in rule_receivers(rule):
        rx = s.receivers.get(rid)
        if rx is None:
            continue
        why = owned_elsewhere(rx)
        if why:
            out.append(why)
            continue
        if not rx.schedulable:
            out.append(f"{rid} has not passed a hardware test ({rx.readiness}); no automatic recording on it until it "
                       "is verified (Receivers → Verify)")
            continue
        bad = []
        for n in rule.get("satellites") or []:
            t0 = now + dt.timedelta(hours=1)
            sample = {"norad": n, "name": (predict.CATALOG.get(n) or {}).get("name") or f"NORAD {n}",
                      "aos": t0.isoformat(), "los": (t0 + dt.timedelta(minutes=10)).isoformat(),
                      "max_elevation_deg": 45.0}
            problems: list[str] = []
            job = build_job(s, {**rule, "rule_id": "check"}, sample, rx, problems)
            checked = specmod.check_job(job, s.context())
            bad += problems + [e for e in checked.errors if "less than" not in e]
            out += [f"{rid}: {w}" for w in checked.warnings if w.startswith("signal path")]
        if bad:
            out.append(f"{rid} cannot take these passes: {'; '.join(dict.fromkeys(bad))}")
        else:
            usable += 1
    if rule_receivers(rule) and not usable:
        out.insert(0, "none of this rule's receivers can record its passes right now, so it will not book anything")
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ planner
class RulePlanner:
    def __init__(self, s: Any, fetch: Any = None):
        self.s = s
        self.fetch = fetch                     # tests: a fake downloader for orbital elements
        self.last: dict[str, Any] = {}

    def _doc(self, name: str) -> dict[str, Any]:
        cur = self.s.config_store.current(name)
        return (cur or {}).get("data") or {}

    def booking(self) -> bool:
        return (self._doc("scheduling").get("mode") or "manual") in ("automatic", "hybrid")

    def maybe_refresh(self) -> dict[str, Any] | None:
        """Download fresh orbital elements when that is on and the last download is older than refresh_hours."""
        sats = self._doc("satellites")
        if not sats.get("refresh_online"):
            return None
        last = predict.last_refresh(self.s.config.data_root)
        if last is not None and self.s.clock() - last < dt.timedelta(hours=float(sats.get("refresh_hours") or 12)):
            return None
        from ..satellites.passes import PassService
        try:
            res = PassService(self.s).refresh(self.fetch)
        except Exception as exc:  # noqa: BLE001 - cached elements stay in use
            LOG.warning("orbital element refresh failed: %s", exc)
            return {"error": str(exc)}
        log_event(LOG, "orbital elements refreshed", updated=res["updated"], failed=sorted(res["failed"]))
        return res

    # ------------------------------------------------------------ the cycle
    def reconcile(self, refresh: bool = True) -> dict[str, Any]:
        now = self.s.clock()
        booking = self.booking()
        out: dict[str, Any] = {"at": now.isoformat(), "booking": booking, "rules": {}, "warnings": []}
        if refresh:
            r = self.maybe_refresh()
            if r is not None:
                out["refresh"] = {k: r.get(k) for k in ("updated", "failed", "error") if k in r}
        from ..satellites.passes import PassService
        loc = PassService(self.s).location()
        rules = {r["rule_id"]: r for r in self.s.rules.list(include_deleted=True)}
        live = {k: r for k, r in rules.items() if r["enabled"] and not r.get("deleted_at")}
        if loc is None:
            if live:
                out["warnings"].append("the observatory location is not set (Settings → Observatory); rules cannot "
                                       "predict passes. Existing jobs are kept.")
            self.last = out
            return out
        sats_doc = self._doc("satellites")
        stale_days = float(sats_doc.get("stale_days") or predict.STALE_DAYS)
        elements = predict.load_elements(self.s.config.data_root)
        horizon_h = float(self._doc("scheduling").get("horizon_hours") or 48)
        horizon = now + dt.timedelta(hours=horizon_h)
        candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        predictable: dict[str, set[int]] = {}
        for rid, rule in live.items():
            info = out["rules"][rid] = {"name": rule["name"], "passes": 0, "inserted": 0, "updated": 0, "moved": 0,
                                        "skipped": 0, "duplicates": 0, "kept_overrides": 0, "warnings": []}
            st = predict.element_status(elements, rule["satellites"], now, stale_days)
            for e in st:
                if e.get("warning"):
                    info["warnings"].append(f"NORAD {e['norad']}{' (' + e['name'] + ')' if e.get('name') else ''}: "
                                            f"{e['warning']}")
            predictable[rid] = {e["norad"] for e in st if e["available"]}
            passes = predict.predict_passes(elements, sorted(predictable[rid]), *loc, now, horizon_h,
                                            float(rule["min_max_elevation_deg"]))
            info["passes"] = len(passes)
            candidates += [(rule, p) for p in passes]
        # the most important first: priority, then the higher pass, then the earlier one
        candidates.sort(key=lambda c: (-c[0]["priority"], -c[1]["max_elevation_deg"], c[1]["aos"]))
        seen: set[str] = set()
        with self.s.locked(), self.s.engine.begin() as conn:
            for rule, p in candidates:
                try:
                    self._candidate(conn, rule, p, rules, booking, now, out["rules"][rule["rule_id"]], seen)
                except Exception as exc:  # noqa: BLE001 - one pass must not stop the others
                    LOG.exception("rule %s pass %s failed", rule["rule_id"], p.get("aos"))
                    out["rules"][rule["rule_id"]]["warnings"].append(f"{p['name']} {p['aos'][:16]}: {exc}")
            self._sweep(conn, rules, live, predictable, seen, horizon, now, out)
        pruned = self.prune()
        if pruned:
            out["pruned_raw_iq"] = pruned
        self.last = out
        changed = {k: v for k, v in out["rules"].items()
                   if any(v[x] for x in ("inserted", "updated", "moved", "skipped"))}
        if changed:
            log_event(LOG, "rules reconciled", booking=booking, **{k: {x: v[x] for x in ("inserted", "updated", "moved",
                                                                                         "skipped")} for k, v in changed.items()})
        return out

    # ------------------------------------------------------------ one predicted pass
    def _match(self, conn: Any, rule_id: str, p: dict[str, Any]) -> Any:
        """The job this rule already has for this pass: same satellite, overlapping AOS-LOS, any status."""
        aos, los = dt.datetime.fromisoformat(p["aos"]), dt.datetime.fromisoformat(p["los"])
        rows = conn.execute(select(sched_jobs).where(and_(
            sched_jobs.c.rule_id == rule_id, sched_jobs.c.record_start < los + dt.timedelta(minutes=20),
            sched_jobs.c.record_end > aos - dt.timedelta(minutes=20)))).mappings().all()
        for r in rows:
            m = (r["spec"] or {}).get("metadata") or {}
            if int(m.get("norad") or 0) != int(p["norad"]):
                continue
            a, b = dt.datetime.fromisoformat(m["aos"]), dt.datetime.fromisoformat(m["los"])
            if a < los and aos < b:
                return r
        return None

    def _duplicate(self, conn: Any, rule_id: str, p: dict[str, Any]) -> Any:
        """Another live job recording this same pass (another rule, a manual booking, a legacy window)."""
        aos, los = dt.datetime.fromisoformat(p["aos"]), dt.datetime.fromisoformat(p["los"])
        rows = conn.execute(select(sched_jobs).where(and_(
            or_(sched_jobs.c.rule_id.is_(None), sched_jobs.c.rule_id != rule_id),
            sched_jobs.c.status.in_(("scheduled", "predicted", "preparing", "recording", "recorded", "processing",
                                     "completed")),
            sched_jobs.c.record_start < los, sched_jobs.c.record_end > aos))).mappings().all()
        name = str(p["name"]).upper().replace("-", " ")
        for r in rows:
            spec = r["spec"] or {}
            norads = {int(x) for x in ((spec.get("target") or {}).get("norad_ids") or [])}
            t = spec.get("target") or {}
            if t.get("norad_id"):
                norads.add(int(t["norad_id"]))
            if (spec.get("metadata") or {}).get("norad"):
                norads.add(int(spec["metadata"]["norad"]))
            names = {str(x).upper().replace("-", " ") for x in (r["target"], t.get("satellite"), t.get("name")) if x}
            if int(p["norad"]) in norads or any(name == n or name in n.split(" + ") for n in names):
                if r["rule_id"] and r["status"] == "predicted" and r["executes"] is False:
                    continue                  # another rule's preview does not block a booking
                return r
        return None

    def _candidate(self, conn: Any, rule: dict[str, Any], p: dict[str, Any], rules: dict[str, dict[str, Any]],
                   booking: bool, now: dt.datetime, info: dict[str, Any], seen: set[str]) -> None:
        protect = now + dt.timedelta(seconds=PROTECT_S)
        row = self._match(conn, rule["rule_id"], p)
        if row is not None:
            seen.add(row["job_id"])
            st = row["status"]
            if st in ACTIVE or st in DONE or st == "cancelled":
                return                        # running, finished, or cancelled by a person: never touched
            if row["override"]:
                info["kept_overrides"] += 1
                self._note_drift(conn, row, p, now)
                return
            if row["reservation_start"] <= protect:
                return
        else:
            dup = self._duplicate(conn, rule["rule_id"], p)
            if dup is not None:
                info["duplicates"] += 1
                return
            aos = dt.datetime.fromisoformat(p["aos"])
            if aos - dt.timedelta(seconds=int(rule.get("pre_aos_s") or 0) + 30) <= protect:
                return                        # too close to book safely
        target = "scheduled" if booking else "predicted"
        placed, reasons = self._place(conn, rule, p, rules, booking, now, row)
        if placed is None:
            reason = "no receiver can take this pass: " + "; ".join(reasons)
            if row is None:
                seen.add(self._insert(conn, rule, p, None, None, "skipped", reason, now) or "")
                info["skipped"] += 1
            elif st != "skipped" or row["status_reason"] != reason:
                self._set(conn, row, status="skipped", reason=reason, executes=False, now=now)
                if st != "skipped":
                    info["skipped"] += 1
            return
        rx, job, displaced = placed
        for d in displaced:
            why = (f"displaced by the higher-priority rule {rule['name']!r} ({p['name']} at {p['aos'][11:16]}Z); "
                   "trying its backup receivers")
            self._set(conn, d, status="skipped", reason=why, executes=False, now=now)
        reason = (f"booked by rule {rule['name']!r}" if booking else
                  f"predicted by rule {rule['name']!r} (Settings → Scheduling is manual, so it is not booked)")
        if rx.id != rule["receiver_id"]:
            reason += f" on backup receiver {rx.id} ({'; '.join(reasons)})"
        if row is None:
            seen.add(self._insert(conn, rule, p, rx, job, target, reason, now) or "")
            info["inserted"] += 1
            return
        moved = row["receiver_id"] != rx.id
        changed = (moved or st != target or row["spec"] != job
                   or abs((row["record_start"] - dt.datetime.fromisoformat(job["resolved"]["record_start"])).total_seconds()) >= 1
                   or abs((row["record_end"] - dt.datetime.fromisoformat(job["resolved"]["record_end"])).total_seconds()) >= 1)
        if changed:
            self._write(conn, row, rx, job, target, reason, now)
            info["moved" if moved else "updated"] += 1

    def _place(self, conn: Any, rule: dict[str, Any], p: dict[str, Any], rules: dict[str, dict[str, Any]],
               booking: bool, now: dt.datetime, row: Any) -> tuple[Any, list[str]]:
        reasons: list[str] = []
        exclude = row["job_id"] if row is not None else None
        cutoff = now + dt.timedelta(seconds=int(self.s.settings.get("preempt_cutoff_seconds", 180)))
        ctx = self.s.context()
        order = rule_receivers(rule)
        for rid in order:
            rx = self.s.receivers.get(rid)
            if rx is None:
                reasons.append(f"{rid} no longer exists")
                continue
            why = owned_elsewhere(rx)
            if why:
                reasons.append(why)
                continue
            if not rx.schedulable:
                reasons.append(f"{rid} is not verified ({rx.readiness})")
                continue
            if not self.s._present(rx):
                reasons.append(f"{rid} is not connected")
                continue
            problems: list[str] = []
            raw = build_job(self.s, rule, p, rx, problems)
            if problems:
                reasons.append(f"{rid}: {'; '.join(problems)}")
                continue
            checked = specmod.check_job(raw, ctx)
            if checked.errors:
                reasons.append(f"{rid}: {'; '.join(checked.errors)}")
                continue
            job = checked.job
            job["metadata"]["warnings"] = checked.warnings
            r = job["resolved"]
            rs, re_ = dt.datetime.fromisoformat(r["reservation_start"]), dt.datetime.fromisoformat(r["reservation_end"])
            blocking, displace = [], []
            for c in self.s.conflicts(conn, rx.serial, rs, re_, exclude=exclude):
                other = rules.get(c["rule_id"] or "")
                if booking and other is not None and c["status"] == "scheduled" and not c["override"] \
                        and c["reservation_start"] > cutoff and int(other["priority"]) < int(rule["priority"]):
                    displace.append(c)
                else:
                    blocking.append(c)
            sessions = [x for x in self.s.session_conflicts(rx.serial, rs, re_)
                        if not (x.get("policy") or {}).get("end_before_passes")]
            if blocking or sessions:
                b = blocking[0] if blocking else None
                reasons.append(f"{rid} is busy" + (f" with {b['job_name']} ({b['job_id']}, {b['source']}, "
                                                   f"{b['reservation_start']:%H:%M}-{b['reservation_end']:%H:%M}Z)"
                                                   if b is not None else " with an SDR++ session"))
                continue
            if booking:
                pl = Placement(True, [], [], job, [])
                self.s._check_storage(conn, pl)
                if pl.errors:
                    reasons.append(f"{rid}: {'; '.join(pl.errors)}")
                    continue
            return (rx, job, displace), reasons
        return None, reasons

    # ------------------------------------------------------------ writes
    def _insert(self, conn: Any, rule: dict[str, Any], p: dict[str, Any], rx: Receiver | None, job: dict[str, Any] | None,
                status: str, reason: str, now: dt.datetime) -> str | None:
        aos = dt.datetime.fromisoformat(p["aos"])
        if job is None:                       # nobody can take it: keep a visible record on the preferred receiver
            rx = self.s.receivers.get(rule["receiver_id"])
            if rx is None:
                return None
            job = build_job(self.s, rule, p, rx, [])
            start = aos - dt.timedelta(seconds=int(rule.get("pre_aos_s") or 0))
            end = dt.datetime.fromisoformat(p["los"]) + dt.timedelta(seconds=int(rule.get("post_los_s") or 0))
            job["resolved"] = {"receiver_id": rx.id, "device_serial": rx.serial, "record_start": start.isoformat(),
                               "record_end": end.isoformat(), "reservation_start": start.isoformat(),
                               "reservation_end": end.isoformat(), "target_name": job["target"]["name"],
                               "estimated_bytes": None}
        assert rx is not None
        r = job["resolved"]
        job_id = new_job_id("a", aos)
        paths = specmod.output_paths(job, job_id) if status != "skipped" else None
        if paths:
            r["paths"] = paths
        conn.execute(sched_jobs.insert().values(
            job_id=job_id, job_name=job["job_name"], source="auto", receiver_id=rx.id, device_serial=rx.serial,
            status=status, status_reason=reason, executes=status == "scheduled",
            reservation_start=dt.datetime.fromisoformat(r["reservation_start"]),
            reservation_end=dt.datetime.fromisoformat(r["reservation_end"]),
            record_start=dt.datetime.fromisoformat(r["record_start"]), record_end=dt.datetime.fromisoformat(r["record_end"]),
            pass_key=f"{rule['rule_id']}:{p['norad']}:{aos:%Y%m%dT%H%M%S}", target=r["target_name"],
            estimated_bytes=r.get("estimated_bytes"), spec=job, paths=paths, rule_id=rule["rule_id"],
            requested_by=f"rule:{rule['name']}"[:128], created_at=now, updated_at=now))
        self.s._event(conn, job_id, None, status, reason, "rules", now)
        if status == "scheduled":
            self.s.notify(conn, "scheduled", job_id, f"Rule {rule['name']!r} booked {job['job_name']} on {rx.id}, "
                          f"{r['record_start'][:16]}Z to {r['record_end'][11:16]}Z (peak {p['max_elevation_deg']}°).",
                          {"receiver": rx.id, "rule_id": rule["rule_id"]}, now)
        return job_id

    def _write(self, conn: Any, row: Any, rx: Receiver, job: dict[str, Any], status: str, reason: str,
               now: dt.datetime) -> None:
        r = job["resolved"]
        paths = specmod.output_paths(job, row["job_id"])
        r["paths"] = paths
        conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == row["job_id"]).values(
            receiver_id=rx.id, device_serial=rx.serial, status=status, status_reason=reason,
            executes=status == "scheduled", preempted_by=None,
            reservation_start=dt.datetime.fromisoformat(r["reservation_start"]),
            reservation_end=dt.datetime.fromisoformat(r["reservation_end"]),
            record_start=dt.datetime.fromisoformat(r["record_start"]), record_end=dt.datetime.fromisoformat(r["record_end"]),
            job_name=job["job_name"], target=r["target_name"], estimated_bytes=r.get("estimated_bytes"), spec=job,
            paths=paths, updated_at=now))
        if row["status"] != status or row["receiver_id"] != rx.id:
            self.s._event(conn, row["job_id"], row["status"], status,
                          reason if row["receiver_id"] == rx.id else f"{reason} (was on {row['receiver_id']})", "rules", now)

    def _set(self, conn: Any, row: Any, status: str, reason: str, executes: bool, now: dt.datetime) -> None:
        conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == row["job_id"])
                     .values(status=status, status_reason=reason, executes=executes, updated_at=now))
        if row["status"] != status:
            self.s._event(conn, row["job_id"], row["status"], status, reason, "rules", now)

    def _note_drift(self, conn: Any, row: Any, p: dict[str, Any], now: dt.datetime) -> None:
        m = (row["spec"] or {}).get("metadata") or {}
        drift = (dt.datetime.fromisoformat(p["aos"]) - dt.datetime.fromisoformat(m["aos"])).total_seconds()
        if abs(drift) < 30:
            return
        note = (f"the latest prediction puts AOS at {p['aos'][11:19]}Z ({drift:+.0f} s); kept as you set it "
                "because it was changed by hand")
        ov = dict(row["override"] or {})
        if ov.get("planner_note") != note:
            ov["planner_note"] = note
            conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == row["job_id"]).values(override=ov))

    def _sweep(self, conn: Any, rules: dict[str, dict[str, Any]], live: dict[str, dict[str, Any]],
               predictable: dict[str, set[int]], seen: set[str], horizon: dt.datetime, now: dt.datetime,
               out: dict[str, Any]) -> None:
        """Future jobs whose rule is off or whose pass is no longer predicted: skipped (overrides kept)."""
        protect = now + dt.timedelta(seconds=PROTECT_S)
        rows = conn.execute(select(sched_jobs).where(and_(
            sched_jobs.c.rule_id.is_not(None), sched_jobs.c.status.in_(("scheduled", "predicted", "skipped")),
            sched_jobs.c.reservation_start > protect))).mappings().all()
        for row in rows:
            if row["job_id"] in seen:
                continue
            rule = rules.get(row["rule_id"])
            m = (row["spec"] or {}).get("metadata") or {}
            if row["rule_id"] not in live:
                why = (f"rule {rule['name']!r} was deleted" if rule and rule.get("deleted_at")
                       else f"rule {rule['name'] if rule else row['rule_id']!r} is disabled")
            elif int(m.get("norad") or 0) not in predictable.get(row["rule_id"], set()):
                continue                       # no elements for it now: cannot tell, so leave it as it is
            elif row["record_end"] > horizon:
                continue
            else:
                why = "pass no longer predicted with the current orbital elements (or below the rule's elevation)"
            if row["override"]:
                ov = dict(row["override"])
                if ov.get("planner_note") != why + "; kept because it was changed by hand":
                    ov["planner_note"] = why + "; kept because it was changed by hand"
                    conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == row["job_id"]).values(override=ov))
                continue
            if row["status"] != "skipped" or row["status_reason"] != why:
                self._set(conn, row, "skipped", why, False, now)
                if row["rule_id"] in out["rules"]:
                    out["rules"][row["rule_id"]]["skipped"] += 1

    # ------------------------------------------------------------ raw IQ retention
    def prune(self) -> int:
        """Apply raw IQ retention (scheduling.retention: rule recordings follow their rule's policy)."""
        from .retention import Retention
        return len([d for d in Retention(self.s).apply() if not d.get("error")])

    # ------------------------------------------------------------ preview
    def preview(self, rule: dict[str, Any], hours: float = 48) -> dict[str, Any]:
        """What this rule would do over the next hours, without booking anything."""
        from ..satellites.passes import PassService
        loc = PassService(self.s).location()
        if loc is None:
            return {"passes": [], "warning": "Set the observatory location first (Settings → Observatory)."}
        now = self.s.clock()
        elements = predict.load_elements(self.s.config.data_root)
        passes = predict.predict_passes(elements, rule["satellites"], *loc, now, min(max(hours, 1), 240),
                                        float(rule["min_max_elevation_deg"]))
        rules = {r["rule_id"]: r for r in self.s.rules.list()}
        out = []
        with self.s.engine.connect() as conn:
            for p in passes:
                placed, reasons = self._place(conn, {**rule, "rule_id": rule.get("rule_id") or "preview"}, p, rules,
                                              True, now, self._match(conn, rule.get("rule_id") or "", p))
                dup = self._duplicate(conn, rule.get("rule_id") or "", p)
                out.append({**p, "receiver_id": placed[0].id if placed else None, "reasons": reasons,
                            "displaces": [d["job_id"] for d in placed[2]] if placed else [],
                            "duplicate_of": dup["job_id"] if dup is not None else None})
        return {"passes": out, "elements": predict.element_status(elements, rule["satellites"], now)}
