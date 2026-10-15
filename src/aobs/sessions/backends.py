"""Session backends: how a session's server is launched, and what proves which device it serves.

A backend is the only place that knows where and how the server runs. Today: "local" (SDR++ in a
bubblewrap sandbox on this computer) and "sim" (the mock server, for simulated receivers). Brief 4 adds
a remote backend with the same interface (launch through the starlab-remote helper over SSH).
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from typing import Any, Callable, Protocol

from ..hardware.rtlsdr import SYSFS_USB
from ..inventory.discovery import find_device
from . import procs, sdrpp


class Backend(Protocol):
    name: str

    def problems(self, rx: Any) -> list[str]: ...
    def launch(self, sid: str, rx: Any, bind: str, port: int, workdir: pathlib.Path,
               lifetime_s: int) -> dict[str, Any]: ...
    def served(self, row: dict[str, Any]) -> dict[str, Any]: ...


def with_lifetime(cmd: list[str], lifetime_s: int) -> list[str]:
    """A hard end that needs no broker: if Starlab is down when the session should end, coreutils
    `timeout` ends the server anyway (TERM, then KILL 10 s later), so the receiver is never held forever."""
    return ["timeout", "--kill-after=10", str(max(int(lifetime_s), 1)), *cmd]


def _spawn(cmd: list[str], sid: str, log: pathlib.Path) -> subprocess.Popen[bytes]:
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, **{procs.MARKER: sid})
    with log.open("ab") as out:
        return subprocess.Popen(cmd, env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)       # own session and process group


class LocalSdrppBackend:
    """SDR++ server on this computer, sandboxed to the one receiver (see sdrpp.py for why)."""
    name = "local"

    def __init__(self, paths: sdrpp.Paths, sysfs: pathlib.Path = SYSFS_USB,
                 find: Callable[..., Any] = find_device, program: Callable[..., list[str]] | None = None,
                 node_source: Callable[[Any], str] | None = None):
        self.paths, self.sysfs, self.find = paths, sysfs, find
        self.program = program                 # tests: run the mock instead of sdrpp, inside the same sandbox
        self.node_source = node_source         # tests: a stand-in file bound at the device node's path

    def problems(self, rx: Any) -> list[str]:
        out = sdrpp.install_problems(rx.driver, self.paths)
        if not out and self.find(rx.serial, self.sysfs) is None:
            out.append(f"{rx.id} (serial {rx.serial}) is not on USB")
        return out

    def launch(self, sid: str, rx: Any, bind: str, port: int, workdir: pathlib.Path,
               lifetime_s: int) -> dict[str, Any]:
        dev = self.find(rx.serial, self.sysfs)
        if dev is None:
            raise RuntimeError(f"{rx.id} (serial {rx.serial}) is not on USB")
        if dev.serial != rx.serial:
            raise RuntimeError(f"USB device {dev.usb_path} reports serial {dev.serial}, not {rx.serial}")
        node = dev.dev_node
        src = self.node_source(dev) if self.node_source else node
        if not os.path.exists(src):
            raise RuntimeError(f"{node} does not exist; the receiver may be re-enumerating")
        root = sdrpp.write_root(workdir / "sdrpp-root", rx.driver, self.paths)
        sys_dev = os.path.realpath(self.sysfs / dev.usb_path)
        inner = self.program(bind, port, root) if self.program else None
        cmd = sdrpp.sandbox_command(self.paths, root, node, sys_dev, dev.usb_path, bind, port, inner)
        if src != node:                                  # tests: bind a stand-in file at the node's path
            i = cmd.index("--dev-bind")
            cmd[i + 1] = src
        cmd = with_lifetime(cmd, lifetime_s)
        proc = _spawn(cmd, sid, workdir / "server.log")
        return {"pid": proc.pid, "pgid": proc.pid, "command": cmd, "log": str(workdir / "server.log"),
                "lifetime_s": lifetime_s, "sandbox": {"device_node": node, "usb_path": dev.usb_path, "sysfs": sys_dev,
                            "visible_devices": [dev.usb_path], "openable_nodes": [node], "ipc": "private"},
                "_proc": proc}

    def served(self, row: dict[str, Any]) -> dict[str, Any]:
        node = (row.get("detail") or {}).get("sandbox", {}).get("device_node")
        if not node or not row.get("pgid"):
            return {"confirmed": False}
        holders = procs.holders_of(node, procs.group_members(int(row["pgid"])))
        return {"confirmed": bool(holders), "device_node": node, "opened_by": holders,
                "note": (f"SDR++ has {node} open: serial {row['device_serial']}, the only device in its sandbox"
                         if holders else f"SDR++ has not opened the receiver yet; it can only open {node}")}


class SimSdrppBackend:
    """Simulated receivers: the mock server stands in for SDR++. Labelled simulated; no hardware."""
    name = "sim"

    def problems(self, rx: Any) -> list[str]:
        return [] if rx.simulated else [f"{rx.id} is not a simulated receiver"]

    def launch(self, sid: str, rx: Any, bind: str, port: int, workdir: pathlib.Path,
               lifetime_s: int) -> dict[str, Any]:
        root = workdir / "sdrpp-root"
        root.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "aobs.sessions.mock_sdrpp", "--server", "--addr", bind, "--port", str(port),
               "--root", str(root), "--simulated"]
        cmd = with_lifetime(cmd, lifetime_s)
        proc = _spawn(cmd, sid, workdir / "server.log")
        return {"pid": proc.pid, "pgid": proc.pid, "command": cmd, "log": str(workdir / "server.log"),
                "sandbox": None, "simulated": True, "_proc": proc}

    def served(self, row: dict[str, Any]) -> dict[str, Any]:
        return {"confirmed": True, "simulated": True,
                "note": "simulated receiver: Starlab's stand-in server sends no real samples; no hardware is involved"}
