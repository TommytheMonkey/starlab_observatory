"""RF inventory and signal paths: the antennas, filters, amplifiers and other parts that feed each receiver.

The owner rearranges hardware often and edits the layout in the drag-and-drop page (/rf). Every
save is a new, append-only version in `rf_layouts`. The current version is authoritative for the
broker's receivers (antenna, signal_path, amplifier, filters, and the ANTSW-01 port map), so job
validation, /radio-status, the calendar and the agent all describe the hardware as it really is.

Each recording keeps the path it was made with in `spec.resolved.rf_path`:
  * broker recordings: the current layout at the moment the capture starts;
  * legacy-recorder files: the layout version in effect at the recording's start time.
Recordings made before the first layout version have no rf_path; nothing is back-filled.

Layout document (older versions lack the newer fields; validate() fills the defaults):
  components: {id: {id, name, kind, status, power, model, manufacturer, description, notes, former_ids,
                     gain_db, min_mhz, max_mhz, ...kind-specific fields (see validate)}}
  receivers:  {id: {id, chain: [component ids, antenna side first], feed?, gain_profiles, notes, managed, ...}}

Every connection is declared by the owner (or comes from the owner's observatory.toml for version 1);
nothing here guesses how hardware is wired. Discovery adds receivers with an empty path.

* A switch heads its receiver's chain; its `ports` map port number -> antenna id. ANTSW-01 is driven by the
  broker for one receiver (driver "antsw-http"); any other switch is a manual one whose `selected_port` the
  owner sets to say which antenna is connected now.
* A splitter lets one antenna feed several receivers: the splitter sits in one receiver's chain and other
  receivers name it as their `feed`; their own chain holds only the parts after their splitter output.
  full_chain() gives the whole path for any receiver.
* A rotator is not in a signal path; an antenna names the rotator that turns it.
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
from .hardware import auto_radio, freq_range_hz, gain_profile_problems, validate_radio
from .store import SchedulerError

LOG = logging.getLogger("aobs.rflayout")

KINDS = ("antenna", "filter", "amplifier", "switch", "splitter", "attenuator", "cable", "connector", "rotator", "other")
PATH_KINDS = tuple(k for k in KINDS if k != "rotator")
STATUSES = ("working", "suspect", "broken", "unknown")
POWER = ("none", "external", "bias_tee")
SUPPLY = ("unknown", "connected", "missing")          # for parts with their own power supply
DC_PASS = ("unknown", "yes", "no")                    # does the part let bias-tee DC through?
FILTER_TYPES = ("other", "bandpass", "notch", "lowpass", "highpass")
POLARIZATIONS = ("unknown", "rhcp", "lhcp", "vertical", "horizontal", "linear", "dual")
MOUNTS = ("unknown", "fixed", "rotatable")
RX_BIAS_TEE = ("unknown", "none", "switchable", "always_on")   # documentation-only receivers
GAIN_MODES = ("manual", "auto")
SWITCH_ID = "ANTSW-01"
SWITCH_PORTS = 8
MAX_PORTS = 16
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.+-]{0,47}$")   # switch-port antennas have names like "ADS Airband"
_RX_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").upper()[:48]
    return s or "PART"


def _iso(t: Any) -> Any:
    return t.isoformat() if hasattr(t, "isoformat") else t


def _text(v: Any, n: int) -> str:
    return str(v or "").strip()[:n]


def _choice(v: Any, options: tuple[str, ...]) -> str:
    return v if v in options else options[0]


# ---------------------------------------------------------------- seed from the config
def seed_from_config(s: Any) -> dict[str, Any]:
    """Version 1: the receivers, antennas, amplifiers and filters the owner's TOML config already describes."""
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


# ---------------------------------------------------------------- whole paths
def full_chain(layout: dict[str, Any], rid: str, _seen: frozenset[str] = frozenset()) -> list[str]:
    """The receiver's whole path, antenna side first: for a receiver fed from a splitter, the path up to and
    including that splitter (from the receiver whose chain holds it, recursively) followed by its own parts."""
    rxs = layout.get("receivers") or {}
    r = rxs.get(rid) or {}
    own = list(r.get("chain") or [])
    feed = r.get("feed")
    if not feed or rid in _seen:
        return own
    owner = next((x for x, v in rxs.items() if feed in (v.get("chain") or [])), None)
    if owner is None or owner == rid:
        return own
    up = full_chain(layout, owner, _seen | {rid})
    return (up[:up.index(feed) + 1] if feed in up else []) + own


