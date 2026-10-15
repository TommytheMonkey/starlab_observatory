"""Process identity for sessions: a session's processes carry STARLAB_SESSION_ID in their environment.

Stopping a session signals one process group, and only after /proc shows its leader carries that
session's ID. Nothing is ever matched by command name (see devtools.py for the incident behind this).
"""

from __future__ import annotations

import os
import pathlib
import signal
import time
from typing import Iterable

MARKER = "STARLAB_SESSION_ID"
PROC = pathlib.Path("/proc")


def session_of(pid: int, proc: pathlib.Path = PROC) -> str | None:
    """The session ID in pid's environment; None if gone, unreadable, or not a session process."""
    try:
        raw = (proc / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    for item in raw.split(b"\0"):
        if item.startswith(MARKER.encode() + b"="):
            return item.split(b"=", 1)[1].decode(errors="replace")
    return None


def is_session_process(pid: int | None, session_id: str, proc: pathlib.Path = PROC) -> bool:
    return bool(pid) and session_of(int(pid), proc) == session_id


def group_members(pgid: int, proc: pathlib.Path = PROC) -> list[int]:
    out = []
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            fields = (d / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid:              # stat: state ppid pgrp ...
                out.append(int(d.name))
        except (OSError, IndexError, ValueError):
            continue
    return out


def stop_group(pid: int, pgid: int, session_id: str, timeout: float = 10.0) -> str:
    """SIGTERM, then SIGKILL, to this session's process group only. Returns what happened."""
    if not is_session_process(pid, session_id):
        return "already gone (or no longer this session's process); nothing signalled"
    if os.getpgid(pid) != pgid:
        return f"pid {pid} is no longer in process group {pgid}; nothing signalled"
    os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_session_process(pid, session_id):
            return "stopped"
        time.sleep(0.1)
    if is_session_process(pid, session_id):
        os.killpg(pgid, signal.SIGKILL)
        return "killed after not stopping within the grace period"
    return "stopped"


def holders_of(node: str, pids: Iterable[int], proc: pathlib.Path = PROC) -> list[int]:
    """Which of these processes have this device node open (evidence of the device actually served)."""
    target = os.path.realpath(node)
    out = []
    for pid in pids:
        try:
            for fd in (proc / str(pid) / "fd").iterdir():
                try:
                    if os.readlink(fd) == target:
                        out.append(pid)
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return out


def established_clients(port: int, proc: pathlib.Path = PROC) -> list[str]:
    """Remote endpoints with an ESTABLISHED TCP connection to this local port (from /proc/net/tcp*)."""
    out = []
    for name in ("tcp", "tcp6"):
        try:
            lines = (proc / "net" / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            parts = ln.split()
            if len(parts) < 4 or parts[3] != "01":             # 01 = ESTABLISHED
                continue
            local, remote = parts[1], parts[2]
            if int(local.rsplit(":", 1)[1], 16) == port:
                out.append(remote)
    return out
