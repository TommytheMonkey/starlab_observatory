"""A stand-in for `sdrpp --server`, for tests and simulation mode. Opens no radio.

Accepts SDR++'s server arguments, listens on --addr/--port, accepts (and counts) TCP clients, and writes
<root>/mock-report.json describing what it could see: its arguments, its session marker, the USB device
nodes under /dev/bus/usb and the entries in /sys/bus/usb/devices (inside a sandbox, only the selected
receiver should be there). MOCK_SDRPP_EXIT_AFTER=<seconds> makes it exit by itself, to test crash handling.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import socket
import sys
import threading
import time


def _listing(path: str) -> list[str]:
    out = []
    for dirpath, _dirs, files in os.walk(path):
        out += [os.path.join(dirpath, f) for f in files]
    return sorted(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", action="store_true")
    ap.add_argument("--addr", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5259)
    ap.add_argument("--root", required=True)
    ap.add_argument("--autostart", action="store_true")
    ap.add_argument("--simulated", action="store_true")
    args = ap.parse_args(argv[1:])
    root = pathlib.Path(args.root)
    sysfs = "/sys/bus/usb/devices"
    report = {"argv": argv[1:], "pid": os.getpid(), "session": os.environ.get("STARLAB_SESSION_ID"),
              "usb_nodes": _listing("/dev/bus/usb") if os.path.isdir("/dev/bus/usb") else [],
              "sysfs_usb": sorted(os.listdir(sysfs)) if os.path.isdir(sysfs) else [],
              "simulated": args.simulated, "clients": 0}
    (root / "mock-report.json").write_text(json.dumps(report, indent=2))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    srv = socket.socket(socket.AF_INET6 if ":" in args.addr else socket.AF_INET)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.addr, args.port))
    srv.listen(4)
    srv.settimeout(0.2)
    conns = []
    deadline = time.monotonic() + float(os.environ.get("MOCK_SDRPP_EXIT_AFTER", "1e9"))
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            c, _ = srv.accept()
            conns.append(c)
            report["clients"] += 1
            (root / "mock-report.json").write_text(json.dumps(report, indent=2))
        except TimeoutError:
            pass
    for c in conns:
        c.close()
    srv.close()
    return 0 if stop.is_set() else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