def head_antenna(layout: dict[str, Any], chain: list[str], job_antenna: str | None = None) -> dict[str, Any] | None:
    """The antenna at the start of a path: the antenna itself, a manual switch's selected antenna, or for ANTSW-01
    the antenna a job asked for (None when unknown)."""
    comps = layout["components"]
    head = comps.get(chain[0]) if chain else None
    if head is None:
        return None
    if head["kind"] == "antenna":
        return head
    if head["kind"] == "switch":
        aid = job_antenna if head["id"] == SWITCH_ID else (head.get("ports") or {}).get(str(head.get("selected_port")))
        return comps.get(aid) if aid else None
    return None


def _rng(c: dict[str, Any]) -> tuple[float | None, float | None]:
    return c.get("min_mhz"), c.get("max_mhz")


def freq_problems(layout: dict[str, Any], chain: list[str], freq_hz: float, antenna: dict[str, Any] | None) -> list[str]:
    """Why a signal at freq_hz may not get through this path, from the ranges the owner entered."""
    comps = layout["components"]
    mhz = freq_hz / 1e6
    f = f"{mhz:.4f}".rstrip("0").rstrip(".") + " MHz"
    out: list[str] = []
    if antenna is not None:
        lo, hi = _rng(antenna)
        if (lo is not None and mhz < lo) or (hi is not None and mhz > hi):
            out.append(f"{f} is outside {antenna['name']}'s range ({_range_text(lo, hi)})")
    for cid in chain:
        c = comps.get(cid) or {}
        lo, hi = _rng(c)
        if c.get("kind") == "filter":
            t = c.get("filter_type")
            if t == "bandpass" and ((lo is not None and mhz < lo) or (hi is not None and mhz > hi)):
                out.append(f"{f} is outside {c['name']}'s passband ({_range_text(lo, hi)})")
            elif t == "notch" and lo is not None and hi is not None and lo <= mhz <= hi:
                out.append(f"{f} is inside {c['name']}'s stop band ({_range_text(lo, hi)})")
            elif t == "lowpass" and hi is not None and mhz > hi:
                out.append(f"{f} is above {c['name']}'s cutoff ({hi:g} MHz)")
            elif t == "highpass" and lo is not None and mhz < lo:
                out.append(f"{f} is below {c['name']}'s cutoff ({lo:g} MHz)")
        elif c.get("kind") == "amplifier" and ((lo is not None and mhz < lo) or (hi is not None and mhz > hi)):
            out.append(f"{f} is outside {c['name']}'s rated range ({_range_text(lo, hi)})")
    return out


def _range_text(lo: float | None, hi: float | None) -> str:
    if lo is not None and hi is not None:
        return f"{lo:g}–{hi:g} MHz"
    return f"from {lo:g} MHz" if lo is not None else f"up to {hi:g} MHz"


def job_warnings(layout: dict[str, Any] | None, rx: Any, job_antenna: str | None, freq_hz: Any) -> list[str]:
    """Signal-path warnings for one job (spec.check_job): the frequency against the antenna, filters and
    amplifiers on the path the job will use. Warnings only: the owner may know better."""
    if not layout or rx.id not in (layout.get("receivers") or {}) or not isinstance(freq_hz, (int, float)):
        return []
    chain = full_chain(layout, rx.id)
    return [f"signal path: {p}" for p in freq_problems(layout, chain, freq_hz, head_antenna(layout, chain, job_antenna))]


# ---------------------------------------------------------------- validation
def _num(c: dict[str, Any], k: str, cid: str, errors: list[str]) -> float | None:
    v = c.get(k)
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        errors.append(f"part {cid}: {k} must be a number")
        return None


