"""RF signal paths: which antenna, filters, amplifiers and other parts feed each receiver.

The owner rearranges hardware often and edits the layout in the drag-and-drop page (/rf). Every
save is a new, append-only version in `rf_layouts`. The current version is authoritative for the
broker's receivers (antenna, signal_path, amplifier, filters, and the ANTSW-01 port map), so job
validation, /radio-status, the calendar and the agent all describe the hardware as it really is.

Each recording keeps the path it was made with in `spec.resolved.rf_path`:
  * broker recordings: the current layout at the moment the capture starts;
  * legacy-recorder files: the layout version in effect at the recording's start time.
Recordings made before the first layout version have no rf_path; nothing is back-filled.

Layout document:
  components: {id: {id, name, kind, status, power, model, notes, gain_db, min_mhz, max_mhz, ports?}}
  receivers:  {id: {id, chain: [component ids, antenna side first], notes, status?, managed, label?, host?, serial?}}
A switch component (ANTSW-01) heads its receiver's chain; its `ports` map port number -> antenna id.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import re
import threading
from typing import Any, Callable

from sqlalchemy import and_, select

from ..hardware.antsw import BOARD_ANTENNA_MAX as BOARD_NAME_MAX, BOARD_LABEL_MAX  # ANTSW-01 firmware limits
from ..db.schema import rf_layouts, sched_jobs
from .store import SchedulerError

LOG = logging.getLogger("aobs.rflayout")

KINDS = ("antenna", "filter", "amplifier", "switch", "splitter", "attenuator", "cable", "other")
STATUSES = ("working", "suspect", "broken", "unknown")
POWER = ("none", "external", "bias_tee")
SWITCH_ID = "ANTSW-01"
SWITCH_PORTS = 8
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.+-]{0,47}$")   # switch-port antennas have names like "ADS Airband"
_RX_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").upper()[:48]
    return s or "PART"


def _iso(t: Any) -> Any:
    return t.isoformat() if hasattr(t, "isoformat") else t


# ---------------------------------------------------------------- seed from the config
def seed_from_config(s: Any) -> dict[str, Any]:
    """Version 1: the receivers, antennas, amplifiers and filters the TOML config already describes."""
    comps: dict[str, dict[str, Any]] = {}

    def add(cid: str, name: str, kind: str, **kw: Any) -> str:
        if cid not in comps:
            comps[cid] = {"id": cid, "name": name, "kind": kind, "status": "unknown", "power": "none",
                          "model": "", "notes": "", "gain_db": None, "min_mhz": None, "max_mhz": None, **kw}
        return cid

    for aid, a in (s.config.section("antennas") or {}).items():
        add(aid, a.get("description") or aid, "antenna",
            min_mhz=a["min_hz"] / 1e6 if a.get("min_hz") else None, max_mhz=a["max_hz"] / 1e6 if a.get("max_hz") else None)
    ports: dict[str, str | None] = {str(n): None for n in range(1, SWITCH_PORTS + 1)}
    for n, p in sorted(s.switch_ports.items()):
        add(str(p["antenna"]), str(p["antenna"]), "antenna", notes=p.get("label", ""))
        ports[str(n)] = str(p["antenna"])
    receivers: dict[str, dict[str, Any]] = {}
    placed = set(ports.values())
    for rx in s.receivers.values():
        chain: list[str] = []
        if rx.antenna == SWITCH_ID:
            add(SWITCH_ID, "ANTSW-01 antenna switch", "switch", ports=ports, power="external")
            chain.append(SWITCH_ID)
        elif rx.antenna and rx.antenna != "UNSPECIFIED":
            chain.append(add(rx.antenna, rx.antenna, "antenna"))
        for f in rx.filters:
            if rx.amplifier and ("(in the" in f.lower() or f.lower() in rx.amplifier.lower()):
                continue                     # a filter built into the amplifier (SAWbird) is not a separate part
            chain.append(add(_slug(f) + "-" + rx.id, f, "filter"))
        if rx.amplifier:
            chain.append(add(_slug(rx.amplifier), rx.amplifier, "amplifier",
                             power="external" if rx.amplifier_power == "external" else
                             "bias_tee" if rx.amplifier_power == "bias_tee" else "none"))
        # the config may name one physical part on two receivers; the first keeps it, the owner sorts it out
        chain = [c for c in chain if c not in placed]
        placed.update(chain)
        if chain and comps[chain[0]]["kind"] not in ("antenna", "switch") and rx.antenna not in ("", "UNSPECIFIED"):
            chain = []
        receivers[rx.id] = {"id": rx.id, "chain": chain, "notes": ""}
    return {"components": comps, "receivers": receivers}


# ---------------------------------------------------------------- validation
def validate(layout: Any, s: Any) -> tuple[dict[str, Any], list[str], list[str]]:
    """Return (normalized layout, errors, warnings)."""
    errors: list[str] = []
    warns: list[str] = []
    if not isinstance(layout, dict):
        return {}, ["layout must be an object"], []
    comps_in = layout.get("components") or {}
    rxs_in = layout.get("receivers") or {}
    if not isinstance(comps_in, dict) or not isinstance(rxs_in, dict):
        return {}, ["components and receivers must be objects keyed by id"], []
    comps: dict[str, dict[str, Any]] = {}
    for cid, c in comps_in.items():
        if not isinstance(c, dict) or not _ID.match(str(cid)):
            errors.append(f"part id {cid!r} must be 1-48 letters, digits, spaces or _ . + -")
            continue
        name = str(c.get("name") or "").strip()[:80]
        kind = c.get("kind")
        if not name:
            errors.append(f"part {cid} needs a name")
        if kind not in KINDS:
            errors.append(f"part {cid}: kind must be one of {', '.join(KINDS)}")
            continue
        out = {"id": cid, "name": name or cid, "kind": kind,
               "status": c.get("status") if c.get("status") in STATUSES else "unknown",
               "power": c.get("power") if c.get("power") in POWER else "none",
               "model": str(c.get("model") or "")[:120], "notes": str(c.get("notes") or "")[:2000],
               # IDs this part had before a rename: tests recorded under them still belong to it
               "former_ids": [str(x) for x in (c.get("former_ids") or []) if _ID.match(str(x)) and str(x) != cid][:20]}
        for k in ("gain_db", "min_mhz", "max_mhz"):
            v = c.get(k)
            try:
                out[k] = None if v in (None, "") else float(v)
            except (TypeError, ValueError):
                errors.append(f"part {cid}: {k} must be a number")
                out[k] = None
        if out["min_mhz"] is not None and out["max_mhz"] is not None and out["min_mhz"] > out["max_mhz"]:
            errors.append(f"part {cid}: min_mhz is above max_mhz")
        if kind == "switch":
            raw_ports = c.get("ports") or {}
            out["ports"] = {str(n): (raw_ports.get(str(n)) or None) for n in range(1, SWITCH_PORTS + 1)}
            extra = set(map(str, raw_ports)) - set(out["ports"])
            if extra:
                errors.append(f"{cid}: ports must be 1-{SWITCH_PORTS} (got {sorted(extra)})")
        comps[cid] = out

    used: dict[str, str] = {}

    def place(cid: str, where: str) -> None:
        if cid in used:
            errors.append(f"{comps[cid]['name']} is in two places ({used[cid]} and {where}); one physical part, one place")
        used[cid] = where

    for c in list(comps.values()):
        if c["kind"] != "switch":
            continue
        on_board: dict[str, str] = {}
        for port, aid in c["ports"].items():
            if aid is None:
                continue
            if aid not in comps:
                errors.append(f"{c['id']} port {port}: unknown part {aid!r}")
            elif comps[aid]["kind"] != "antenna":
                errors.append(f"{c['id']} port {port}: only antennas go on switch ports, not {comps[aid]['name']}")
            elif len(board_id_or_none(aid) or aid) > BOARD_NAME_MAX:
                errors.append(f"{c['id']} port {port}: the switch board stores antenna names of at most {BOARD_NAME_MAX} "
                              f"characters; {aid!r} is {len(aid)}. Give that antenna a shorter ID.")
            elif board_id_or_none(aid) is None:
                errors.append(f"{c['id']} port {port}: {aid!r} has no characters the switch board can store")
            else:
                bid = board_id_or_none(aid)
                if bid in on_board and on_board[bid] != aid:
                    errors.append(f"{c['id']} port {port}: {aid!r} and {on_board[bid]!r} would both be stored on the switch "
                                  f"board as {bid!r}; rename one")
                on_board[bid] = aid
                place(aid, f"{c['id']} port {port}")

    receivers: dict[str, dict[str, Any]] = {}
    for rid, r in rxs_in.items():
        if not isinstance(r, dict) or not _RX_ID.match(str(rid)):
            errors.append(f"receiver id {rid!r} is not valid")
            continue
        managed = rid in s.receivers
        chain = [str(x) for x in (r.get("chain") or [])]
        out = {"id": rid, "chain": chain, "notes": str(r.get("notes") or "")[:2000], "managed": managed}
        if not managed:
            out.update(label=str(r.get("label") or rid)[:80], host=str(r.get("host") or "")[:120],
                       serial=str(r.get("serial") or "")[:64], hardware=str(r.get("hardware") or "")[:120],
                       status=r.get("status") if r.get("status") in STATUSES else "unknown")
        for i, cid in enumerate(chain):
            if cid not in comps:
                errors.append(f"{rid}: unknown part {cid!r}")
                continue
            c = comps[cid]
            place(cid, rid)
            if c["kind"] in ("antenna", "switch") and i != 0:
                errors.append(f"{rid}: {c['name']} is an {c['kind']} and must be first (the antenna end)")
        receivers[rid] = out
    for rid in s.receivers:
        if rid not in receivers:
            errors.append(f"receiver {rid} is missing; broker receivers cannot be removed here")
    if errors:
        return {}, errors, warns

    for rid, r in receivers.items():
        chain = [comps[c] for c in r["chain"]]
        head = chain[0] if chain else None
        if head is None or head["kind"] not in ("antenna", "switch"):
            warns.append(f"{rid} has no antenna in its path")
        rx = s.receivers.get(rid)
        cfg_switched = rx is not None and _config_switched(s, rid)
        if cfg_switched and (head is None or head["id"] != SWITCH_ID):
            errors.append(f"{rid} is driven through {SWITCH_ID} by the broker (leases, port selection); "
                          f"keep {SWITCH_ID} at the start of its path. Changing that needs a config change.")
        if not cfg_switched and any(c["id"] == SWITCH_ID for c in chain):
            errors.append(f"{SWITCH_ID} is controlled for {_switch_receiver(s)} only; it cannot feed {rid}")
        for c in chain:
            if c["status"] == "broken":
                warns.append(f"{rid}: {c['name']} is marked broken")
            elif c["status"] == "suspect":
                warns.append(f"{rid}: {c['name']} is marked suspect")
        if rx is not None:
            ext = [c for c in chain if c["power"] == "external" and c["kind"] == "amplifier"]
            needs = [c for c in chain if c["power"] == "bias_tee"]
            if rx.bias_tee_fixed is True and ext:
                warns.append(f"{rid}'s bias tee is permanently on and its DC would reach "
                             f"{', '.join(c['name'] for c in ext)}, which has its own supply")
            if needs and not rx.bias_tee_supported:
                warns.append(f"{rid} cannot supply bias-tee power, but {', '.join(c['name'] for c in needs)} needs it")
    if errors:
        return {}, errors, warns
    return {"components": comps, "receivers": receivers}, errors, warns


def _config_switched(s: Any, rid: str) -> bool:
    raw = (s.config.section("scheduler").get("receivers") or {}).get(rid) or {}
    return raw.get("antenna") == SWITCH_ID


def _switch_receiver(s: Any) -> str:
    return next((rid for rid in s.receivers if _config_switched(s, rid)), "OBS-R05")


# ---------------------------------------------------------------- derived receiver fields
def path_text(layout: dict[str, Any], rid: str, port_antenna: str | None = None, port: Any = None) -> str:
    comps = layout["components"]
    parts = []
    for cid in layout["receivers"][rid]["chain"]:
        c = comps[cid]
        if c["kind"] == "switch":
            if port_antenna:
                ant = comps.get(port_antenna, {}).get("name", port_antenna)
                parts += [ant, f"{c['name']} port {port}" if port else c["name"]]
            else:
                parts.append(f"{cid} port")
        else:
            parts.append(c["name"])
    return " > ".join(parts + [rid])


def derive(layout: dict[str, Any], rid: str) -> dict[str, Any]:
    comps = layout["components"]
    chain = [comps[c] for c in layout["receivers"][rid]["chain"]]
    head = chain[0] if chain else None
    amps = [c for c in chain if c["kind"] == "amplifier"]
    power = "external" if any(c["power"] == "external" for c in amps) else \
        "bias_tee" if any(c["power"] == "bias_tee" for c in amps) else ""
    return {"antenna": head["id"] if head and head["kind"] in ("antenna", "switch") else "UNSPECIFIED",
            "signal_path": path_text(layout, rid), "amplifier": "; ".join(c["name"] for c in amps),
            "amplifier_power": power, "filters": tuple(c["name"] for c in chain if c["kind"] == "filter")}


# ---------------------------------------------------------------- the service
class RfLayout:
    def __init__(self, s: Any, on_ports: Callable[[dict[int, dict[str, Any]]], None] | None = None,
                 board_read: Callable[[], dict[str, Any]] | None = None,
                 board_push: Callable[[dict[int, dict[str, Any]]], dict[str, Any]] | None = None):
        self.s = s
        self.on_ports = on_ports
        self.board_read, self.board_push = board_read, board_push
        self._lock = threading.RLock()
        self.current: dict[str, Any] | None = None

    # ---- storage
    def _rows(self, limit: int | None = None, before: dt.datetime | None = None) -> list[dict[str, Any]]:
        q = select(rf_layouts).order_by(rf_layouts.c.version.desc())
        if before is not None:
            q = q.where(rf_layouts.c.created_at <= before)
        if limit:
            q = q.limit(limit)
        with self.s.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings().all()]

    def load(self) -> dict[str, Any]:
        """At broker start: apply the latest version, or seed version 1 from the config."""
        with self._lock:
            rows = self._rows(1)
            if rows:
                self.current = rows[0]
                try:
                    norm, errors, _ = validate(self.current["layout"], self.s)
                    if errors:
                        raise ValueError("; ".join(errors))
                    self.current["layout"] = norm
                except ValueError as exc:
                    LOG.error("rf layout v%s no longer validates (%s); keeping the config's receivers",
                              self.current["version"], exc)
                    return self.current
            else:
                layout, errors, _ = validate(seed_from_config(self.s), self.s)
                if errors:
                    raise RuntimeError(f"cannot seed the RF layout from the config: {errors}")
                self.current = self._insert(layout, "seed (observatory.toml)", "initial layout from the config")
            self._apply(self.current["layout"])
            return self.current

    def _insert(self, layout: dict[str, Any], author: str, note: str) -> dict[str, Any]:
        now = self.s.clock()
        with self.s.engine.begin() as conn:
            last = conn.execute(select(rf_layouts.c.version).order_by(rf_layouts.c.version.desc()).limit(1)).scalar()
            version = (last or 0) + 1
            conn.execute(rf_layouts.insert().values(version=version, layout=layout, author=author[:128],
                                                    note=note or None, created_at=now))
        return {"version": version, "layout": layout, "author": author, "note": note, "created_at": now}

    # ---- applying to the broker
    def _apply(self, layout: dict[str, Any]) -> None:
        for rid, rx in list(self.s.receivers.items()):
            if rid in layout["receivers"]:
                self.s.receivers[rid] = dataclasses.replace(rx, **derive(layout, rid))
        sw = layout["components"].get(SWITCH_ID)
        if sw is not None:
            old = dict(self.s.switch_ports)
            ports = {int(n): {"port": int(n), "code": int(n) - 1, "antenna": aid,
                              "label": layout["components"][aid]["notes"] or layout["components"][aid]["name"]}
                     for n, aid in sw["ports"].items() if aid}
            self.s.switch_ports.clear()
            self.s.switch_ports.update(ports)
            if self.on_ports is not None and ports != old:
                try:
                    self.on_ports(ports)
                except Exception:  # noqa: BLE001
                    LOG.exception("updating the broker's switch ports failed")

    def _refresh_future_jobs(self, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
        """Future rows show the new path (calendar, status). R05 jobs follow an antenna to its new port."""
        notes: list[str] = []
        now = self.s.clock()
        changed = {rid for rid in after["receivers"] if rid in self.s.receivers and
                   (rid not in before.get("receivers", {}) or derive(before, rid) != derive(after, rid))}
        sw_before = (before.get("components", {}).get(SWITCH_ID) or {}).get("ports") or {}
        sw_after = (after["components"].get(SWITCH_ID) or {}).get("ports") or {}
        port_of = {aid: int(n) for n, aid in sw_after.items() if aid}
        if sw_before != sw_after:
            changed.add(_switch_receiver(self.s))
        if not changed:
            return notes
        with self.s.locked(), self.s.engine.begin() as conn:
            rows = conn.execute(select(sched_jobs.c.job_id, sched_jobs.c.receiver_id, sched_jobs.c.spec, sched_jobs.c.job_name)
                                .where(and_(sched_jobs.c.receiver_id.in_(sorted(changed)),
                                            sched_jobs.c.status.in_(("scheduled", "shadow")),
                                            sched_jobs.c.reservation_start > now))).all()
            for r in rows:
                spec = dict(r.spec or {})
                rxb = dict(spec.get("receiver") or {})
                rx = self.s.receivers[r.receiver_id]
                resolved = dict(spec.get("resolved") or {})
                if rx.switched:
                    ant = rxb.get("antenna")
                    old_port = resolved.get("switch_port")
                    new_port = port_of.get(str(ant)) if ant else None
                    if ant and new_port is None:
                        notes.append(f"{r.job_name} ({r.job_id}) uses {ant}, which is no longer on {SWITCH_ID}; "
                                     "cancel or reschedule it")
                    elif new_port is not None and new_port != old_port:
                        resolved["switch_port"] = new_port
                        notes.append(f"{r.job_name} ({r.job_id}): {ant} moved from port {old_port} to {new_port}; "
                                     "the job follows it")
                    rxb["signal_path"] = path_text(after, r.receiver_id, ant, resolved.get("switch_port")) if ant \
                        else rx.signal_path
                else:
                    rxb["antenna"], rxb["signal_path"] = rx.antenna, rx.signal_path
                spec["receiver"], spec["resolved"] = rxb, resolved
                conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == r.job_id).values(spec=spec, updated_at=now))
        return notes

    # ---- API
    def save(self, layout: Any, base_version: int | None, author: str, note: str = "",
             dry_run: bool = False) -> dict[str, Any]:
        with self._lock:
            norm, errors, warns = validate(layout, self.s)
            cur = self.current or {}
            if errors:
                if dry_run:
                    return {"ok": False, "errors": errors, "warnings": warns}
                raise SchedulerError("; ".join(errors), 400)
            if dry_run:
                return {"ok": True, "errors": [], "warnings": warns, "derived": self._derived(norm)}
            if base_version is not None and cur and base_version != cur["version"]:
                raise SchedulerError(f"the layout changed since you loaded it (now version {cur['version']}); "
                                     "reload and redo the change", 409)
            if cur and norm == cur.get("layout"):
                return {"ok": True, "unchanged": True, "version": cur["version"], "warnings": warns}
            before = cur.get("layout") or {"components": {}, "receivers": {}}
            self.current = self._insert(norm, author, note)
            self._apply(norm)
            jobs = self._refresh_future_jobs(before, norm)
            LOG.info("rf layout v%s saved by %s: %s", self.current["version"], author, note)
            summary = diff_summary(before, norm)
            board = None
            # Starlab is authoritative: send whenever the board disagrees, not only when this save renamed a port
            # (a layout seeded from the config, or a board edited by hand, would otherwise never be corrected).
            if _switch_names(before) != _switch_names(norm) or self.board_status().get("in_sync") is False:
                board = self._push_board()
                summary.append(board["text"])
            if jobs or summary:
                with self.s.engine.begin() as conn:
                    self.s.notify(conn, "rf_layout", None,
                                  f"Signal paths updated (v{self.current['version']}, {author})"
                                  + (f": {note}" if note else "") + "\n" + "\n".join(summary[:14] + jobs[:12]))
            return {"ok": True, "version": self.current["version"], "warnings": warns, "jobs": jobs, "changes": summary,
                    "switch_board": board}

    def set_lane(self, rid: str, entry: dict[str, Any], author: str, note: str) -> int | None:
        """Inventory changes (receiver added, removed): one lane, without pushing to the switch board."""
        with self._lock:
            cur = self.current or self.load()
            layout = json.loads(json.dumps(cur["layout"]))
            layout["receivers"][rid] = {**layout["receivers"].get(rid, {"id": rid, "chain": [], "notes": ""}), **entry}
            norm, errors, _ = validate(layout, self.s)
            if errors:
                raise SchedulerError("; ".join(errors), 400)
            if norm == cur["layout"]:
                return None
            self.current = self._insert(norm, author, note)
            self._apply(norm)
            return int(self.current["version"])

    # ---- the ANTSW-01 board's own port names (antswitch.local)
    def _push_board(self) -> dict[str, Any]:
        if self.board_push is None:
            return {"pushed": False, "text": f"{SWITCH_ID} board: not connected to this broker; names not sent"}
        try:
            r = dict(self.board_push(dict(self.s.switch_ports)))
        except Exception as exc:  # noqa: BLE001 - the layout is saved either way; say what the board did not get
            LOG.exception("pushing port names to %s failed", SWITCH_ID)
            return {"pushed": False, "error": str(exc),
                    "text": f"{SWITCH_ID} board NOT updated ({exc}); use 'Send to the switch' in the editor to retry"}
        if r.get("verified"):
            ports = ", ".join(map(str, r.get("changed_ports") or [])) or "none changed"
            r["text"] = (f"{SWITCH_ID} board updated and read back (ports {ports}); hardware codes and the selected "
                         "port unchanged")
        else:
            r["text"] = (f"{SWITCH_ID} board was sent the names but reads back differently: {r.get('differences')}"
                         + ("" if r.get("codes_unchanged", True) else "; its port codes CHANGED, check the board")
                         + ("" if r.get("selected_port_unchanged", True) else
                            f"; its selected port CHANGED (now {r.get('selected_port')}), check the board"))
        if r.get("stored_as"):
            r["text"] += "; stored on the board as " + ", ".join(f"port {n}: {v}" for n, v in r["stored_as"].items()) \
                         + " (the board allows only letters, digits, _ . -)"
        if r.get("labels_shortened"):
            r["text"] += (f"; labels on port(s) {', '.join(map(str, r['labels_shortened']))} shortened to the board's "
                          f"{BOARD_LABEL_MAX} characters")
        return r

    def board_status(self) -> dict[str, Any]:
        if self.board_read is None:
            return {"available": False}
        from ..hardware.antsw import diff_names
        try:
            board = self.board_read()
        except Exception as exc:  # noqa: BLE001
            return {"available": True, "reachable": False, "error": str(exc)}
        diffs = diff_names(board, self.s.switch_ports)
        return {"available": True, "reachable": True, "in_sync": not diffs, "differences": diffs}

    def push_board(self, author: str) -> dict[str, Any]:
        with self._lock:
            r = self._push_board()
        with self.s.engine.begin() as conn:
            self.s.notify(conn, "rf_layout", None, f"{r['text']} (sent by {author})")
        return r

    def rename(self, old: str, new: str, base_version: int | None, author: str) -> dict[str, Any]:
        """Change a part's ID everywhere it is referenced: the layout, upcoming jobs that name it (OBS-R05 jobs pick
        their switch port by antenna ID), and the switch board. Past tests keep the ID they were recorded with;
        the old ID is kept as a former ID so the part's history stays together."""
        with self._lock:
            cur = self.current or self.load()
            if base_version is not None and base_version != cur["version"]:
                raise SchedulerError(f"the layout changed since you loaded it (now version {cur['version']}); reload first", 409)
            lay = json.loads(json.dumps(cur["layout"]))
            comps = lay["components"]
            new = (new or "").strip()
            if old not in comps:
                raise SchedulerError(f"no part {old}", 404)
            if old == SWITCH_ID or comps[old]["kind"] == "switch":
                raise SchedulerError(f"{SWITCH_ID}'s ID is fixed: the broker controls OBS-R05 through it", 400)
            if not _ID.match(new):
                raise SchedulerError("an ID is 1-48 letters, digits, spaces or _ . + -", 400)
            if new == old:
                return {"ok": True, "unchanged": True, "version": cur["version"]}
            if new in comps or any(new in (c.get("former_ids") or []) and c["id"] != old for c in comps.values()):
                raise SchedulerError(f"{new} is already used by another part", 409)
            c = comps.pop(old)
            c["id"] = new
            c["former_ids"] = [x for x in dict.fromkeys((c.get("former_ids") or []) + [old]) if x != new]
            comps[new] = c
            for r in lay["receivers"].values():
                r["chain"] = [new if x == old else x for x in r.get("chain") or []]
            for sw in comps.values():
                for port, aid in (sw.get("ports") or {}).items():
                    if aid == old:
                        sw["ports"][port] = new
            norm, errors, _ = validate(lay, self.s)
            if errors:
                raise SchedulerError("; ".join(errors), 400)
            # upcoming jobs that name the part (switch-port antennas) follow the new ID before the save re-resolves ports
            moved = []
            now = self.s.clock()
            with self.s.locked(), self.s.engine.begin() as conn:
                rows = conn.execute(select(sched_jobs.c.job_id, sched_jobs.c.spec, sched_jobs.c.job_name).where(and_(
                    sched_jobs.c.status.in_(("scheduled", "shadow")), sched_jobs.c.reservation_start > now))).all()
                for r in rows:
                    spec = dict(r.spec or {})
                    rxb = dict(spec.get("receiver") or {})
                    if rxb.get("antenna") == old:
                        rxb["antenna"] = new
                        spec["receiver"] = rxb
                        conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == r.job_id).values(spec=spec, updated_at=now))
                        moved.append(f"{r.job_name} ({r.job_id})")
            res = self.save(norm, cur["version"], author, f"renamed {old} → {new}")
            res["renamed"] = {"from": old, "to": new, "jobs_updated": moved}
            return res

    def aliases(self, cid: str) -> set[str]:
        """The part's current ID plus every former ID (or the part that now owns `cid` as a former ID)."""
        comps = ((self.current or {}).get("layout") or {}).get("components") or {}
        for c in comps.values():
            if c["id"] == cid or cid in (c.get("former_ids") or []):
                return {c["id"], *(c.get("former_ids") or [])}
        return {cid}

    def _derived(self, layout: dict[str, Any]) -> dict[str, Any]:
        return {rid: {k: (list(v) if isinstance(v, tuple) else v) for k, v in derive(layout, rid).items()}
                for rid in layout["receivers"]}

    def view(self) -> dict[str, Any]:
        cur = self.current or self.load()
        return {"version": cur["version"], "saved_at": _iso(cur["created_at"]), "author": cur["author"],
                "note": cur.get("note"), "layout": cur["layout"], "derived": self._derived(cur["layout"]),
                "kinds": list(KINDS), "statuses": list(STATUSES), "powers": list(POWER), "switch_id": SWITCH_ID,
                "switch_receiver": _switch_receiver(self.s)}

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._rows(limit)
        out = []
        for i, r in enumerate(rows):
            prev = rows[i + 1]["layout"] if i + 1 < len(rows) else None
            out.append({"version": r["version"], "author": r["author"], "note": r["note"],
                        "saved_at": _iso(r["created_at"]),
                        "changes": diff_summary(prev, r["layout"]) if prev is not None else ["initial layout"]})
        return out

    def version(self, v: int) -> dict[str, Any]:
        with self.s.engine.connect() as conn:
            r = conn.execute(select(rf_layouts).where(rf_layouts.c.version == v)).mappings().first()
        if r is None:
            raise SchedulerError(f"no layout version {v}", 404)
        return {"version": r["version"], "layout": r["layout"], "author": r["author"], "note": r["note"],
                "saved_at": _iso(r["created_at"])}

    # ---- recordings
    def snapshot(self, receiver_id: str, at: dt.datetime | None = None, port_antenna: str | None = None,
                 port: Any = None) -> dict[str, Any] | None:
        if at is None:
            row = self.current
            basis = "layout in effect when the capture started"
        else:
            rows = self._rows(1, before=at)
            row = rows[0] if rows else None
            basis = "layout in effect at the recording's start time"
        if not row:
            return None
        layout = row["layout"]
        r = layout.get("receivers", {}).get(receiver_id)
        if r is None:
            return None
        chain = []
        for cid in r["chain"]:
            c = layout["components"].get(cid)
            if c is None:
                continue
            if c["kind"] == "switch" and port_antenna:
                a = layout["components"].get(port_antenna)
                chain.append({"id": port_antenna, "name": a["name"] if a else port_antenna, "kind": "antenna",
                              "status": a["status"] if a else "unknown", "via_port": port})
            chain.append({k: c.get(k) for k in ("id", "name", "kind", "model", "status", "power", "gain_db")})
        return {"layout_version": row["version"], "layout_saved_at": _iso(row["created_at"]), "basis": basis,
                "receiver_id": receiver_id, "chain": chain,
                "text": path_text(layout, receiver_id, port_antenna, port), "taken_at": self.s.clock().isoformat()}

    def stamp(self, job: dict[str, Any], at_record_start: bool = False) -> dict[str, Any] | None:
        """Store the path in spec.resolved.rf_path once; never overwrite an existing one."""
        spec = dict(job.get("spec") or {})
        resolved = dict(spec.get("resolved") or {})
        if resolved.get("rf_path"):
            return resolved["rf_path"]
        at = dt.datetime.fromisoformat(job["record_start"]) if at_record_start else None
        rx = self.s.receivers.get(job["receiver_id"])
        ant = (spec.get("receiver") or {}).get("antenna") if rx is not None and rx.switched else None
        snap = self.snapshot(job["receiver_id"], at, ant, resolved.get("switch_port"))
        if snap is None:
            return None
        resolved["rf_path"] = snap
        spec["resolved"] = resolved
        self.s.update(job["job_id"], spec=spec)
        return snap


