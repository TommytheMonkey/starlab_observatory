"""Client for ANTSW-01 (ESP32-S3, firmware antsw-web-2.1).

Documented interface (from the board's own web UI, inspected 2026-10-11):
  GET  /api/state            -> current port/antenna, receiver fed, port map, lease, health
  GET  /api/history?limit=N  -> change log
  GET  /api/ports            -> editable port map
  POST /api/select {port,by} -> switch (requires X-Api-Token)
  PUT  /api/ports {ports:[{port,code,antenna,label}],switch_id,receiver}
                             -> edit the board's map (requires X-Api-Token); antenna <= 16 chars of
                                letters, digits, _ . - (HTTP 400 for the whole map otherwise), label <= 48
  DELETE /api/ports          -> back to the firmware defaults   [NOT USED]

The switch feeds OBS-R05, the receiver ADS-B also uses. Only the broker
(aobs.coordination) calls select(), and only while it holds R05; it selects by
port number, so names never decide which RF port is used. The /rf editor's
layout is authoritative for names; each save that changes the ports pushes
them here with push_names(), which keeps every port's hardware code as the
board reports it.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any


def get_state(base_url: str, timeout: float = 5.0) -> dict[str, Any]:
    request = urllib.request.Request(base_url.rstrip("/") + "/api/state", headers={"User-Agent": "aobs/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(256 * 1024))


def select(base_url: str, token: str, port: int, by: str = "aobs-broker", timeout: float = 5.0) -> dict[str, Any]:
    body = json.dumps({"port": int(port), "by": by}).encode()
    request = urllib.request.Request(base_url.rstrip("/") + "/api/select", data=body, method="POST",
                                     headers={"User-Agent": "aobs/0.1", "Content-Type": "application/json",
                                              "X-Api-Token": token})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(256 * 1024) or b"{}")


class BoardRefused(RuntimeError):
    def __init__(self, status: int, body: str):
        self.status, self.body = status, body
        super().__init__(f"the board refused the port map (HTTP {status}): {body[:300] or 'no reason given'}")


BOARD_ANTENNA_MAX = 16
BOARD_LABEL_MAX = 48
_BOARD_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")   # firmware: "antenna name may only use letters, digits, _ . -"


def board_id(name: str) -> str:
    """The identifier the board stores for a layout antenna: each run of characters the firmware refuses becomes
    one '-' ("ADS Airband" -> "ADS-Airband"). Deterministic, so the two names are treated as the same antenna."""
    out = _BOARD_UNSAFE.sub("-", str(name).strip()).strip("-")
    if not out:
        raise ValueError(f"antenna name {name!r} has no characters the board can store (letters, digits, _ . -)")
    if len(out) > BOARD_ANTENNA_MAX:
        raise ValueError(f"antenna name {name!r} is longer than the board's {BOARD_ANTENNA_MAX} characters")
    return out


def get_ports(base_url: str, timeout: float = 5.0) -> dict[str, Any]:
    request = urllib.request.Request(base_url.rstrip("/") + "/api/ports", headers={"User-Agent": "aobs/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(256 * 1024))


def put_ports(base_url: str, token: str, ports: list[dict[str, Any]], switch_id: str, receiver: str,
              timeout: float = 5.0) -> dict[str, Any]:
    body = json.dumps({"ports": ports, "switch_id": switch_id, "receiver": receiver}).encode()
    request = urllib.request.Request(base_url.rstrip("/") + "/api/ports", data=body, method="PUT",
                                     headers={"User-Agent": "aobs/0.1", "Content-Type": "application/json",
                                              "X-Api-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(256 * 1024) or b"{}")
    except urllib.error.HTTPError as exc:          # the board says why it refused; keep that, not just "400"
        why = exc.read(4096).decode(errors="replace").strip()
        raise BoardRefused(exc.code, why) from exc


def board_form(wanted: dict[int, dict[str, Any]], port: int) -> dict[str, Any]:
    """What the board stores for this port: the one form both sending and comparing use, so a label shortened to
    the board's limit never shows up as a difference. Names over the limit are refused, never cut; see board_id for characters."""
    w = wanted.get(port)
    if not w or not w.get("antenna"):
        return {"antenna": None, "label": "Empty"}
    try:
        name = board_id(w["antenna"])
    except ValueError as exc:
        raise ValueError(f"port {port}: {exc}") from None
    label = " ".join(str(w.get("label") or "").split())[:BOARD_LABEL_MAX]
    return {"antenna": name, "label": label or None}


def diff_names(board: dict[str, Any], wanted: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Ports where the board's stored antenna name or label differs from the layout's (in board form)."""
    out = []
    for p in board.get("ports", []):
        n = int(p["port"])
        want = board_form(wanted, n)
        have = {"antenna": (p.get("antenna") or "").strip() or None, "label": " ".join(str(p.get("label") or "").split()) or None}
        if have["antenna"] != want["antenna"] or (want["antenna"] and have["label"] != want["label"]):
            d = {"port": n, "board": have["antenna"], "layout": want["antenna"]}
            if have["antenna"] == want["antenna"]:
                d.update(board_label=have["label"], layout_label=want["label"])
            out.append(d)
    return out


def push_names(base_url: str, token: str, wanted: dict[int, dict[str, Any]], timeout: float = 5.0) -> dict[str, Any]:
    """Write antenna names/labels to the board, then read them back. Codes, switch_id and receiver are copied from
    the board itself; the selected RF port is never touched (checked before and after)."""
    port_before = get_state(base_url, timeout).get("port")
    board = get_ports(base_url, timeout)
    rows, shortened = [], []
    for p in board["ports"]:
        n = int(p["port"])
        f = board_form(wanted, n)
        full = " ".join(str((wanted.get(n) or {}).get("label") or "").split())
        if f["antenna"] and len(full) > BOARD_LABEL_MAX:
            shortened.append(n)
        rows.append({"port": n, "code": p["code"], **f})
    put_ports(base_url, token, rows, board.get("switch_id") or "ANTSW-01", board.get("receiver") or "", timeout)
    after = get_ports(base_url, timeout)
    port_after = get_state(base_url, timeout).get("port")
    left = diff_names(after, wanted)
    codes_kept = [int(p["code"]) for p in after["ports"]] == [int(p["code"]) for p in board["ports"]]
    port_kept = port_after == port_before
    return {"pushed": True, "verified": not left and codes_kept and port_kept, "differences": left,
            "codes_unchanged": codes_kept, "selected_port_unchanged": port_kept, "selected_port": port_after,
            "stored_in": after.get("source"), "labels_shortened": shortened,
            "stored_as": {n: board_id(w["antenna"]) for n, w in sorted(wanted.items())
                          if w and w.get("antenna") and board_id(w["antenna"]) != w["antenna"]},
            "changed_ports": [d["port"] for d in diff_names(board, wanted)]}