def _gain_profiles(rid: str, raw: Any, errors: list[str]) -> list[dict[str, Any]]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        errors.append(f"{rid}: gain_profiles must be a list")
        return []
    out, names = [], set()
    for p in raw[:20]:
        if not isinstance(p, dict):
            errors.append(f"{rid}: each gain profile must be an object")
            continue
        name = _text(p.get("name"), 40)
        if not name:
            errors.append(f"{rid}: a gain profile needs a name")
            continue
        if name in names:
            errors.append(f"{rid}: two gain profiles are called {name!r}")
        names.add(name)
        prof: dict[str, Any] = {"name": name, "gain_mode": _choice(p.get("gain_mode"), GAIN_MODES), "gain_db": None,
                                "gain_elements": {}, "notes": _text(p.get("notes"), 500)}
        try:
            prof["gain_db"] = None if p.get("gain_db") in (None, "") else float(p["gain_db"])
            for k, v in (p.get("gain_elements") or {}).items():
                if v in (None, ""):
                    continue
                if not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,15}$", str(k)):
                    raise ValueError(f"gain element name {k!r}")
                prof["gain_elements"][str(k)] = float(v)
        except (TypeError, ValueError, AttributeError) as exc:
            errors.append(f"{rid}: gain profile {name!r} has a value that is not a number ({exc})")
        out.append(prof)
    return out


