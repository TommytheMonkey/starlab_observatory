"""First-run setup wizard: steps, persisted progress, validation, finish.

Progress is the hidden `setup` settings document, so the wizard resumes where it was left, in any
browser, after a restart. Every value the wizard sets goes through the normal stores (settings
documents, receiver inventory), so everything stays editable afterwards. Finishing never enables
automatic recording.
"""

from __future__ import annotations

import os
import shutil
import stat
from typing import Any

from .store import SettingsError

STEPS: tuple[tuple[str, str, bool], ...] = (       # id, label, optional
    ("welcome", "Welcome", False),
    ("identity", "Observatory", False),
    ("hosts", "Receiver hosts", True),
    ("discovery", "Hardware discovery", True),
    ("receivers", "Receivers", True),
    ("satellites", "Satellites", True),
    ("scheduling", "Scheduling", True),
    ("integrations", "Integrations", True),
    ("validation", "Validation", False),
    ("finish", "Finish", False),
)
IDS = [s[0] for s in STEPS]


class Setup:
    def __init__(self, s: Any):
        self.s = s

    def _state(self) -> dict[str, Any]:
        cur = self.s.config_store.current("setup")
        return dict((cur or {}).get("data") or {"completed": False, "step": "welcome", "done": [], "skipped": []})

    def _save(self, data: dict[str, Any], actor: str, note: str) -> dict[str, Any]:
        self.s.config_store.save("setup", data, None, actor, note, config=self.s.config)
        return self.state()

    def state(self) -> dict[str, Any]:
        st = self._state()
        steps = []
        for sid, label, optional in STEPS:
            state = "done" if sid in st["done"] else "skipped" if sid in st["skipped"] else \
                "current" if sid == st["step"] else "todo"
            steps.append({"id": sid, "label": label, "optional": optional, "state": state})
        return {"completed": bool(st["completed"]), "step": st["step"], "steps": steps}

    def step(self, sid: str, action: str, actor: str) -> dict[str, Any]:
        if sid not in IDS:
            raise SettingsError(f"unknown setup step {sid!r}", 400)
        st = self._state()
        optional = dict((s[0], s[2]) for s in STEPS)[sid]
        if action == "skip" and not optional:
            raise SettingsError(f"{sid} cannot be skipped", 400)
        if action == "done" and sid == "identity":
            obs = (self.s.config_store.current("observatory") or {}).get("data") or {}
            if not str(obs.get("name") or "").strip():
                raise SettingsError("give the observatory a name first", 400)
        if action in ("done", "skip"):
            key, other = ("done", "skipped") if action == "done" else ("skipped", "done")
            st[key] = list(dict.fromkeys([*st[key], sid]))
            st[other] = [x for x in st[other] if x != sid]
            nxt = IDS[min(IDS.index(sid) + 1, len(IDS) - 1)]
            st["step"] = nxt
        elif action == "goto":
            st["step"] = sid
        else:
            raise SettingsError("action must be done, skip or goto", 400)
        return self._save(st, actor, f"setup: {sid} {action}")

    def reopen(self, actor: str) -> dict[str, Any]:
        st = self._state()
        st.update(completed=False, step="welcome")
        return self._save(st, actor, "setup reopened")

    # ------------------------------------------------------------ validation
    def validate(self) -> dict[str, Any]:
        from ..db.migrate import current_version
        from ..db.schema import SCHEMA_VERSION
        checks: list[dict[str, Any]] = []

        def add(name: str, level: str, detail: str) -> None:
            checks.append({"check": name, "level": level, "detail": detail})

        v = current_version(self.s.engine)
        add("Database", "pass" if v == SCHEMA_VERSION else "fail", f"schema {v} (this version needs {SCHEMA_VERSION})")
        sched = self.s.config.section("scheduler")
        reserve = float(sched.get("reserve_free_gib", 5))
        for key in ("default_output_root", "auto_output_root"):
            path = str(sched.get(key) or "")
            target = path
            while target and not os.path.exists(target):
                target = os.path.dirname(target)
            ok = bool(target) and os.access(target, os.W_OK)
            free = shutil.disk_usage(target).free / 2**30 if target else 0
            level = "fail" if not ok else "warn" if free < reserve + 1 else "pass"
            add(f"Storage: {key.replace('_', ' ')}", level,
                f"{path}: {'writable' if ok else 'NOT writable by the Starlab user'}, {free:.0f} GiB free "
                f"(keeps {reserve:.0f} GiB free)")
        from ..satellites.passes import PassService
        ps = PassService(self.s)
        if ps.location() is None:
            add("Location", "warn", "latitude/longitude not set: pass prediction is off until they are")
        else:
            add("Location", "pass", "set (stored in this station's database only)")
            pred = ps.predicted(48)
            n = len(pred["passes"])
            stale = [e for e in pred["satellites"]["elements"] if e.get("warning")]
            add("Pass prediction", "pass" if n and not stale else "warn",
                f"{n} pass(es) in the next 48 h for {len(pred['satellites']['selected'])} satellite(s)"
                + (f"; {stale[0]['warning']}" if stale else ""))
        rows = self.s.inventory.listing()
        if not rows:
            add("Receivers", "warn", "none configured yet: add one from Receivers (or a simulated one) at any time")
        for r in rows:
            label = {"verified": ("pass", "verified by a hardware test"),
                     "simulated": ("info", "simulated: tested with synthetic samples only, no hardware"),
                     "failed": ("warn", "configured; its last hardware test failed"),
                     }.get(r["readiness"], ("warn" if r["origin"] == "ui" else "info",
                                            "configured; not yet verified by a hardware test"))
            add(f"Receiver {r['id']}", label[0], f"{r['model'] or r['driver']}: {label[1]}"
                + ("" if r["enabled"] else " (disabled)"))
        auto = [rid for rid, rx in self.s.receivers.items() if rx.auto.get("enabled")]
        add("Automatic recording", "info", f"enabled on {', '.join(auto)}" if auto else
            "off on every receiver; nothing records until you book it or enable it later")
        sec = self.s.config.secrets_dir
        try:
            mode = stat.S_IMODE(sec.stat().st_mode)
            add("Secrets folder", "pass" if mode & 0o077 == 0 else "warn",
                f"{sec} is {'private' if mode & 0o077 == 0 else f'readable by others (mode {mode:o}); chmod 700 it'}")
        except OSError:
            add("Secrets folder", "warn", f"{sec} is missing")
        host = str(self.s.config.section("broker").get("host", "127.0.0.1"))
        add("Network", "pass" if host in ("127.0.0.1", "localhost", "::1") else "warn",
            f"listening on {host}" + ("" if host in ("127.0.0.1", "localhost", "::1") else
                                      ": reachable from other machines without HTTPS; use a VPN or a reverse proxy"))
        cal = self.s.config.section("calendar")
        if cal.get("enabled"):
            add("Google Calendar", "info", "enabled; it starts publishing after the next restart if its credentials "
                                           "are in place")
        return {"ok": not any(c["level"] == "fail" for c in checks), "checks": checks}

    def finish(self, actor: str) -> dict[str, Any]:
        res = self.validate()
        if not res["ok"]:
            raise SettingsError("setup cannot finish while a check fails: " + "; ".join(
                f"{c['check']}: {c['detail']}" for c in res["checks"] if c["level"] == "fail"), 409)
        st = self._state()
        st.update(completed=True, step="finish", done=list(dict.fromkeys([*st["done"], "validation", "finish"])))
        return self._save(st, actor, "setup finished")
