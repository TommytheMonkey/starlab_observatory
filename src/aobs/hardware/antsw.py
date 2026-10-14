"""Client for ANTSW-01 (ESP32-S3, firmware antsw-web-2.1).

Documented interface (from the board's own web UI, inspected 2026-10-11):
  GET  /api/state            -> current port/antenna, receiver fed, port map, lease, health
  GET  /api/history?limit=N  -> change log
  GET  /api/ports            -> editable port map
  POST /api/select {port,by} -> switch (requires X-Api-Token)
  PUT  /api/ports {ports:[{port,code,antenna,label}],switch_id,receiver}
                             -> edit the board's map (requires X-Api-Token); antenna <= 16 chars, label <= 48
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


BOARD_ANTENNA_MAX = 16
BOARD_LABEL_MAX = 48


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
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(256 * 1024) or b"{}")


def diff_names(board: dict[str, Any], wanted: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Ports whose antenna name on the board differs from the layout's."""
    out = []
    for p in board.get("ports", []):
        n = int(p["port"])
        want = (wanted.get(n) or {}).get("antenna")
        if (p.get("antenna") or None) != (want or None):
            out.append({"port": n, "board": p.get("antenna"), "layout": want})
    return out


def push_names(base_url: str, token: str, wanted: dict[int, dict[str, Any]], timeout: float = 5.0) -> dict[str, Any]:
    """Write antenna names/labels to the board. Codes, switch_id and receiver are copied from the board itself."""
    board = get_ports(base_url, timeout)
    rows = []
    for p in board["ports"]:
        n = int(p["port"])
        w = wanted.get(n)
        name = (w or {}).get("antenna")
        if name and len(name) > BOARD_ANTENNA_MAX:
            raise ValueError(f"port {n}: antenna name {name!r} is longer than the board's {BOARD_ANTENNA_MAX} characters")
        label = ((w or {}).get("label") or "").strip()[:BOARD_LABEL_MAX] if w else "Empty"
        rows.append({"port": n, "code": p["code"], "antenna": name or None, "label": label or None})
    put_ports(base_url, token, rows, board.get("switch_id") or "ANTSW-01", board.get("receiver") or "OBS-R05", timeout)
    after = get_ports(base_url, timeout)
    left = diff_names(after, wanted)
    codes_kept = [int(p["code"]) for p in after["ports"]] == [int(p["code"]) for p in board["ports"]]
    return {"pushed": True, "verified": not left and codes_kept, "differences": left, "codes_unchanged": codes_kept,
            "changed_ports": [d["port"] for d in diff_names(board, wanted)]}