def _component(cid: str, c: dict[str, Any], errors: list[str]) -> dict[str, Any] | None:
    name = str(c.get("name") or "").strip()[:80]
    kind = c.get("kind")
    if not name:
        errors.append(f"part {cid} needs a name")
    if kind not in KINDS:
        errors.append(f"part {cid}: kind must be one of {', '.join(KINDS)}")
        return None
    out: dict[str, Any] = {
        "id": cid, "name": name or cid, "kind": kind,
        "status": c.get("status") if c.get("status") in STATUSES else "unknown",
        "power": c.get("power") if c.get("power") in POWER else "none",
        "model": _text(c.get("model"), 120), "manufacturer": _text(c.get("manufacturer"), 120),
        "description": _text(c.get("description"), 500), "notes": str(c.get("notes") or "")[:2000],
        # IDs this part had before a rename: tests recorded under them still belong to it
        "former_ids": [str(x) for x in (c.get("former_ids") or []) if _ID.match(str(x)) and str(x) != cid][:20]}
    for k in ("gain_db", "min_mhz", "max_mhz"):
        out[k] = _num(c, k, cid, errors)
    if out["min_mhz"] is not None and out["max_mhz"] is not None and out["min_mhz"] > out["max_mhz"]:
        errors.append(f"part {cid}: min_mhz is above max_mhz")
    if out["power"] == "external":
        out["supply"] = _choice(c.get("supply"), SUPPLY)
    if kind not in ("antenna", "rotator"):
        out["dc_pass"] = _choice(c.get("dc_pass"), DC_PASS)
    if kind == "antenna":
        out.update(antenna_type=_text(c.get("antenna_type"), 40), polarization=_choice(c.get("polarization"), POLARIZATIONS),
                   location=_text(c.get("location"), 120), orientation=_text(c.get("orientation"), 120),
                   mount=_choice(c.get("mount"), MOUNTS), rotator=_text(c.get("rotator"), 48) or None)
        if out["rotator"]:
            out["mount"] = "rotatable"
    elif kind == "filter":
        out["filter_type"] = _choice(c.get("filter_type"), FILTER_TYPES)
    elif kind == "splitter":
        n = _num(c, "outputs", cid, errors)
        out["outputs"] = int(n) if n is not None else 2
        if not 2 <= out["outputs"] <= MAX_PORTS:
            errors.append(f"{cid}: a splitter has 2-{MAX_PORTS} outputs")
    elif kind == "cable":
        out["length_m"] = _num(c, "length_m", cid, errors)
    elif kind == "switch":
        raw_ports = c.get("ports") or {}
        if cid == SWITCH_ID:
            out["driver"], count = "antsw-http", SWITCH_PORTS
        else:
            out["driver"] = "manual"
            n = _num(c, "port_count", cid, errors)
            count = int(n) if n is not None else max([SWITCH_PORTS if not raw_ports else 2] +
                                                      [int(p) for p in raw_ports if str(p).isdigit()])
            if not 2 <= count <= MAX_PORTS:
                errors.append(f"{cid}: a switch has 2-{MAX_PORTS} ports")
                count = max(2, min(count, MAX_PORTS))
        out["port_count"] = count
        out["ports"] = {str(n): (raw_ports.get(str(n)) or None) for n in range(1, count + 1)}
        extra = set(map(str, raw_ports)) - set(out["ports"])
        if extra and any(raw_ports.get(p) for p in extra):
            errors.append(f"{cid}: ports must be 1-{count} (got {sorted(extra)})")
        if out["driver"] == "manual":
            sel = c.get("selected_port")
            out["selected_port"] = str(sel) if sel not in (None, "") and str(sel) in out["ports"] else None
    return out


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
        out = _component(cid, c, errors)
        if out is not None:
            comps[cid] = out

    used: dict[str, str] = {}

    def place(cid: str, where: str) -> None:
        if cid in used:
            errors.append(f"{comps[cid]['name']} is in two places ({used[cid]} and {where}); one physical part, one place. "
                          "To feed two receivers from one antenna, put a splitter in one path and feed the other "
                          "receiver from it")
        used[cid] = where

    for c in list(comps.values()):
        if c["kind"] == "antenna" and c["rotator"] and (comps.get(c["rotator"]) or {}).get("kind") != "rotator":
            errors.append(f"{c['name']}: {c['rotator']!r} is not a rotator in the inventory")
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
            elif c["id"] != SWITCH_ID:
                place(aid, f"{c['id']} port {port}")
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
        out = {"id": rid, "chain": chain, "notes": str(r.get("notes") or "")[:2000], "managed": managed,
               "feed": _text(r.get("feed"), 48) or None, "gain_profiles": _gain_profiles(rid, r.get("gain_profiles"), errors)}
        if not managed:
            out.update(label=str(r.get("label") or rid)[:80], host=str(r.get("host") or "")[:120],
                       serial=str(r.get("serial") or "")[:64], hardware=str(r.get("hardware") or "")[:120],
                       status=r.get("status") if r.get("status") in STATUSES else "unknown",
                       min_mhz=_num(r, "min_mhz", rid, errors), max_mhz=_num(r, "max_mhz", rid, errors),
                       sample_rates=_text(r.get("sample_rates"), 200), formats=_text(r.get("formats"), 120),
                       bias_tee=_choice(r.get("bias_tee"), RX_BIAS_TEE))
        for i, cid in enumerate(chain):
            if cid not in comps:
                errors.append(f"{rid}: unknown part {cid!r}")
                continue
            c = comps[cid]
            place(cid, rid)
            if c["kind"] == "rotator":
                errors.append(f"{rid}: {c['name']} is a rotator; it turns an antenna and is not part of a signal path "
                              "(choose it on the antenna instead)")
            elif c["kind"] in ("antenna", "switch") and i != 0:
                errors.append(f"{rid}: {c['name']} is an {c['kind']} and must be first (the antenna end)")
            elif c["kind"] in ("antenna", "switch") and out["feed"]:
                errors.append(f"{rid} is fed from a splitter, so its own path cannot start with {c['name']}")
        receivers[rid] = out
    for rid in s.receivers:
        if rid not in receivers:
            errors.append(f"receiver {rid} is missing; broker receivers cannot be removed here")

    # splitter feeds: one splitter output per receiver, no loops, and the splitter must be in a path
    owner = {cid: rid for rid, r in receivers.items() for cid in r["chain"]}
    fed: dict[str, list[str]] = {}
    for rid, r in receivers.items():
        f = r["feed"]
        if not f:
            continue
        if f not in comps or comps[f]["kind"] != "splitter":
            errors.append(f"{rid} is fed from {f!r}, which is not a splitter in the inventory")
        elif f not in owner:
            errors.append(f"{rid} is fed from {comps[f]['name']}, which is not in any receiver's path (it is in the parts bin)")
        elif owner[f] == rid:
            errors.append(f"{rid} cannot be fed from {comps[f]['name']}: that splitter is in its own path")
        else:
            fed.setdefault(f, []).append(rid)
    for f, rids in fed.items():
        if 1 + len(rids) > comps[f]["outputs"]:
            errors.append(f"{comps[f]['name']} has {comps[f]['outputs']} outputs but feeds {1 + len(rids)} receivers "
                          f"({', '.join([owner[f], *sorted(rids)])}); that path is impossible")
    for rid in receivers:
        seen, cur = [], rid
        while cur and receivers[cur]["feed"] in owner and cur not in seen:
            seen.append(cur)
            cur = owner[receivers[cur]["feed"]]
        if cur in seen:
            errors.append(f"{rid}: the splitter feeds form a loop ({' → '.join(seen + [cur])})")
            break
    if errors:
        return {}, errors, warns
    norm = {"components": comps, "receivers": receivers}

    for rid, r in receivers.items():
        ids = full_chain(norm, rid)
        chain = [comps[c] for c in ids]
        head = chain[0] if chain else None
        if head is None or head["kind"] not in ("antenna", "switch"):
            warns.append(f"{rid} has no antenna in its path")
        rx = s.receivers.get(rid)
        cfg_switched = rx is not None and _config_switched(s, rid)
        if cfg_switched and (head is None or head["id"] != SWITCH_ID):
            errors.append(f"{rid} is driven through {SWITCH_ID} by the broker (leases, port selection); "
                          f"keep {SWITCH_ID} at the start of its path. Changing that needs a config change.")
        if not cfg_switched and any(c["id"] == SWITCH_ID for c in chain):
            errors.append(f"{SWITCH_ID} is controlled for {_switch_receiver(s)} only; it cannot feed {rid}"
                          + (" (not even through a splitter: the broker changes its port for "
                             f"{_switch_receiver(s)}'s jobs)" if r["feed"] else ""))
        if head is not None and head["kind"] == "switch" and head["id"] != SWITCH_ID:
            sel = head.get("selected_port")
            if sel is None:
                warns.append(f"{rid}: no port is selected on {head['name']}, so its recordings have no antenna on record")
            elif not head["ports"].get(sel):
                warns.append(f"{rid}: {head['name']} is set to port {sel}, which has no antenna")
        for c in chain:
            if c["status"] == "broken":
                warns.append(f"{rid}: {c['name']} is marked broken")
            elif c["status"] == "suspect":
                warns.append(f"{rid}: {c['name']} is marked suspect")
        warns += _power_warnings(rid, r, chain, rx)
        warns += _freq_warnings(norm, rid, r, ids, rx)
        if rx is not None:
            for p in r["gain_profiles"]:
                warns += [f"{rid}: gain profile \"{p['name']}\": {x}" for x in gain_profile_problems(rx, p)]
    warns += _splitter_dc_warnings(norm, s, fed, owner)
    if errors:
        return {}, errors, warns
    return norm, errors, warns


