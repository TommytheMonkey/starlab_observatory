"""Decoder profiles (brief 8): which SatDump pipeline, with which settings, decodes which satellites and
transmission modes.

* A profile names a pipeline, the satellites (NORAD IDs) and/or transmission modes (the first word of the
  catalog's mode, e.g. LRPT) it applies to, and optional decode settings: target frequency, fine-search
  offsets, shift method and extra SatDump parameters. The settings are checked with the same rules as a
  decode request, so a profile can never ask for something the processor would refuse.
* `auto_decode` makes decoding the default for new rules and bookings of those satellites. A rule or
  booking still decides for itself; changing a profile never rewrites what is already booked.
* Decode requests without a pipeline use the best matching profile (a satellite match beats a mode match,
  then the higher priority). Each attempt records the profile id and revision it used.
* Every save is a new revision, kept in decoder_profile_versions; a stale revision is refused (409).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import secrets
from typing import Any

from sqlalchemy import and_, select

from ..db.schema import decoder_profile_versions, decoder_profiles
from ..logjson import log_event
from ..satellites import predict
from .store import SchedulerError

LOG = logging.getLogger("aobs.decoders")
SAFE_PARAM = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:+-]{0,64}$")
PIPELINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,31}$")
RESERVED = ("samplerate", "baseband_format", "freq_shift")
READ_ONLY = ("profile_id", "revision", "created_by", "updated_by", "created_at", "updated_at", "deleted_at", "installed",
             "warnings")          # what the store returns besides the profile; ignored when a profile is sent back
DEFAULTS: dict[str, Any] = {"name": "", "enabled": True, "pipeline": "", "satellites": [], "modes": [],
                            "auto_decode": False, "priority": 50, "frequency_hz": None, "search_offsets_hz": None,
                            "shift_method": None, "additional_parameters": {}, "notes": ""}


class ProfileError(SchedulerError):
    def __init__(self, message: str, status: int = 400, errors: list[str] | None = None):
        super().__init__(message, status)
        self.errors = errors or [message]


def mode_key(text: str | None) -> str | None:
    """The transmission mode of a catalog entry: its first word (\"LRPT (QPSK, 72 kbit/s) …\" -> LRPT)."""
    word = (text or "").strip().split(" ")[0].strip(",;:")
    return word.upper() or None


def recording_identity(rec: dict[str, Any]) -> tuple[int | None, str | None]:
    """(NORAD ID, transmission mode) of a recording, from its request or the satellite catalog."""
    spec = rec.get("spec") or {}
    tgt, meta = spec.get("target") or {}, spec.get("metadata") or {}
    norad = tgt.get("norad_id") or meta.get("norad")
    try:
        norad = int(norad) if norad is not None else None
    except (TypeError, ValueError):
        norad = None
    if norad is None:          # older bookings name the satellite only
        name = (rec.get("target") or tgt.get("name") or "").upper().replace("-", " ")
        norad = next((n for n, c in predict.CATALOG.items() if c["name"].upper().replace("-", " ") == name), None)
    mode = tgt.get("mode") or meta.get("mode") or (predict.CATALOG.get(norad) or {}).get("mode")
    return norad, mode_key(mode)


def validate_profile(data: Any, pipelines: frozenset[str] | set[str]) -> tuple[dict[str, Any], list[str], list[str]]:
    """(normalized profile, errors, warnings). Unknown keys are errors; an uninstalled pipeline is a warning
    (the profile is kept but not used until SatDump has that pipeline)."""
    if not isinstance(data, dict):
        return dict(DEFAULTS), ["a profile must be an object"], []
    errors = [f"unknown profile field {k!r}" for k in data if k not in DEFAULTS and k not in READ_ONLY]
    warnings: list[str] = []
    p = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
    p["name"] = str(p["name"] or "").strip()
    if not 1 <= len(p["name"]) <= 80:
        errors.append("give the profile a name (at most 80 characters)")
    for k in ("enabled", "auto_decode"):
        if not isinstance(p[k], bool):
            errors.append(f"{k} must be true or false")
    p["pipeline"] = str(p["pipeline"] or "").strip()
    if not p["pipeline"] or not PIPELINE_RE.match(p["pipeline"]):
        errors.append("choose a SatDump pipeline (letters, digits, _ . and - only)")
    elif p["pipeline"] not in pipelines:
        warnings.append(f"SatDump pipeline {p['pipeline']!r} is not installed here; the profile is not used until it is")
    try:
        p["satellites"] = sorted({int(n) for n in (p["satellites"] or [])})
        if any(not 0 < n < 1_000_000 for n in p["satellites"]):
            raise ValueError
    except (TypeError, ValueError):
        errors.append("satellites must be a list of NORAD IDs")
        p["satellites"] = []
    modes = p["modes"] or []
    if not isinstance(modes, list) or not all(isinstance(m, str) and MODE_RE.match(m.strip()) for m in modes):
        errors.append("modes must be a list of short names such as LRPT or APT")
        modes = []
    p["modes"] = sorted({m.strip().upper() for m in modes})
    if not p["satellites"] and not p["modes"]:
        errors.append("name at least one satellite or transmission mode the profile applies to")
    if isinstance(p["priority"], bool) or not isinstance(p["priority"], int) or not 1 <= p["priority"] <= 100:
        errors.append("priority must be a whole number 1-100")
    if p["frequency_hz"] not in (None, ""):
        if isinstance(p["frequency_hz"], bool) or not isinstance(p["frequency_hz"], (int, float)) \
                or not 1e6 <= p["frequency_hz"] <= 1e11:
            errors.append("frequency must be in Hz (1 MHz - 100 GHz) or empty")
        else:
            p["frequency_hz"] = int(p["frequency_hz"])
    else:
        p["frequency_hz"] = None
    s = p["search_offsets_hz"]
    if s not in (None, "", []):
        if not (isinstance(s, list) and 1 <= len(s) <= 15 and all(isinstance(o, int) and not isinstance(o, bool)
                                                                    and abs(o) <= 20_000 for o in s)):
            errors.append("fine-search offsets must be a list of 1-15 whole numbers within ±20000 Hz")
    else:
        p["search_offsets_hz"] = None
    if p["shift_method"] in ("", None):
        p["shift_method"] = None
    elif p["shift_method"] not in ("internal", "satdump", "none"):
        errors.append("shift method must be internal, satdump or none")
    if p["search_offsets_hz"] and p["shift_method"] not in (None, "internal"):
        errors.append("fine-search offsets need the internal shift method")
    extra = p["additional_parameters"] or {}
    if not isinstance(extra, dict):
        errors.append("additional parameters must be name: value pairs")
        extra = {}
    for k, v in extra.items():
        if k in RESERVED or not SAFE_PARAM.match(str(k)) or not isinstance(v, (bool, int, float, str)) \
                or (isinstance(v, str) and not SAFE_VALUE.match(v)):
            errors.append(f"additional parameter {k!r} is not allowed")
    p["additional_parameters"] = extra
    p["notes"] = str(p["notes"] or "")[:2000]
    return p, errors, warnings


def decode_params(profile: dict[str, Any]) -> dict[str, Any]:
    """The decode request a profile stands for (only the settings it actually sets)."""
    out: dict[str, Any] = {"pipeline": profile["pipeline"]}
    if profile.get("frequency_hz"):
        out["target_frequency_hz"] = int(profile["frequency_hz"])
    for k in ("search_offsets_hz", "shift_method"):
        if profile.get(k):
            out[k] = profile[k]
    if profile.get("additional_parameters"):
        out["additional_parameters"] = dict(profile["additional_parameters"])
    return out


class ProfileStore:
    def __init__(self, s: Any):
        self.s = s

    @staticmethod
    def _out(row: Any) -> dict[str, Any]:
        d = dict(row)
        for k in ("created_at", "updated_at", "deleted_at"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        data = d.pop("data") or {}
        return {**DEFAULTS, **data, **{k: d[k] for k in ("profile_id", "name", "enabled", "revision", "created_by",
                                                          "updated_by", "created_at", "updated_at", "deleted_at")}}

    def list(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        q = select(decoder_profiles)
        if not include_deleted:
            q = q.where(decoder_profiles.c.deleted_at.is_(None))
        with self.s.engine.connect() as conn:
            rows = conn.execute(q.order_by(decoder_profiles.c.created_at)).mappings().all()
        out = [self._out(r) for r in rows]
        for p in out:
            p["installed"] = p["pipeline"] in self.s.pipelines
        return out

    def get(self, profile_id: str, include_deleted: bool = False) -> dict[str, Any]:
        with self.s.engine.connect() as conn:
            row = conn.execute(select(decoder_profiles).where(decoder_profiles.c.profile_id == profile_id)).mappings().first()
        if row is None or (row["deleted_at"] is not None and not include_deleted):
            raise ProfileError(f"no decoder profile {profile_id}", 404)
        return {**self._out(row), "installed": row["data"].get("pipeline") in self.s.pipelines}

    def history(self, profile_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.s.engine.connect() as conn:
            rows = conn.execute(select(decoder_profile_versions).where(decoder_profile_versions.c.profile_id == profile_id)
                                .order_by(decoder_profile_versions.c.id.desc()).limit(limit)).mappings().all()
        return [{**dict(r), "at": r["at"].isoformat()} for r in rows]

    def check(self, data: Any, profile_id: str | None = None) -> dict[str, Any]:
        p, errors, warnings = validate_profile(data, self.s.pipelines)
        if not errors and any(o["name"].lower() == p["name"].lower() for o in self.list() if o["profile_id"] != profile_id):
            errors.append(f"another profile is already called {p['name']!r}")
        return {"ok": not errors, "errors": errors, "warnings": warnings, "profile": p}

    def _version(self, conn: Any, pid: str, rev: int, action: str, data: dict[str, Any], author: str,
                 now: dt.datetime) -> None:
        conn.execute(decoder_profile_versions.insert().values(profile_id=pid, revision=rev, action=action, data=data,
                                                              author=author, at=now))

    def create(self, data: Any, author: str, action: str = "created") -> dict[str, Any]:
        res = self.check(data)
        if not res["ok"]:
            raise ProfileError("the profile was not saved: " + "; ".join(res["errors"]), 400, res["errors"])
        p, now = res["profile"], self.s.clock()
        pid = "d" + secrets.token_hex(5)
        body = {k: v for k, v in p.items() if k not in ("name", "enabled")}
        with self.s.engine.begin() as conn:
            conn.execute(decoder_profiles.insert().values(profile_id=pid, name=p["name"], enabled=p["enabled"], revision=1,
                                                          data=body, created_by=author, updated_by=author,
                                                          created_at=now, updated_at=now))
            self._version(conn, pid, 1, action, p, author, now)
        log_event(LOG, "decoder profile created", profile_id=pid, name=p["name"], pipeline=p["pipeline"], by=author)
        return {**self.get(pid), "warnings": res["warnings"]}

    def update(self, profile_id: str, data: Any, revision: int | None, author: str) -> dict[str, Any]:
        cur = self.get(profile_id)
        if revision is None or int(revision) != cur["revision"]:
            raise ProfileError(f"{cur['name']} was changed elsewhere (now revision {cur['revision']}); reload it and "
                               "make your change again", 409)
        res = self.check(data, profile_id)
        if not res["ok"]:
            raise ProfileError("the profile was not saved: " + "; ".join(res["errors"]), 400, res["errors"])
        p, now = res["profile"], self.s.clock()
        body = {k: v for k, v in p.items() if k not in ("name", "enabled")}
        with self.s.engine.begin() as conn:
            n = conn.execute(decoder_profiles.update().where(and_(decoder_profiles.c.profile_id == profile_id,
                                                                  decoder_profiles.c.revision == cur["revision"]))
                             .values(name=p["name"], enabled=p["enabled"], data=body, revision=cur["revision"] + 1,
                                     updated_by=author, updated_at=now)).rowcount
            if n != 1:
                raise ProfileError(f"{cur['name']} was changed elsewhere; reload it", 409)
            self._version(conn, profile_id, cur["revision"] + 1, "updated", p, author, now)
        log_event(LOG, "decoder profile updated", profile_id=profile_id, name=p["name"], by=author)
        return {**self.get(profile_id), "warnings": res["warnings"]}

    def delete(self, profile_id: str, revision: int | None, author: str) -> dict[str, Any]:
        cur = self.get(profile_id)
        if revision is None or int(revision) != cur["revision"]:
            raise ProfileError(f"{cur['name']} was changed elsewhere (now revision {cur['revision']}); reload it", 409)
        now = self.s.clock()
        with self.s.engine.begin() as conn:
            conn.execute(decoder_profiles.update().where(decoder_profiles.c.profile_id == profile_id)
                         .values(deleted_at=now, enabled=False, revision=cur["revision"] + 1, updated_by=author,
                                 updated_at=now))
            self._version(conn, profile_id, cur["revision"] + 1, "deleted",
                          {k: cur[k] for k in DEFAULTS}, author, now)
        log_event(LOG, "decoder profile deleted", profile_id=profile_id, name=cur["name"], by=author)
        return {"profile_id": profile_id, "deleted": True}

    def seed(self, author: str = "Starlab (built-in decoders)") -> list[str]:
        """Once per database: one profile per pipeline in the satellite catalog, describing what Starlab already
        did (METEOR LRPT with the 1 kHz fine search). Never re-created after the owner deletes it."""
        with self.s.engine.connect() as conn:
            if conn.execute(select(decoder_profile_versions.c.id).limit(1)).first() is not None:
                return []
        by_pipe: dict[str, list[int]] = {}
        for n, c in predict.CATALOG.items():
            if c.get("pipeline"):
                by_pipe.setdefault(c["pipeline"], []).append(n)
        made = []
        for pipe, norads in sorted(by_pipe.items()):
            modes = sorted({m for m in (mode_key(predict.CATALOG[n].get("mode")) for n in norads) if m})
            names = sorted({predict.CATALOG[n]["name"].split(" ")[0] for n in norads})
            data = {**DEFAULTS, "name": f"{' / '.join(names)} {' / '.join(modes)}".strip(), "pipeline": pipe,
                    "satellites": norads, "modes": modes, "auto_decode": False,
                    "notes": "Created by Starlab from its satellite catalog. METEOR decodes search ±6 kHz in 1 kHz steps "
                             "by default, because Doppler and receiver error exceed SatDump's carrier lock range."}
            res = self.check(data)
            if res["errors"]:
                LOG.warning("built-in decoder profile %s not created: %s", pipe, res["errors"])
                continue
            made.append(self.create(res["profile"], author, action="seeded")["profile_id"])
        return made

    # ------------------------------------------------------------ choosing a profile
    def match(self, rec: dict[str, Any], pipeline: str | None = None) -> dict[str, Any] | None:
        """The profile that decodes this recording: enabled, installed, applying to its satellite (best) or its
        transmission mode; restricted to `pipeline` when given. Ties go to the higher priority, then the older."""
        norad, mode = recording_identity(rec)
        best: tuple[int, int, str] | None = None
        chosen = None
        for p in self.list():
            if not p["enabled"] or not p["installed"] or (pipeline and p["pipeline"] != pipeline):
                continue
            score = 2 if norad is not None and norad in p["satellites"] else (1 if mode and mode in p["modes"] else 0)
            if not score:
                continue
            key = (score, p["priority"], p["created_at"])
            if best is None or key[:2] > best[:2]:
                best, chosen = key, p
        return chosen

    def for_satellite(self, norad: int) -> dict[str, Any] | None:
        """The profile new rules and bookings of this satellite default to."""
        return self.match({"spec": {"target": {"norad_id": norad}}})
