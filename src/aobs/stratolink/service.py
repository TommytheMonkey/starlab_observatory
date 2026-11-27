"""The Stratolink daemon (`python -m aobs.stratolink run`, systemd unit starlab-stratolink): the UDP receiver, the
control endpoint, log import at start, and housekeeping (status.json, privacy retention). It borrows no receivers and
never touches the broker's scheduler: it only listens on the network."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import signal
import threading
from typing import Any

from ..piratewatch.lease import read_token
from . import config as cfgmod
from .control import API, ControlServer
from .mission import Engine
from .receiver import Receiver, import_log
from .store import Store, iso

LOG = logging.getLogger("aobs.stratolink")
UTC = dt.timezone.utc


def seed(store: Store, s: cfgmod.Settings) -> None:
    """Trackers and gateways listed in the settings file, added once (edits made on the page win)."""
    for t in s.trackers:
        store.register_tracker(t["node_id"], t.get("name") or t["node_id"], seed=True,
                               **{k: t.get(k) for k in ("short_name", "hardware", "gps", "channel", "color", "profile",
                                                        "notes")})
    for g in s.gateways:
        if not (store.gateway(g["eui"].upper()) or {}).get("registered"):
            store.register_gateway(g["eui"], **{k: v for k, v in g.items() if k != "eui"})


def write_status(s: cfgmod.Settings, rx: Receiver) -> None:
    p = s.path("status.json")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"at": iso(dt.datetime.now(UTC)), "pid": os.getpid(), **rx.snapshot()}))
    tmp.replace(p)


def main(s: cfgmod.Settings) -> int:
    s.root.mkdir(parents=True, exist_ok=True)
    store = Store(s.db_path)
    seed(store, s)
    for path in s.import_paths:
        p = pathlib.Path(path).expanduser()
        if p.is_file():
            try:
                LOG.info("import %s: %s", p, import_log(store, s, p))
            except OSError as exc:
                LOG.warning("import %s failed: %s", p, exc)
    rx = Receiver(s, store)
    try:
        token = read_token(s.broker_token_file)
    except OSError as exc:
        LOG.error("broker token %s unreadable (%s): the page cannot reach the control endpoint", s.broker_token_file,
                  exc)
        token = ""
    server = ControlServer(API(s, store, rx.snapshot), token, s.control_host, s.control_port)
    stopping = threading.Event()

    def _term(signum: int, _frame: Any) -> None:
        LOG.info("signal %d: stopping", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    rx.start()
    server.start()
    last_purge = dt.datetime.min.replace(tzinfo=UTC)
    engine = Engine(store, s.loss_warn_s, s.loss_lost_s)
    try:
        while not stopping.wait(min(5.0, s.mission_step_s)):
            write_status(s, rx)
            now = dt.datetime.now(UTC)
            for f in store.active_flights():           # armed and flying missions: detection and timeline
                try:
                    engine.step(f["id"], now)
                except Exception:  # noqa: BLE001 - a mission's analysis must never stop the receiver
                    LOG.exception("mission %s: engine step failed", f["id"])
            if now - last_purge > dt.timedelta(hours=1):
                n = store.purge_foreign_raw(now - dt.timedelta(hours=s.raw_retention_h))
                if n:
                    LOG.info("dropped the raw frames of %d receptions from other people's nodes (older than %s h)", n,
                             s.raw_retention_h)
                last_purge = now
    finally:
        rx.stop()
        server.stop()
        write_status(s, rx)
        store.close()
    return 0