def _bias(r: dict[str, Any], rx: Any) -> tuple[bool | None, bool | None]:
    """(can supply bias-tee DC, permanently on): None when unknown."""
    if rx is not None:
        return bool(rx.bias_tee_supported or rx.bias_tee_fixed), rx.bias_tee_fixed
    b = r.get("bias_tee", "unknown")
    return {"none": (False, False), "switchable": (True, None), "always_on": (True, True)}.get(b, (None, None))


def _power_warnings(rid: str, r: dict[str, Any], chain: list[dict[str, Any]], rx: Any) -> list[str]:
    out: list[str] = []
    can, fixed = _bias(r, rx)
    ext = [c for c in chain if c["power"] == "external" and c["kind"] == "amplifier"]
    needs = [c for c in chain if c["power"] == "bias_tee"]
    if fixed is True and ext:
        out.append(f"{rid}'s bias tee is permanently on and its DC would reach "
                   f"{', '.join(c['name'] for c in ext)}, which has its own supply")
    if needs and can is False:
        out.append(f"{rid} cannot supply bias-tee power, but {', '.join(c['name'] for c in needs)} needs it")
    elif needs and fixed is False:
        out.append(f"{rid}'s bias tee is permanently off, but {', '.join(c['name'] for c in needs)} needs bias-tee power")
    for i, c in enumerate(chain):
        if c["power"] == "bias_tee":
            block = [x["name"] for x in chain[i + 1:] if x.get("dc_pass") == "no"]
            if block:
                out.append(f"{rid}: {c['name']} needs bias-tee DC from the receiver, but {', '.join(block)} "
                           "does not pass DC")
        if c["power"] == "external" and c.get("supply") == "missing":
            out.append(f"{rid}: {c['name']} needs its own power supply, which is marked not connected")
    return out


