"""Configuration export and import.

An export is one JSON file with everything the owner configured: the settings documents, the
signal-path layout (antennas, parts, chains, switch ports) and the receivers added in the web
UI. It never contains secrets (credentials stay in files on the server, and any key that looks
like one is dropped as a second line of defence), nor hardware evidence: readiness is proven
again on the hardware it applies to.

An import is validated completely before anything is written (dry run first, by default).
Receivers are never created by an import: a receiver must be plugged in and identified, so the
report lists them to be re-added from the Receivers page.
"""

from __future__ import annotations

import re
from typing import Any

from ..db.schema import SCHEMA_VERSION
from .store import DOCS, SettingsError

EXPORT_FORMAT = 1
SECRET_KEY = re.compile(r"token|secret|password|passwd|private_key|credential|api_?key|cookie|session", re.I)


def scrub(value: Any, path: str = "", dropped: list[str] | None = None) -> Any:
    """Copy without any key that looks like a secret; record what was dropped."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if SECRET_KEY.search(str(k)):
                if dropped is not None:
                    dropped.append(f"{path}{k}")
                continue
            out[k] = scrub(v, f"{path}{k}.", dropped)
        return out
    if isinstance(value, list):
        return [scrub(v, path, dropped) for v in value]
    return value


def export(s: Any) -> dict[str, Any]:
    settings = {}
    for name in DOCS:
        cur = s.config_store.current(name)
        if cur is not None:
            settings[name] = {"version": cur["version"], "data": cur["data"]}
    rf = getattr(s, "rf", None)
    layout = None
    if rf is not None:
        cur = rf.current or rf.load()
        layout = {"version": cur["version"], "layout": cur["layout"]}
    receivers, toml_owned = [], []
    for r in s.inventory.rows():
        if r["origin"] != "ui":
            toml_owned.append(r["id"])
            continue
        receivers.append({k: r[k] for k in ("id", "name", "host", "driver", "serial", "identity", "usb_path", "usb_vid",
                                            "usb_pid", "manufacturer", "model", "enabled", "config", "capabilities")})
    dropped: list[str] = []
    body = scrub({"settings": settings, "signal_paths": layout, "receivers": receivers}, "", dropped)
    return {"starlab_export": EXPORT_FORMAT, "schema_version": SCHEMA_VERSION, "exported_at": s.clock().isoformat(),
            **body,
            "not_included": ["secrets (credentials stay in files on the server)",
                             "hardware test evidence (readiness is proven again on the hardware)",
                             f"receivers configured in observatory.toml ({', '.join(toml_owned) or 'none'}): "
                             "they come with that file",
                             "recordings, tests and job history (they stay in the database and its backups)"]
            + ([f"keys that looked like secrets: {', '.join(dropped)}"] if dropped else [])}


def import_config(s: Any, payload: Any, author: str, dry_run: bool = True) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("starlab_export") != EXPORT_FORMAT:
        raise SettingsError("this is not a Starlab configuration export (format 1)")
    if int(payload.get("schema_version") or 0) > SCHEMA_VERSION:
        raise SettingsError(f"this export comes from a newer Starlab (schema {payload['schema_version']}); "
                            "update this installation first")
    payload = scrub(payload)
    report: dict[str, Any] = {"settings": {}, "signal_paths": None, "receivers": [], "errors": []}
    store = s.config_store
    settings = payload.get("settings") or {}
    for name, doc in settings.items():
        if name not in DOCS:
            report["errors"].append(f"unknown settings document {name!r}")
            continue
        res = store.save(name, (doc or {}).get("data"), None, author, dry_run=True)
        report["settings"][name] = res
        report["errors"] += res.get("errors") or []
    rf = getattr(s, "rf", None)
    sp = payload.get("signal_paths")
    if sp is not None and rf is not None:
        from ..scheduling.store import SchedulerError
        try:
            res = rf.save(sp.get("layout"), None, author, "imported", dry_run=True)
        except SchedulerError as exc:
            res = {"ok": False, "errors": [str(exc)]}
        cur = (rf.current or rf.load())["layout"]
        report["signal_paths"] = {"ok": res.get("ok"), "errors": res.get("errors", []),
                                  "unchanged": bool(res.get("ok")) and _norm_layout(rf, sp.get("layout")) == cur}
        report["errors"] += [f"signal paths: {e}" for e in res.get("errors", [])]
    existing = {r["id"]: r for r in s.inventory.rows()}
    for r in payload.get("receivers") or []:
        rid = r.get("id")
        same = existing.get(rid)
        if same and same["serial"] == r.get("serial"):
            report["receivers"].append({"id": rid, "action": "already here"})
        else:
            report["receivers"].append({"id": rid, "action": "re-add from Receivers when it is plugged in",
                                        "model": r.get("model"), "serial": r.get("serial")})
    report["ok"] = not report["errors"]
    if dry_run or not report["ok"]:
        report["dry_run"] = True
        return report
    applied = []
    for name, doc in settings.items():
        res = store.save(name, doc["data"], None, author, "imported", config=s.config)
        if not res.get("unchanged"):
            applied.append(f"{DOCS[name].label} → version {res['version']}")
    if sp is not None and rf is not None and not report["signal_paths"]["unchanged"]:
        res = rf.save(sp["layout"], None, author, "imported")
        if not res.get("unchanged"):
            applied.append(f"signal paths → version {res['version']}")
    report.update(dry_run=False, applied=applied)
    return report


def _norm_layout(rf: Any, layout: Any) -> Any:
    from ..scheduling.rflayout import validate
    norm, errors, _ = validate(layout, rf.s)
    return None if errors else norm
