"""Development isolation: a dev broker that can never touch, or be confused with, production.

Incident, 2026-10-15: a cleanup `pkill -f "aobs broker"` aimed at a scratch broker also killed the
production broker (same user, same command name); systemd restarted it 5 s later. Rules since:

  * Development processes are started by `scripts/dev start` in their own session and process group,
    with a PID file in .dev/run/. `scripts/dev stop` signals only the process group recorded there, and
    only after /proc confirms it is that dev broker (its STARLAB_CONFIG is the dev config). Nothing
    is ever stopped by matching a name.
  * With STARLAB_DEV=1 (set by .dev/env.sh), anything that opens a database or starts a broker refuses
    unless the config, data folder and database are inside the dev root and the port is not a
    production port (assert_dev_safe).
  * A broker holds <data>/run/broker.lock, so two brokers can never share one database (broker_lock).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import Any, Iterator

PRODUCTION_PORTS = {8751}


class DevSafetyError(RuntimeError):
    pass


def dev_root() -> pathlib.Path | None:
    if os.environ.get("STARLAB_DEV") != "1":
        return None
    root = os.environ.get("STARLAB_DEV_ROOT")
    if not root:
        raise DevSafetyError("STARLAB_DEV=1 needs STARLAB_DEV_ROOT (source .dev/env.sh)")
    return pathlib.Path(root).resolve()


def _inside(path: str | os.PathLike[str], root: pathlib.Path) -> bool:
    p = pathlib.Path(path).resolve()
    return p == root or root in p.parents


def assert_dev_safe(config: Any, port: int | None = None) -> None:
    """In a dev environment, refuse anything that could reach production's config, data, database or port."""
    root = dev_root()
    if root is None:
        return
    problems = []
    if not _inside(config.path, root):
        problems.append(f"config {config.path} is outside {root}")
    if not _inside(config.data_root, root):
        problems.append(f"data folder {config.data_root} is outside {root}")
    url = str(config.database_url)
    if url.startswith("sqlite:///") and not _inside(url.removeprefix("sqlite:///"), root):
        problems.append(f"database {url} is outside {root}")
    elif not url.startswith("sqlite:///"):
        problems.append("only a SQLite database inside the dev root is allowed in development")
    token = config.section("broker").get("token_file")
    if token and not _inside(token, root):
        problems.append(f"API token file {token} is outside {root}")
    secrets = getattr(config, "secrets_dir", None)
    if secrets is not None and not _inside(secrets, root):
        problems.append(f"secrets folder {secrets} (API token, SSH key) is outside {root}")
    p = int(port if port is not None else config.section("broker").get("port", 8751))
    if p in PRODUCTION_PORTS:
        problems.append(f"port {p} is a production port")
    lo, hi = (config.section("sessions").get("port_range") or [5259, 5299])[:2]
    if int(lo) <= 5299 and int(hi) >= 5259:
        problems.append(f"session ports {lo}-{hi} overlap production's SDR++ range 5259-5299")
    if problems:
        raise DevSafetyError("development safety check failed: " + "; ".join(problems)
                             + ". Nothing was started or opened.")


@contextlib.contextmanager
def broker_lock(data_root: pathlib.Path) -> Iterator[pathlib.Path]:
    """Exclusive lock on <data>/run/broker.lock for the broker's lifetime, plus a PID file."""
    run = data_root / "run"
    run.mkdir(parents=True, exist_ok=True)
    lock = (run / "broker.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        other = (run / "broker.pid").read_text().strip() if (run / "broker.pid").exists() else "?"
        raise DevSafetyError(f"another Starlab broker (pid {other}) is already using {data_root}; "
                             "two brokers must never share a database") from None
    pidfile = run / "broker.pid"
    pidfile.write_text(f"{os.getpid()}\n")
    try:
        yield pidfile
    finally:
        with contextlib.suppress(OSError):
            pidfile.unlink()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


# ------------------------------------------------------------------ dev broker control
def _environ(pid: int) -> dict[str, str]:
    raw = pathlib.Path(f"/proc/{pid}/environ").read_bytes()
    return dict(x.split("=", 1) for x in raw.decode(errors="replace").split("\0") if "=" in x)


def is_dev_process(pid: int, dev_config: pathlib.Path) -> bool:
    """True only if pid is alive, ours, and was started with this dev config."""
    try:
        env = _environ(pid)
    except OSError:                        # gone, or another user's process (production may run as root)
        return False
    return pathlib.Path(env.get("STARLAB_CONFIG", "/nonexistent")).resolve() == dev_config.resolve() \
        and env.get("STARLAB_DEV") == "1"


def paths(root: pathlib.Path) -> dict[str, pathlib.Path]:
    return {"config": root / "observatory.toml", "run": root / "run", "pid": root / "run" / "dev-broker.pid",
            "log": root / "run" / "dev-broker.log"}


def start(root: pathlib.Path, argv: list[str]) -> int:
    p = paths(root)
    p["run"].mkdir(parents=True, exist_ok=True)
    if p["pid"].exists():
        pid = int(p["pid"].read_text().split()[0])
        if is_dev_process(pid, p["config"]):
            print(f"dev broker already running (pid {pid})")
            return 0
        p["pid"].unlink()
    env = dict(os.environ, STARLAB_DEV="1", STARLAB_DEV_ROOT=str(root), STARLAB_CONFIG=str(p["config"]),
               AOBS_CONFIG=str(p["config"]))
    log = p["log"].open("ab")
    proc = subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True)       # own session and process group
    p["pid"].write_text(f"{proc.pid}\n")
    print(f"dev broker started: pid {proc.pid}, process group {proc.pid}, log {p['log']}")
    return 0


def stop(root: pathlib.Path, timeout: float = 15) -> int:
    """Stop the dev broker recorded in the PID file. Never matches processes by name."""
    p = paths(root)
    if not p["pid"].exists():
        print("no dev broker PID file; nothing to stop")
        return 0
    pid = int(p["pid"].read_text().split()[0])
    if not is_dev_process(pid, p["config"]):
        print(f"pid {pid} is not the dev broker (gone, or a different process now); not signalling it")
        p["pid"].unlink()
        return 0
    os.killpg(os.getpgid(pid), signal.SIGTERM)          # its own group: started with a new session
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and is_dev_process(pid, p["config"]):
        time.sleep(0.2)
    if is_dev_process(pid, p["config"]):
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    p["pid"].unlink(missing_ok=True)
    print(f"dev broker {pid} stopped")
    return 0


def status(root: pathlib.Path) -> int:
    p = paths(root)
    pid = int(p["pid"].read_text().split()[0]) if p["pid"].exists() else None
    alive = pid is not None and is_dev_process(pid, p["config"])
    print(f"dev broker: {'running, pid ' + str(pid) if alive else 'not running'}; config {p['config']}")
    return 0


def main(argv: list[str]) -> int:
    root = dev_root()
    if root is None:
        print("run this through scripts/dev (it sources .dev/env.sh)", file=sys.stderr)
        return 2
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "start":
        return start(root, [sys.executable, "-m", "aobs.cli", "broker"])
    if cmd == "stop":
        return stop(root)
    if cmd == "status":
        return status(root)
    print("usage: scripts/dev start|stop|status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