def _freq_warnings(layout: dict[str, Any], rid: str, r: dict[str, Any], ids: list[str], rx: Any) -> list[str]:
    comps = layout["components"]
    out: list[str] = []
    ant = head_antenna(layout, ids)
    if rx is not None:
        hz = freq_range_hz(rx)
        rlo, rhi = (hz[0] / 1e6, hz[1] / 1e6) if hz else (None, None)
    else:
        rlo, rhi = r.get("min_mhz"), r.get("max_mhz")
    if ant is not None:
        alo, ahi = _rng(ant)
        if None not in (alo, ahi, rlo, rhi) and (ahi < rlo or alo > rhi):
            out.append(f"{rid}: {ant['name']} ({_range_text(alo, ahi)}) and the receiver ({_range_text(rlo, rhi)}) "
                       "have no frequencies in common")
        for cid in ids:
            c = comps[cid]
            lo, hi = _rng(c)
            if c["kind"] == "filter" and c.get("filter_type") == "bandpass" and None not in (lo, hi, alo, ahi) \
                    and (hi < alo or lo > ahi):
                out.append(f"{rid}: {c['name']}'s passband ({_range_text(lo, hi)}) is outside {ant['name']}'s range "
                           f"({_range_text(alo, ahi)})")
    if rx is not None and rx.auto.get("enabled"):
        try:
            radio = auto_radio(rx)
        except (KeyError, TypeError, ValueError) as exc:
            return out + [f"{rid}: the automatic-recording settings are incomplete ({exc})"]
        f = radio.get("target_frequency_hz") or radio["center_frequency_hz"]
        sats = rx.auto.get("satellites") or rx.auto.get("norad_ids") or []
        what = f"automatic recordings ({len(sats)} satellite{'s' if len(sats) != 1 else ''})" if sats else "automatic recordings"
        out += [f"{rid}: {what}: {p}" for p in freq_problems(layout, ids, f, ant)]
        errs: list[str] = []
        validate_radio(rx, radio, rx.engines()[0], rx.auto.get("format", ""), errs, [])
        out += [f"{rid}: {what}: {e}" for e in errs]
    return out


def _splitter_dc_warnings(layout: dict[str, Any], s: Any, fed: dict[str, list[str]], owner: dict[str, str]) -> list[str]:
    out = []
    for f, rids in fed.items():
        on = [x for x in [owner[f], *rids] if _bias(layout["receivers"][x], s.receivers.get(x))[1] is True]
        if len(on) > 1:
            out.append(f"{layout['components'][f]['name']}: {', '.join(on)} all have their bias tee permanently on; "
                       "their DC meets at the splitter")
    return out


def _config_switched(s: Any, rid: str) -> bool:
    raw = (s.config.section("scheduler").get("receivers") or {}).get(rid) or {}
    return raw.get("antenna") == SWITCH_ID


def _switch_receiver(s: Any) -> str:
    return next((rid for rid in s.receivers if _config_switched(s, rid)), "OBS-R05")


# ---------------------------------------------------------------- derived receiver fields
def path_text(layout: dict[str, Any], rid: str, port_antenna: str | None = None, port: Any = None) -> str:
    comps = layout["components"]
    parts = []
    for cid in full_chain(layout, rid):
        c = comps[cid]
        if c["kind"] == "switch" and cid == SWITCH_ID:
            if port_antenna:
                ant = comps.get(port_antenna, {}).get("name", port_antenna)
                parts += [ant, f"{c['name']} port {port}" if port else c["name"]]
            else:
                parts.append(f"{cid} port")
        elif c["kind"] == "switch":
            sel = c.get("selected_port")
            aid = (c.get("ports") or {}).get(str(sel)) if sel else None
            parts += [comps.get(aid, {}).get("name", aid), f"{c['name']} port {sel}"] if aid else [f"{c['name']} (no port selected)"]
        else:
            parts.append(c["name"])
    return " > ".join(parts + [rid])


