"""Versioned settings documents (table settings_docs).

observatory.toml remains the bootstrap file (paths, database, bind address, legacy integration,
and anything the cutover scripts edit). The settings below moved to the database:

  * seeded once from the TOML on the first start after the upgrade, never overwritten with
    defaults afterwards;
  * every save is a new version (author, note, time); restoring an old version appends a copy;
  * validated as a whole before anything is written, so a bad value cannot replace a good one;
  * applied over the TOML values in memory at start and on save, so the rest of the code reads
    them where it always did. A field marked "restart" is read once at broker start.

No field holds a secret. Credentials stay in files referenced by the bootstrap TOML.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import re
import zoneinfo
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import Engine, select

from ..config import Config
from ..db.schema import settings_docs
from ..timeutil import utc_now

LOG = logging.getLogger("aobs.settings")

NOTIFICATION_KINDS = ("scheduled", "recording", "completed", "failed", "partial", "missed", "preempted", "skipped",
                      "attempt_completed", "attempt_failed", "health", "calendar", "rf_layout", "session")


class SettingsError(RuntimeError):
    def __init__(self, message: str, status: int = 400, errors: list[str] | None = None):
        super().__init__(message)
        self.status, self.errors = status, errors or [message]


@dataclass(frozen=True)
class Field:
    key: str
    kind: str                         # text | int | float | bool | tz | path | url | kinds
    label: str
    toml: tuple[str, str] | None = None    # the TOML value this overrides (section, key)
    applies: str = "now"              # now | restart
    default: Any = None
    nullable: bool = False
    lo: float | None = None
    hi: float | None = None
    max_len: int = 200
    help: str = ""
    choices: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "label": self.label, "applies": self.applies,
                "nullable": self.nullable, "min": self.lo, "max": self.hi, "help": self.help,
                "toml": ".".join(self.toml) if self.toml else None,
                **({"choices": list(NOTIFICATION_KINDS)} if self.kind == "kinds" else {}),
                **({"choices": list(self.choices)} if self.kind == "enum" else {})}


@dataclass(frozen=True)
class Doc:
    name: str
    label: str
    description: str
    fields: tuple[Field, ...]
    hidden: bool = False                       # internal (setup progress): not on the Settings page or in exports


DOCS: dict[str, Doc] = {d.name: d for d in (
    Doc("observatory", "Observatory", "Who and where this station is.", (
        Field("name", "text", "Station name", ("site", "name"), default="Observatory", max_len=80),
        Field("description", "text", "Description", default="", max_len=500),
        Field("timezone", "tz", "Display time zone", ("site", "display_timezone"), default="America/New_York",
              help="Times are stored in UTC; this is only how they are shown (web pages, calendar, Slack)."),
        Field("latitude", "float", "Latitude (°, north positive)", nullable=True, lo=-90, hi=90,
              help="Stored only in this station's database, never in the repository. Needed to predict passes."),
        Field("longitude", "float", "Longitude (°, east positive)", nullable=True, lo=-180, hi=180),
        Field("elevation_m", "float", "Elevation (m above sea level)", nullable=True, lo=-500, hi=9000),
    )),
    Doc("storage", "Storage", "Where recordings go, how long their raw IQ is kept, and how much space is kept free.", (
        Field("default_output_root", "path", "Manual recordings", ("scheduler", "default_output_root"),
              default="/data/observatory/jobs"),
        Field("auto_output_root", "path", "Automatic recordings", ("scheduler", "auto_output_root"),
              default="/data/observatory/auto"),
        Field("test_output_root", "path", "Signal-path tests", ("scheduler", "test_output_root"), nullable=True,
              help="Empty: a 'tests' folder next to the manual recordings."),
        Field("processing_root", "path", "Decodes and analyses of legacy and imported recordings",
              ("scheduler", "processing_root"), nullable=True, applies="restart",
              help="Empty: 'processing' under the data folder. Other recordings keep their results next to the recording."),
        Field("retention_enabled", "bool", "Delete raw IQ by the policies below", ("scheduler", "retention_enabled"),
              default=True, help="Off: nothing is deleted automatically; the Recordings page still shows what would be. "
                                 "Rules with their own retention follow it; recordings marked Keep never expire. "
                                 "Logs, plots, images and the metadata sidecar are always kept."),
        Field("auto_raw_policy", "enum", "Automatic recordings: raw IQ", ("scheduler", "auto_raw_policy"),
              default="hours", choices=("keep", "hours", "after_decode"),
              help="keep: forever. hours: for the hours below, counted from the end of the recording. "
                   "after_decode: until a decode produces an image."),
        Field("auto_retention_hours", "int", "Keep raw IQ of automatic recordings (hours)",
              ("scheduler", "auto_retention_hours"), default=48, lo=1, hi=8760),
        Field("manual_raw_policy", "enum", "Manual recordings: raw IQ", ("scheduler", "manual_raw_policy"),
              default="keep", choices=("keep", "hours", "after_decode")),
        Field("manual_raw_hours", "int", "Keep raw IQ of manual recordings (hours)", ("scheduler", "manual_raw_hours"),
              default=720, lo=1, hi=87600),
        Field("test_raw_policy", "enum", "Signal-path tests: raw IQ", ("scheduler", "test_raw_policy"),
              default="keep", choices=("keep", "hours")),
        Field("test_raw_hours", "int", "Keep raw IQ of signal-path tests (hours)", ("scheduler", "test_raw_hours"),
              default=720, lo=1, hi=87600),
        Field("reserve_free_gib", "int", "Always keep free (GiB)", ("scheduler", "reserve_free_gib"), default=64,
              lo=0, hi=1_000_000, help="A job is refused if it would leave less than this free on top of its own minimum."),
    )),
    Doc("notifications", "Notifications", "What reaches Slack. Signal-path tests are always quiet.", (
        Field("disabled_kinds", "kinds", "Do not send", ("scheduler", "notifications_disabled"), default=[]),
    )),
    Doc("integrations", "Integrations", "Optional services. Credentials stay in files on the server and are never "
        "shown or exported.", (
        Field("calendar_enabled", "bool", "Google Calendar", ("calendar", "enabled"), default=False),
        Field("calendar_id", "text", "Calendar ID", ("calendar", "calendar_id"), default="", max_len=300),
        Field("publish_jobs_to_calendar", "bool", "Publish recordings to the calendar", ("scheduler", "calendar"),
              default=False),
        Field("antenna_switch_url", "url", "Antenna switch board address", ("antenna_switch", "base_url"),
              applies="restart", default="", nullable=True),
    )),
    Doc("slack", "Slack", "Optional. Starlab works fully without Slack. Credentials are entered separately, stored in "
        "the secrets folder, and never shown or exported.", (
        Field("enabled", "bool", "Enable Slack integration", default=False),
        Field("mode", "enum", "Connection", default="socket", choices=("socket", "http"),
              help="socket: Starlab connects out to Slack; no public address needed (recommended). http: Slack calls "
                   "a public HTTPS address of this server, verified with the Signing Secret."),
        Field("app_name", "text", "App name (in the manifest)", default="Starlab", max_len=35),
        Field("public_base_url", "url", "Public HTTPS address (HTTP mode only)", default="", nullable=True),
        Field("channel_id", "text", "Channel ID for notifications", default="", max_len=20,
              help="In Slack: open the channel → its name → the ID at the bottom (C…). Invite the app to it."),
        Field("notify_upcoming", "bool", "Upcoming passes", default=False),
        Field("upcoming_minutes", "int", "Announce passes this many minutes ahead", default=15, lo=1, hi=240),
        Field("notify_recording_start", "bool", "Recording started", default=False),
        Field("notify_completed", "bool", "Recording completed", default=True),
        Field("notify_failed", "bool", "Recording failed, missed or partial", default=True),
        Field("notify_receiver_offline", "bool", "Receiver offline or back", default=True),
        Field("notify_storage", "bool", "Storage running low", default=True),
        Field("notify_decoded_images", "bool", "Decoded images (posts the images)", default=True),
        Field("commands_enabled", "bool", "Slash command", default=True),
        Field("command", "text", "Command name", default="starlab", max_len=30),
        Field("recording_commands", "bool", "Allow record and cancel from Slack", default=False,
              help="Only for the users below."),
        Field("authorized_users", "text", "Authorized users (Slack user IDs, comma-separated)", default="", max_len=500,
              help="Your ID: Slack → your profile → ⋮ → Copy member ID (U…)."),
        Field("rate_limit_per_minute", "int", "Commands per user per minute", default=20, lo=1, hi=120),
    )),
    Doc("simulation", "Simulation", "Explore Starlab without receivers. Simulated devices and their recordings are "
        "labelled everywhere; their tests never count as hardware verification.", (
        Field("enabled", "bool", "Offer simulated receivers", default=False,
              help="Adds simulated devices to Receivers → Discovered hardware. Real receivers are unaffected."),
    )),
    Doc("satellites", "Satellites", "Which satellites to predict passes for, and from where (Observatory location).", (
        Field("selected", "norads", "Satellites (NORAD IDs)", default=[57166, 59051]),
        Field("min_elevation_deg", "float", "Ignore passes peaking below (°)", default=20, lo=0, hi=90),
        Field("refresh_online", "bool", "Download current orbital elements", default=False,
              help="Off: the sample elements shipped with Starlab are used, and pass times drift by minutes after a "
                   "few weeks. On: refreshed when you press Refresh, and automatically every few hours (below)."),
        Field("elements_source", "enum", "Orbital-element source", default="celestrak_omm",
              choices=("celestrak_omm", "celestrak_tle", "custom"),
              help="celestrak_omm: CelesTrak OMM (JSON), falling back to TLE. celestrak_tle: TLE only. custom: the "
                   "address below."),
        Field("custom_url", "url", "Custom source address", default="",
              help="Must contain {norad}, replaced by each satellite's catalogue number."),
        Field("custom_format", "enum", "Custom source format", default="tle", choices=("tle", "omm")),
        Field("refresh_hours", "int", "Refresh automatically every (hours)", default=12, lo=1, hi=168,
              help="Only while downloading is on. Every refresh recalculates future passes."),
        Field("stale_days", "float", "Warn when elements are older than (days)", default=14, lo=1, hi=60),
    )),
    Doc("scheduling", "Scheduling", "How recordings get booked.", (
        Field("mode", "enum", "Scheduling", default="manual", choices=("manual", "automatic", "hybrid"),
              help="Manual: you book recordings; enabled rules only show the passes they would record (PREDICTED). "
                   "Automatic/hybrid: enabled rules book their passes, and you can still book, change or skip any "
                   "recording by hand (your changes are never overwritten). Nothing records automatically until a "
                   "verified receiver is named in an enabled rule."),
        Field("horizon_hours", "int", "Book automatic passes this far ahead (hours)", default=48, lo=6, hi=240),
    )),
    Doc("setup", "Setup", "First-run wizard progress.", (
        Field("completed", "bool", "Completed", default=False),
        Field("step", "text", "Current step", default="welcome", max_len=40),
        Field("done", "steps", "Done steps", default=[]),
        Field("skipped", "steps", "Skipped steps", default=[]),
    ), hidden=True),
    Doc("ui", "Interface", "Defaults for the web pages (per-browser things like unsaved drafts stay in the browser).", (
        Field("default_test_duration_s", "int", "Default signal-test length (s)", default=60, lo=5, hi=600),
    )),
)}


def _get(raw: dict[str, Any], path: tuple[str, str]) -> Any:
    return (raw.get(path[0]) or {}).get(path[1])


def validate(doc: str, data: Any, allowed_roots: list[str] | None = None) -> tuple[dict[str, Any], list[str]]:
    """Normalized document and errors. Unknown keys are errors (a typo must not be silently dropped)."""
    if doc not in DOCS:
        return {}, [f"unknown settings document {doc!r}"]
    if not isinstance(data, dict):
        return {}, [f"{doc}: expected an object"]
    d = DOCS[doc]
    errors = [f"{doc}.{k}: unknown setting" for k in data if k not in {f.key for f in d.fields}]
    out: dict[str, Any] = {}
    for f in d.fields:
        v = data.get(f.key, f.default)
        name = f"{doc}.{f.key} ({f.label})"
        if v is None or (v == "" and f.nullable and f.kind not in ("text",)):
            if not f.nullable and f.kind not in ("text", "url"):
                errors.append(f"{name} is required")
            out[f.key] = None if f.nullable else v
            continue
        try:
            if f.kind == "text":
                v = str(v).strip()
                if len(v) > f.max_len:
                    errors.append(f"{name}: at most {f.max_len} characters")
            elif f.kind == "bool":
                if not isinstance(v, bool):
                    errors.append(f"{name}: must be true or false")
            elif f.kind in ("int", "float"):
                if isinstance(v, bool):
                    raise ValueError
                v = int(v) if f.kind == "int" and float(v).is_integer() else float(v)
                if f.kind == "int" and not isinstance(v, int):
                    errors.append(f"{name}: must be a whole number")
                elif not math.isfinite(v):
                    errors.append(f"{name}: must be a finite number")
                elif (f.lo is not None and v < f.lo) or (f.hi is not None and v > f.hi):
                    errors.append(f"{name}: must be between {f.lo:g} and {f.hi:g}")
            elif f.kind == "tz":
                v = str(v).strip()
                zoneinfo.ZoneInfo(v)
            elif f.kind == "path":
                v = str(v).strip().rstrip("/") or "/"
                if not v.startswith("/") or ".." in v.split("/"):
                    errors.append(f"{name}: must be an absolute path without '..'")
                elif allowed_roots and not any(v == r.rstrip("/") or v.startswith(r.rstrip("/") + "/")
                                               for r in allowed_roots):
                    errors.append(f"{name}: must be under one of the allowed output roots {allowed_roots} "
                                  "(set in observatory.toml)")
            elif f.kind == "url":
                v = str(v).strip()
                if v and not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+(/\S*)?", v):
                    errors.append(f"{name}: must be an http:// or https:// address")
            elif f.kind == "enum":
                if v not in f.choices:
                    errors.append(f"{name}: one of {', '.join(f.choices)}")
            elif f.kind == "norads":
                if not isinstance(v, list) or len(v) > 200:
                    raise ValueError
                v = sorted({int(x) for x in v})
                if any(not 0 < x < 10**9 for x in v):
                    errors.append(f"{name}: NORAD catalogue numbers are positive whole numbers")
            elif f.kind == "steps":
                if not isinstance(v, list) or any(not isinstance(x, str) or len(x) > 40 for x in v):
                    raise ValueError
                v = list(dict.fromkeys(v))
            elif f.kind == "kinds":
                if not isinstance(v, list) or any(k not in NOTIFICATION_KINDS for k in v):
                    errors.append(f"{name}: a list of {', '.join(NOTIFICATION_KINDS)}")
                else:
                    v = sorted(set(v))
        except (TypeError, ValueError, zoneinfo.ZoneInfoNotFoundError):
            errors.append(f"{name}: {v!r} is not a valid {f.kind}")
        out[f.key] = v
    return out, errors


class SettingsStore:
    def __init__(self, engine: Engine, clock: Callable[[], dt.datetime] = utc_now):
        self.engine, self.clock = engine, clock
        self.toml_values: dict[str, dict[str, Any]] = {}       # what the file said, before the database overrode it
        self.allowed_roots: list[str] = []
        self.on_change: list[Callable[[str, dict[str, Any]], None]] = []

    # ------------------------------------------------------------ reading
    def current(self, doc: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            r = conn.execute(select(settings_docs).where(settings_docs.c.doc == doc)
                             .order_by(settings_docs.c.version.desc()).limit(1)).mappings().first()
        return self._row(r) if r else None

    def history(self, doc: str, limit: int = 50) -> list[dict[str, Any]]:
        self._known(doc)
        with self.engine.connect() as conn:
            rows = conn.execute(select(settings_docs).where(settings_docs.c.doc == doc)
                                .order_by(settings_docs.c.version.desc()).limit(limit)).mappings().all()
        out = [self._row(r) for r in rows]
        for i, r in enumerate(out):
            prev = out[i + 1]["data"] if i + 1 < len(out) else None
            r["changes"] = diff(prev, r["data"]) if prev is not None else ["first version"]
        return out

    def version(self, doc: str, version: int) -> dict[str, Any]:
        self._known(doc)
        with self.engine.connect() as conn:
            r = conn.execute(select(settings_docs).where(settings_docs.c.doc == doc,
                                                         settings_docs.c.version == version)).mappings().first()
        if r is None:
            raise SettingsError(f"{doc} has no version {version}", 404)
        return self._row(r)

    @staticmethod
    def _row(r: Any) -> dict[str, Any]:
        return {"doc": r["doc"], "version": r["version"], "data": r["data"], "author": r["author"],
                "note": r["note"], "saved_at": r["created_at"].isoformat()}

    @staticmethod
    def _known(doc: str) -> None:
        if doc not in DOCS:
            raise SettingsError(f"unknown settings document {doc!r}; known: {', '.join(DOCS)}", 404)

    def describe(self, include_hidden: bool = False) -> dict[str, Any]:
        out = {}
        for name, d in DOCS.items():
            if d.hidden and not include_hidden:
                continue
            cur = self.current(name)
            differs = []
            for f in d.fields:
                if f.toml and cur is not None:
                    file_value = self.toml_values.get(name, {}).get(f.key)
                    if file_value is not None and file_value != cur["data"].get(f.key):
                        differs.append({"key": f.key, "toml": ".".join(f.toml), "file": file_value,
                                        "database": cur["data"].get(f.key)})
            out[name] = {"label": d.label, "description": d.description, "fields": [f.public() for f in d.fields],
                         "current": cur, "toml_differs": differs}
        return out

    # ------------------------------------------------------------ seeding and applying
    def seed(self, config: Config, actor: str = "seed (observatory.toml)") -> list[str]:
        """First start after the upgrade: copy the TOML's values in. Existing documents are never touched."""
        self.allowed_roots = list(config.section("scheduler").get("allowed_output_roots") or [])
        seeded = []
        for name, d in DOCS.items():
            self.toml_values[name] = {f.key: _get(config.raw, f.toml) for f in d.fields if f.toml}
            if self.current(name) is not None:
                continue
            data = {f.key: (_get(config.raw, f.toml) if f.toml and _get(config.raw, f.toml) is not None else f.default)
                    for f in d.fields}
            if name == "setup" and (config.legacy_enabled or config.section("scheduler").get("receivers")):
                data.update(completed=True, step="finish")     # an existing station: never send it into the wizard
            norm, errors = validate(name, data)          # roots unchecked: the file's own values are kept as they are
            if errors:
                LOG.warning("seeding %s from observatory.toml: %s; storing the file's values unvalidated", name, errors)
                norm = data
            self._insert(name, norm, actor, "initial values from observatory.toml")
            seeded.append(name)
        return seeded

    def apply(self, config: Config) -> None:
        """Override the TOML's values in memory with the current documents."""
        for name in DOCS:
            cur = self.current(name)
            if cur is not None:
                self._apply_doc(config, name, cur["data"])

    @staticmethod
    def _apply_doc(config: Config, name: str, data: dict[str, Any]) -> None:
        for f in DOCS[name].fields:
            if not f.toml:
                continue
            sec = config.raw.setdefault(f.toml[0], {})
            if data.get(f.key) is None:
                sec.pop(f.toml[1], None)
            else:
                sec[f.toml[1]] = data[f.key]

    # ------------------------------------------------------------ writing
    def _insert(self, doc: str, data: dict[str, Any], author: str, note: str | None) -> dict[str, Any]:
        now = self.clock()
        with self.engine.begin() as conn:
            last = conn.execute(select(settings_docs.c.version).where(settings_docs.c.doc == doc)
                                .order_by(settings_docs.c.version.desc()).limit(1)).scalar()
            version = (last or 0) + 1
            conn.execute(settings_docs.insert().values(doc=doc, version=version, data=data, author=author[:128],
                                                       note=(note or None) and note[:500], created_at=now))
        return {"doc": doc, "version": version, "data": data, "author": author, "note": note, "saved_at": now.isoformat()}

    def save(self, doc: str, data: Any, base_version: int | None, author: str, note: str = "",
             dry_run: bool = False, config: Config | None = None) -> dict[str, Any]:
        self._known(doc)
        norm, errors = validate(doc, data, self.allowed_roots)
        cur = self.current(doc)
        if errors:
            if dry_run:
                return {"ok": False, "errors": errors}
            raise SettingsError(f"{doc} was not saved: " + "; ".join(errors), 400, errors)
        if base_version is not None and cur is not None and base_version != cur["version"]:
            raise SettingsError(f"{DOCS[doc].label} changed since you loaded it (now version {cur['version']}); "
                                "reload and redo the change", 409)
        changes = diff(cur["data"] if cur else None, norm)
        restart = sorted({f.label for f in DOCS[doc].fields
                          if f.applies == "restart" and cur and cur["data"].get(f.key) != norm.get(f.key)})
        if dry_run:
            return {"ok": True, "errors": [], "changes": changes, "needs_restart": restart}
        if cur is not None and cur["data"] == norm:
            return {"ok": True, "unchanged": True, "version": cur["version"], "changes": [], "needs_restart": []}
        row = self._insert(doc, norm, author, note)
        if config is not None:
            self._apply_doc(config, doc, norm)
        for fn in self.on_change:
            try:
                fn(doc, norm)
            except Exception:  # noqa: BLE001 - persistence succeeded; a live-apply hook must not undo that
                LOG.exception("applying %s live failed; it applies at the next restart", doc)
        LOG.info("settings %s v%s saved by %s", doc, row["version"], author)
        return {"ok": True, "version": row["version"], "saved_at": row["saved_at"], "changes": changes,
                "needs_restart": restart}

    def restore(self, doc: str, version: int, author: str, config: Config | None = None) -> dict[str, Any]:
        old = self.version(doc, version)
        return self.save(doc, old["data"], None, author, f"restored version {version}", config=config)


def diff(before: dict[str, Any] | None, after: dict[str, Any]) -> list[str]:
    if before is None:
        return ["first version"]
    out = []
    for k in sorted(set(before) | set(after)):
        if before.get(k) != after.get(k):
            out.append(f"{k}: {before.get(k)!r} → {after.get(k)!r}")
    return out
