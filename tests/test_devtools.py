"""Regression tests for the 2026-10-15 incident: development cleanup must never touch production."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time
import types

import pytest

from aobs import devtools
from aobs.sessions import procs

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)", "aobs", "broker"]   # looks like a broker by name


def _cfg(root: pathlib.Path, data: pathlib.Path, port: int = 18751, srange=(15259, 15299)):
    return types.SimpleNamespace(path=root / "observatory.toml", data_root=data,
                                 database_url=f"sqlite:///{data}/db/x.sqlite3",
                                 section=lambda n: {"broker": {"port": port, "token_file": str(root / "secrets/t")},
                                                    "sessions": {"port_range": list(srange)}}.get(n, {}))


def test_dev_safety_check(tmp_path, monkeypatch):
    root = tmp_path / ".dev"
    monkeypatch.setenv("STARLAB_DEV", "1")
    monkeypatch.setenv("STARLAB_DEV_ROOT", str(root))
    devtools.assert_dev_safe(_cfg(root, root / "data"))                       # all inside: fine
    with pytest.raises(devtools.DevSafetyError, match="data folder .* is outside"):
        devtools.assert_dev_safe(_cfg(root, pathlib.Path("/data/autonomous-observatory")))
    with pytest.raises(devtools.DevSafetyError, match="port 8751 is a production port"):
        devtools.assert_dev_safe(_cfg(root, root / "data", port=8751))
    with pytest.raises(devtools.DevSafetyError, match="overlap production"):
        devtools.assert_dev_safe(_cfg(root, root / "data", srange=(5259, 5299)))
    leaky = _cfg(root, root / "data")
    leaky.secrets_dir = pathlib.Path("/home/someone/auto-observe/secrets")      # where the SSH key would be made
    with pytest.raises(devtools.DevSafetyError, match="secrets folder .* is outside"):
        devtools.assert_dev_safe(leaky)
    monkeypatch.delenv("STARLAB_DEV")
    devtools.assert_dev_safe(_cfg(root, pathlib.Path("/data/x"), port=8751))  # production: the check does nothing


def test_two_brokers_can_never_share_a_data_folder(tmp_path):
    with devtools.broker_lock(tmp_path):
        assert (tmp_path / "run" / "broker.pid").read_text().strip() == str(os.getpid())
        with pytest.raises(devtools.DevSafetyError, match="already using"):
            with devtools.broker_lock(tmp_path):
                pass
    with devtools.broker_lock(tmp_path):                                      # released afterwards
        pass


def _spawn(env_extra: dict[str, str]) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STARLAB_")}
    env.update(env_extra)
    return subprocess.Popen(SLEEPER, env=env, start_new_session=True)


def test_dev_stop_only_signals_the_recorded_dev_broker(tmp_path, monkeypatch):
    root = tmp_path / ".dev"
    p = devtools.paths(root)
    p["run"].mkdir(parents=True)
    prod = _spawn({"STARLAB_CONFIG": "/home/someone/auto-observe/config/observatory.toml"})  # "production" lookalike
    dev = _spawn({"STARLAB_DEV": "1", "STARLAB_CONFIG": str(p["config"])})
    try:
        time.sleep(0.3)
        p["pid"].write_text(f"{dev.pid}\n")
        devtools.stop(root)
        dev.wait(timeout=10)
        assert dev.returncode is not None and prod.poll() is None            # only the dev process stopped
        p["pid"].write_text(f"{prod.pid}\n")                                 # a PID file pointing elsewhere
        devtools.stop(root)
        time.sleep(0.3)
        assert prod.poll() is None                                           # ...is never signalled
    finally:
        prod.kill()
        prod.wait()
        if dev.poll() is None:
            dev.kill()


def test_session_stop_only_signals_its_own_process_group():
    mine = _spawn({procs.MARKER: "s-mine"})
    other = _spawn({procs.MARKER: "s-other"})
    plain = _spawn({})
    try:
        time.sleep(0.3)
        assert procs.session_of(mine.pid) == "s-mine" and os.getpgid(mine.pid) == mine.pid
        assert procs.stop_group(other.pid, other.pid, "s-mine").startswith("already gone")   # wrong session: no-op
        assert procs.stop_group(mine.pid, mine.pid, "s-mine") == "stopped"
        mine.wait(timeout=5)
        assert other.poll() is None and plain.poll() is None
    finally:
        for p in (other, plain, mine):
            if p.poll() is None:
                p.kill()
                p.wait()