def derive(layout: dict[str, Any], rid: str) -> dict[str, Any]:
    comps = layout["components"]
    ids = full_chain(layout, rid)
    chain = [comps[c] for c in ids]
    head = chain[0] if chain else None
    amps = [c for c in chain if c["kind"] == "amplifier"]
    power = "external" if any(c["power"] == "external" for c in amps) else \
        "bias_tee" if any(c["power"] == "bias_tee" for c in amps) else ""
    if head is not None and head["kind"] == "switch" and head["id"] != SWITCH_ID:
        ant = head_antenna(layout, ids)
        antenna = ant["id"] if ant else "UNSPECIFIED"
    else:
        antenna = head["id"] if head and head["kind"] in ("antenna", "switch") else "UNSPECIFIED"
    return {"antenna": antenna, "signal_path": path_text(layout, rid), "amplifier": "; ".join(c["name"] for c in amps),
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
            if old == SWITCH_ID:
                raise SchedulerError(f"{SWITCH_ID}'s ID is fixed: the broker controls {_switch_receiver(self.s)} through it", 400)
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
                if r.get("feed") == old:
                    r["feed"] = new
            for sw in comps.values():
                if sw.get("rotator") == old:
                    sw["rotator"] = new
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
        return {rid: {**{k: (list(v) if isinstance(v, tuple) else v) for k, v in derive(layout, rid).items()},
                      "full_chain": full_chain(layout, rid)}
                for rid in layout["receivers"]}

    def view(self) -> dict[str, Any]:
        cur = self.current or self.load()
        return {"version": cur["version"], "saved_at": _iso(cur["created_at"]), "author": cur["author"],
                "note": cur.get("note"), "layout": cur["layout"], "derived": self._derived(cur["layout"]),
                "kinds": list(KINDS), "statuses": list(STATUSES), "powers": list(POWER), "switch_id": SWITCH_ID,
                "switch_receiver": _switch_receiver(self.s), "warnings": validate(cur["layout"], self.s)[2],
                "options": {"supply": list(SUPPLY), "dc_pass": list(DC_PASS), "filter_types": list(FILTER_TYPES),
                            "polarizations": list(POLARIZATIONS), "mounts": list(MOUNTS), "rx_bias_tee": list(RX_BIAS_TEE),
                            "gain_modes": list(GAIN_MODES), "max_ports": MAX_PORTS}}

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
        own = set(r.get("chain") or [])
        for cid in full_chain(layout, receiver_id):
            c = layout["components"].get(cid)
            if c is None:
                continue
            if c["kind"] == "switch":
                aid, via = (port_antenna, port) if cid == SWITCH_ID else \
                    ((c.get("ports") or {}).get(str(c.get("selected_port"))), c.get("selected_port"))
                a = layout["components"].get(aid) if aid else None
                if aid and (cid != SWITCH_ID or port_antenna):
                    chain.append({"id": aid, "name": a["name"] if a else aid, "kind": "antenna",
                                  "status": a["status"] if a else "unknown", "via_port": via,
                                  **({k: a.get(k) for k in ("polarization", "min_mhz", "max_mhz")} if a else {})})
            entry = {k: c.get(k) for k in ("id", "name", "kind", "model", "status", "power", "gain_db")}
            entry.update({k: c[k] for k in ("filter_type", "polarization", "min_mhz", "max_mhz") if c.get(k) is not None})
            if cid not in own:
                entry["shared"] = True          # upstream of a splitter, shared with another receiver
            chain.append(entry)
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
        fa = (after.get("receivers", {}).get(rid) or {}).get("feed")
        fb = (before.get("receivers", {}).get(rid) or {}).get("feed")
        if fa != fb:
            out.append(f"{rid}: fed from {names.get(fa, fa)}" if fa else f"{rid}: no longer fed from {names.get(fb, fb)}")
    for cid in sorted(set(ac) & set(bc)):
        if ac[cid].get("kind") == "switch" and cid != SWITCH_ID and ac[cid].get("selected_port") != bc[cid].get("selected_port"):
            out.append(f"{ac[cid]['name']}: set to port {ac[cid].get('selected_port') or '(none)'}")
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