def diff_summary(before: dict[str, Any] | None, after: dict[str, Any]) -> list[str]:
    if before is None:
        return []
    out = []
    bc, ac = before.get("components", {}), after.get("components", {})
    for cid in sorted(set(ac) - set(bc)):
        out.append(f"new part: {ac[cid]['name']}")
    for cid in sorted(set(bc) - set(ac)):
        out.append(f"removed part: {bc[cid]['name']}")
    for cid in sorted(set(ac) & set(bc)):
        if ac[cid].get("status") != bc[cid].get("status"):
            out.append(f"{ac[cid]['name']}: {bc[cid].get('status')} → {ac[cid].get('status')}")
    names = {**{k: v["name"] for k, v in bc.items()}, **{k: v["name"] for k, v in ac.items()}}
    for rid in sorted(set(after.get("receivers", {})) | set(before.get("receivers", {}))):
        a = [names.get(c, c) for c in (after.get("receivers", {}).get(rid) or {}).get("chain", [])]
        b = [names.get(c, c) for c in (before.get("receivers", {}).get(rid) or {}).get("chain", [])]
        if a != b:
            out.append(f"{rid}: {' > '.join(a) or '(nothing)'}")
    pa = (ac.get(SWITCH_ID) or {}).get("ports") or {}
    pb = (bc.get(SWITCH_ID) or {}).get("ports") or {}
    for n in sorted(set(pa) | set(pb), key=int):
        if pa.get(n) != pb.get(n):
            out.append(f"{SWITCH_ID} port {n}: {names.get(pa.get(n), '(empty)') if pa.get(n) else '(empty)'}")
    return out


def board_id_or_none(aid: str) -> str | None:
    from ..hardware.antsw import board_id
    try:
        return board_id(aid)
    except ValueError:
        return None


def _switch_names(layout: dict[str, Any]) -> dict[str, Any]:
    sw = (layout.get("components") or {}).get(SWITCH_ID) or {}
    comps = layout.get("components") or {}
    return {n: (a, (comps.get(a) or {}).get("notes") or (comps.get(a) or {}).get("name")) if a else None
            for n, a in (sw.get("ports") or {}).items()}
