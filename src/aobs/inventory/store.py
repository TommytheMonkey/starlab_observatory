"""Persisted receiver inventory (tables inv_receivers / inv_events, schema 2).

Two kinds of rows:

  * origin "toml": imported from [scheduler.receivers.*] in observatory.toml. The file still owns
    their settings (the cutover scripts and `aobs schedule set-mode` edit it), so they are
    re-synced from the file whenever the broker or a CLI tool starts. They were already recording
    before the inventory existed, so they stay `in_service` (schedulable) while untested.
  * origin "ui": added from discovered hardware in the web UI. Owned by the database.

Readiness is never inferred from configuration or USB presence. It changes only when a hardware
test finishes (inventory.service.on_test_capture): verified when real IQ samples were captured
and checked, failed otherwise. It is bound to the receiver's hardware identity (host, driver,
serial, USB port identity): change any of those and the receiver is untested again.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any, Callable

from sqlalchemy import Engine, select

from ..config import Config
from ..db.schema import inv_events, inv_receivers
from ..timeutil import utc_now
from ..scheduling.hardware import Receiver, load_receivers, receiver_from_config

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")
EDITABLE = ("name", "notes", "enabled")


class InventoryError(RuntimeError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def identity_hash(host: str, driver: str, serial: str, identity: str, usb_path: str | None, soapy: str = "") -> str:
    key = json.dumps([host, driver, serial, identity, usb_path if identity == "usb_path" else "", soapy])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def identity_key(identity: str, serial: str, usb_path: str | None) -> str:
    return f"port:{usb_path or ''}" if identity == "usb_path" else f"sn:{serial}"


def row_identity(row: dict[str, Any]) -> str:
    return identity_hash(row["host"], row["driver"], row["serial"], row["identity"], row.get("usb_path"),
                         (row.get("config") or {}).get("soapy_driver", ""))


def effective_readiness(row: dict[str, Any]) -> tuple[str, bool]:
    """(readiness, stale): evidence for a different hardware identity does not count."""
    det = row.get("readiness_detail") or {}
    if row["readiness"] in ("verified", "failed", "simulated") and det.get("identity_hash") != row_identity(row):
        return "untested", True
    return row["readiness"], False


def lifecycle_state(readiness: str) -> str:
    """discovered (on USB, not in the inventory) -> configured (in the inventory) -> verified (hardware evidence).
    A failed or stale test leaves a receiver configured; `readiness` says why."""
    return {"verified": "verified", "simulated": "simulated"}.get(readiness, "configured")


def _public(row: dict[str, Any]) -> dict[str, Any]:
    d = {k: (v.isoformat() if isinstance(v, dt.datetime) else v) for k, v in row.items()}
    d["readiness"], d["readiness_stale"] = effective_readiness(row)
    d["state"] = lifecycle_state(d["readiness"])
    return d


class Inventory:
    def __init__(self, engine: Engine, clock: Callable[[], dt.datetime] = utc_now):
        self.engine, self.clock = engine, clock

    # ------------------------------------------------------------ reading
    def rows(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(inv_receivers).order_by(inv_receivers.c.id)).mappings()]

    def get(self, rid: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            r = conn.execute(select(inv_receivers).where(inv_receivers.c.id == rid)).mappings().first()
        return dict(r) if r else None

    def public(self, rid: str) -> dict[str, Any]:
        row = self.get(rid)
        if row is None:
            raise InventoryError(f"no receiver {rid}", 404)
        return _public(row)

    def listing(self) -> list[dict[str, Any]]:
        return [_public(r) for r in self.rows()]

    def events(self, rid: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(inv_events).where(inv_events.c.receiver_id == rid)
                                .order_by(inv_events.c.id.desc()).limit(limit)).mappings().all()
        return [{**dict(r), "at": r["at"].isoformat()} for r in rows]

    def receivers(self) -> dict[str, Receiver]:
        """Enabled receivers as the scheduler sees them."""
        out = {}
        for row in self.rows():
            if not row["enabled"] or row["host"] != "local":
                continue                       # remote receivers: discovered and configured, not recorded yet
            readiness, _ = effective_readiness(row)
            cfg = dict(row["config"])
            out[row["id"]] = receiver_from_config(
                row["id"], {**cfg, "driver": row["driver"], "serial": row["serial"]}, name=row["name"],
                origin=row["origin"], readiness=readiness, in_service=row["origin"] == "toml",
                soapy_driver=cfg.get("soapy_driver", ""), caps=dict(row.get("capabilities") or {}),
                identity=row["identity"], usb_path=row.get("usb_path") or "")
        return out

    # ------------------------------------------------------------ writing
    def _event(self, conn: Any, rid: str, actor: str, action: str, detail: dict[str, Any] | None = None) -> None:
        conn.execute(inv_events.insert().values(receiver_id=rid, at=self.clock(), actor=actor[:128], action=action,
                                                detail=detail))

    def sync_from_toml(self, config: Config, actor: str = "observatory.toml") -> dict[str, list[str]]:
        """Import or refresh the TOML receivers. The file owns their settings; UI rows are never touched."""
        declared = load_receivers(config)
        raw = config.section("scheduler").get("receivers", {})
        now = self.clock()
        done: dict[str, list[str]] = {"imported": [], "updated": [], "retired": []}
        with self.engine.begin() as conn:
            existing = {r["id"]: dict(r) for r in conn.execute(select(inv_receivers)).mappings()}
            for rid, rx in declared.items():
                cfg = {k: v for k, v in raw[rid].items() if k not in ("driver", "serial")}
                clash = next((r for r in existing.values() if r["id"] != rid and r["serial"] == rx.serial
                              and r["driver"] == rx.driver and r["host"] == "local"), None)
                if clash is not None:
                    raise InventoryError(f"observatory.toml declares {rid} with serial {rx.serial}, which is already "
                                         f"receiver {clash['id']} in the inventory; remove one of them")
                row = existing.get(rid)
                if row is not None and row["origin"] != "toml":
                    raise InventoryError(f"observatory.toml declares {rid}, but {rid} was added in the web UI; "
                                         "rename one of them")
                vals = {"driver": rx.driver, "serial": rx.serial, "config": cfg, "enabled": True,
                        "identity_key": identity_key("serial", rx.serial, None)}
                if row is None:
                    conn.execute(inv_receivers.insert().values(
                        id=rid, name=rid, host="local", identity="serial", usb_vid=None, usb_pid=None, usb_path=None,
                        manufacturer=None, model=rx.hardware or None, origin="toml", capabilities=None,
                        readiness="untested", readiness_detail=None, revision=1, updated_by=actor,
                        created_at=now, updated_at=now, **vals))
                    self._event(conn, rid, actor, "imported", {"from": "observatory.toml", "driver": rx.driver,
                                                               "serial": rx.serial, "mode": rx.mode})
                    done["imported"].append(rid)
                elif any(row[k] != v for k, v in vals.items()) or row["model"] != (rx.hardware or None):
                    changed = sorted(k for k, v in vals.items() if row[k] != v)
                    conn.execute(inv_receivers.update().where(inv_receivers.c.id == rid).values(
                        model=rx.hardware or None, revision=row["revision"] + 1, updated_by=actor, updated_at=now,
                        **vals))
                    self._event(conn, rid, actor, "updated", {"from": "observatory.toml", "fields": changed})
                    done["updated"].append(rid)
            for rid, row in existing.items():
                if row["origin"] == "toml" and rid not in declared and row["enabled"]:
                    conn.execute(inv_receivers.update().where(inv_receivers.c.id == rid).values(
                        enabled=False, revision=row["revision"] + 1, updated_by=actor, updated_at=now))
                    self._event(conn, rid, actor, "disabled", {"reason": "no longer in observatory.toml"})
                    done["retired"].append(rid)
        return done

    def create(self, *, rid: str, name: str, driver: str, serial: str, identity: str, usb_path: str | None,
               usb_vid: str | None, usb_pid: str | None, manufacturer: str | None, model: str | None,
               config: dict[str, Any], capabilities: dict[str, Any] | None, actor: str, host: str = "local",
               identification: dict[str, Any] | None = None) -> dict[str, Any]:
        if not ID_RE.match(rid):
            raise InventoryError("receiver ID: 2-32 letters, digits, - or _, starting with a letter or digit", 400)
        now = self.clock()
        with self.engine.begin() as conn:
            if conn.execute(select(inv_receivers.c.id).where(inv_receivers.c.id == rid)).first():
                raise InventoryError(f"there is already a receiver {rid}")
            key = identity_key(identity, serial, usb_path)
            other = conn.execute(select(inv_receivers.c.id).where(
                inv_receivers.c.host == host, inv_receivers.c.driver == driver,
                inv_receivers.c.identity_key == key)).first()
            if other:
                what = f"serial {serial}" if identity == "serial" else f"the device in USB port {usb_path}"
                raise InventoryError(f"{what} on {host} is already receiver {other.id}")
            conn.execute(inv_receivers.insert().values(
                id=rid, name=name[:120] or rid, host=host, driver=driver, serial=serial, identity=identity,
                identity_key=key, identification=identification,
                usb_vid=usb_vid, usb_pid=usb_pid, usb_path=usb_path, manufacturer=manufacturer, model=model,
                origin="ui", enabled=True, config=config, capabilities=capabilities, readiness="untested",
                readiness_detail=None, revision=1, updated_by=actor, created_at=now, updated_at=now))
            self._event(conn, rid, actor, "created", {"host": host, "driver": driver, "serial": serial,
                                                      "identity": identity, "usb_path": usb_path, "model": model})
        return self.public(rid)

    def update(self, rid: str, changes: dict[str, Any], revision: int, actor: str) -> dict[str, Any]:
        bad = set(changes) - set(EDITABLE) - {"notes"}
        if bad:
            raise InventoryError(f"these fields cannot be changed here: {sorted(bad)}", 400)
        now = self.clock()
        with self.engine.begin() as conn:
            row = conn.execute(select(inv_receivers).where(inv_receivers.c.id == rid)).mappings().first()
            if row is None:
                raise InventoryError(f"no receiver {rid}", 404)
            if row["revision"] != revision:
                raise InventoryError(f"{rid} was changed elsewhere (revision {row['revision']}, you had {revision}); "
                                     "reload and try again")
            if row["origin"] == "toml" and set(changes) - {"name"}:
                raise InventoryError(f"{rid} is configured in observatory.toml; change it there", 409)
            vals: dict[str, Any] = {}
            cfg = dict(row["config"])
            if "name" in changes:
                vals["name"] = str(changes["name"] or rid).strip()[:120] or rid
            if "notes" in changes:
                cfg["notes"] = str(changes["notes"] or "")[:2000]
                vals["config"] = cfg
            if "enabled" in changes:
                vals["enabled"] = bool(changes["enabled"])
            conn.execute(inv_receivers.update().where(inv_receivers.c.id == rid).values(
                revision=row["revision"] + 1, updated_by=actor, updated_at=now, **vals))
            action = ("enabled" if vals.get("enabled") else "disabled") if "enabled" in vals and \
                vals["enabled"] != row["enabled"] else "updated"
            self._event(conn, rid, actor, action, {"fields": sorted(changes)})
        return self.public(rid)

    def delete(self, rid: str, revision: int, actor: str) -> None:
        with self.engine.begin() as conn:
            row = conn.execute(select(inv_receivers).where(inv_receivers.c.id == rid)).mappings().first()
            if row is None:
                raise InventoryError(f"no receiver {rid}", 404)
            if row["origin"] == "toml":
                raise InventoryError(f"{rid} is configured in observatory.toml; remove it there")
            if row["revision"] != revision:
                raise InventoryError(f"{rid} was changed elsewhere; reload and try again")
            conn.execute(inv_receivers.delete().where(inv_receivers.c.id == rid))
            self._event(conn, rid, actor, "deleted", {"serial": row["serial"], "driver": row["driver"]})

    def set_capabilities(self, rid: str, caps: dict[str, Any], actor: str) -> None:
        now = self.clock()
        with self.engine.begin() as conn:
            row = conn.execute(select(inv_receivers).where(inv_receivers.c.id == rid)).mappings().first()
            if row is None:
                raise InventoryError(f"no receiver {rid}", 404)
            conn.execute(inv_receivers.update().where(inv_receivers.c.id == rid).values(
                capabilities=caps, revision=row["revision"] + 1, updated_by=actor, updated_at=now))
            self._event(conn, rid, actor, "identified", {"source": caps.get("source")})

    def set_identification(self, rid: str, ident: dict[str, Any], actor: str) -> None:
        """What the vendor tools reported (identify). Informational: never changes readiness or capabilities."""
        now = self.clock()
        with self.engine.begin() as conn:
            row = conn.execute(select(inv_receivers).where(inv_receivers.c.id == rid)).mappings().first()
            if row is None:
                raise InventoryError(f"no receiver {rid}", 404)
            conn.execute(inv_receivers.update().where(inv_receivers.c.id == rid).values(
                identification=ident, revision=row["revision"] + 1, updated_by=actor, updated_at=now))
            self._event(conn, rid, actor, "identified", {"tools": [s.get("tool") for s in ident.get("steps", [])]})

    def record_readiness(self, rid: str, ok: bool, detail: dict[str, Any], actor: str, simulated: bool = False) -> None:
        """verified/failed from a hardware test; a simulated device can only ever become "simulated"."""
        now = self.clock()
        with self.engine.begin() as conn:
            row = conn.execute(select(inv_receivers).where(inv_receivers.c.id == rid)).mappings().first()
            if row is None:
                return
            detail = {**detail, "identity_hash": row_identity(dict(row)), "at": now.isoformat()}
            state = ("simulated" if simulated else "verified") if ok else "failed"
            conn.execute(inv_receivers.update().where(inv_receivers.c.id == rid).values(
                readiness=state, readiness_detail=detail, revision=row["revision"] + 1, updated_by=actor,
                updated_at=now))
            self._event(conn, rid, actor, "readiness", {"readiness": state, "test_id": detail.get("test_id"),
                                                        "job_id": detail.get("job_id"),
                                                        "problems": detail.get("problems")})
