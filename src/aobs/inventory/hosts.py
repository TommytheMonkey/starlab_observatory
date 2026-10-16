"""Receiver hosts: this computer, and remote Linux computers Starlab reaches over SSH.

A host runs commands; discovery and identification are the same code for every host (snapshot.py,
adapters.py). The SSH rules:

  * Key authentication only. Starlab has its own key pair (secrets/ssh/starlab_ed25519), or uses a key
    file the owner names. There is no password field anywhere, BatchMode stops ssh from ever asking for
    one, and a request that carries a password is refused.
  * Host keys are always checked (StrictHostKeyChecking=yes, against Starlab's own known_hosts built from
    the database; the user's ~/.ssh config and known_hosts are not used). A new host's key is fetched
    with ssh-keyscan and shown as fingerprints; the owner compares them with the host and accepts them,
    and the connection is then tested with checking on before anything is saved. A key that changes
    later stops every connection until the owner accepts the new key the same way.
  * A host is saved only after a successful test connection.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import ipaddress
import os
import pathlib
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

from sqlalchemy import Engine, select

from ..db.schema import inv_events, inv_hosts, inv_receivers
from ..timeutil import utc_now
from .store import ID_RE, InventoryError

HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9-]{1,63})*\.?$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
FPR_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
KEY_NAME = "starlab_ed25519"
LOCAL = "local"


@dataclasses.dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    error: str | None = None          # a plain-language reason when the connection itself failed
    error_kind: str | None = None     # host_key_changed | host_key_unknown | auth_failed | refused | timeout | ...


def classify_ssh_error(stderr: str) -> tuple[str, str] | None:
    s = stderr or ""
    if "REMOTE HOST IDENTIFICATION HAS CHANGED" in s:
        return "host_key_changed", ("the host's SSH key is not the one you accepted. This can mean the host was "
                                    "reinstalled, or that something is intercepting the connection. Nothing was run. "
                                    "Check the key on the host and accept the new one only if it matches.")
    if "Host key verification failed" in s or "No ED25519 host key is known" in s or "host key is known" in s:
        return "host_key_unknown", "the host's SSH key has not been accepted; nothing was run"
    if "Permission denied" in s:
        return "auth_failed", ("the host refused Starlab's SSH key (add Starlab's public key to "
                               "~/.ssh/authorized_keys of that user on the host)")
    if "Connection closed by" in s or "kex_exchange_identification" in s or "Connection reset" in s:
        return "closed", ("the host closed the connection before logging in. OpenSSH does this for a few seconds "
                          "after a key scan or failed logins from the same address (PerSourcePenalties), or when "
                          "too many connections are open (MaxStartups); try again shortly")
    if "Connection refused" in s:
        return "refused", "nothing accepts SSH connections on that port"
    if "timed out" in s.lower():
        return "timeout", "no answer from the host (switched off, wrong address, or a firewall)"
    if "Could not resolve hostname" in s or "Name or service not known" in s:
        return "dns", "the host name could not be resolved"
    if "No route to host" in s or "Network is unreachable" in s:
        return "unreachable", "no network route to the host"
    return None


# Programs that open an SDR. In development (STARLAB_DEV=1) and in tests (STARLAB_NO_HARDWARE=1) they are
# refused unless they are mocks (a host's path_prefix), on this computer and over SSH alike: this station's
# real receivers are recording, and development never opens them.
OPENS_DEVICES = frozenset({"rtl_test", "rtl_eeprom", "rtl_sdr", "rtl_biast", "rtl_tcp", "airspy_info", "airspy_rx",
                           "hackrf_info", "hackrf_transfer", "iio_info", "LimeUtil", "uhd_find_devices"})
SOAPY_OPENING = ("--probe", "--find", "--make", "--rate", "--sparse", "--check")


def opens_device(argv: list[str]) -> bool:
    tool = os.path.basename(argv[0]) if argv else ""
    return tool in OPENS_DEVICES or (tool == "SoapySDRUtil" and any(a.startswith(SOAPY_OPENING) for a in argv[1:]))


def hardware_blocked() -> str | None:
    # Under pytest the guard is on whatever the environment says: a test file outside tests/ (no conftest)
    # once ran the real airspy_info on this station (2026-10-15). Tests use mocks via path_prefix.
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return "this is a test run (pytest), which never opens a physical SDR"
    if os.environ.get("STARLAB_DEV") == "1":
        return "this is the development environment (STARLAB_DEV=1), which never opens a physical SDR"
    if os.environ.get("STARLAB_NO_HARDWARE") == "1":
        return "STARLAB_NO_HARDWARE=1 (tests) forbids opening a physical SDR"
    return None


def _guard(argv: list[str], path_prefix: str | None, local: bool) -> tuple[list[str], RunResult | None]:
    """In development and tests a device-opening tool runs only as the mock in path_prefix, by absolute path, so
    a missing mock can never fall through to the real program on PATH."""
    why = hardware_blocked()
    if not why or not opens_device(argv):
        return argv, None
    mock = f"{path_prefix}/{os.path.basename(argv[0])}" if path_prefix else None
    if mock and (not local or os.access(mock, os.X_OK)):
        return [mock, *argv[1:]], None
    return argv, RunResult(126, "", "", f"not run: {why}", "blocked")


def _run(argv: list[str], input: str | None, timeout: float, env: dict[str, str] | None = None) -> RunResult:
    try:
        r = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout, env=env,
                           stdin=None if input is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return RunResult(124, "", "", f"no answer within {timeout:.0f} s", "timeout")
    except OSError as exc:
        return RunResult(127, "", str(exc), f"could not run {argv[0]}: {exc}", "missing")
    return RunResult(r.returncode, r.stdout or "", r.stderr or "")


class LocalHost:
    """This computer. `path_prefix` (tests only) puts mocked tools first on PATH."""
    id = LOCAL
    label = "this computer"
    remote = False

    def __init__(self, path_prefix: str | None = None):
        self.path_prefix = path_prefix

    def run(self, argv: list[str], input: str | None = None, timeout: float = 60) -> RunResult:
        argv, blocked = _guard(argv, self.path_prefix, local=True)
        if blocked:
            return blocked
        env = None
        if self.path_prefix:
            env = dict(os.environ, PATH=f"{self.path_prefix}:{os.environ.get('PATH', '')}")
        return _run(argv, input, timeout, env)


class SshHost:
    """A remote host. Every command goes through ssh with host key checking on and key authentication only."""
    remote = True

    def __init__(self, id: str, hostname: str, port: int, username: str, key_file: pathlib.Path,
                 known_hosts: pathlib.Path, path_prefix: str | None = None, connect_timeout: int = 10):
        self.id, self.hostname, self.port, self.username = id, hostname, int(port), username
        self.key_file, self.known_hosts, self.path_prefix = key_file, known_hosts, path_prefix
        self.connect_timeout = connect_timeout
        self.label = f"{username}@{hostname}:{port}"

    def command(self, argv: list[str]) -> list[str]:
        remote = shlex.join(argv)
        if self.path_prefix:                       # tests: mocked SDR tools on the remote PATH
            remote = f"PATH={shlex.quote(self.path_prefix)}:\"$PATH\"; export PATH; {remote}"
        return ["ssh", "-F", "/dev/null", "-T",
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                "-o", "IdentitiesOnly=yes", "-o", f"ConnectTimeout={self.connect_timeout}",
                "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
                "-i", str(self.key_file), "-p", str(self.port), "-l", self.username, "--", self.hostname, remote]

    def run(self, argv: list[str], input: str | None = None, timeout: float = 60) -> RunResult:
        argv, blocked = _guard(argv, self.path_prefix, local=False)
        if blocked:
            return blocked
        r = _run(self.command(argv), input, timeout)
        if r.returncode == 255 and r.error is None:
            c = classify_ssh_error(r.stderr)
            r.error_kind, r.error = c if c else ("ssh", f"SSH failed: {r.stderr.strip()[-300:]}")
        return r


# ------------------------------------------------------------------ host keys
def check_address(hostname: str, port: Any) -> tuple[str, int]:
    h = str(hostname or "").strip()
    try:
        ipaddress.ip_address(h)
    except ValueError:
        if not HOSTNAME_RE.match(h):
            raise InventoryError("host name: letters, digits, dots and dashes (or an IP address)", 400) from None
    try:
        p = int(port or 22)
    except (TypeError, ValueError):
        raise InventoryError("SSH port must be a number", 400) from None
    if not 1 <= p <= 65535:
        raise InventoryError("SSH port must be 1-65535", 400)
    return h, p


def fingerprints(lines: list[str], run: Callable[..., RunResult] = _run) -> list[dict[str, str]]:
    """known_hosts lines -> [{type, fingerprint, line}] using ssh-keygen (SHA256, as `ssh-keygen -lf` prints)."""
    out = []
    for line in lines:
        r = run(["ssh-keygen", "-l", "-f", "-"], line + "\n", 10)
        m = re.match(r"^\d+\s+(SHA256:\S+)\s+.*\((\S+)\)\s*$", r.stdout.strip())
        if r.returncode == 0 and m:
            out.append({"type": m.group(2), "fingerprint": m.group(1), "line": line})
    return out


def scan_host_keys(hostname: str, port: int, run: Callable[..., RunResult] = _run, timeout: int = 5) -> list[dict[str, str]]:
    """The keys the host offers right now (ssh-keyscan). Shown to the owner to compare; never trusted by itself."""
    h, p = check_address(hostname, port)
    r = run(["ssh-keyscan", "-T", str(timeout), "-p", str(p), h], None, timeout + 10)
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        why = classify_ssh_error(r.stderr)
        raise InventoryError(f"could not read the SSH host key of {h}:{p}"
                             f"{': ' + why[1] if why else ''}", 502)
    return fingerprints(lines, run)


def client_key(secrets_dir: pathlib.Path, run: Callable[..., RunResult] = _run) -> dict[str, str]:
    """Starlab's own SSH key pair, created on first use. The private key never leaves this computer."""
    d = secrets_dir / "ssh"
    key = d / KEY_NAME
    if not key.exists():
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        r = run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"starlab@{socket.gethostname()}",
                 "-f", str(key)], None, 30)
        if r.returncode != 0:
            raise InventoryError(f"could not create Starlab's SSH key: {r.stderr.strip() or r.error}", 500)
    pub = (key.with_suffix(".pub")).read_text().strip()
    return {"private_key": str(key), "public_key": pub,
            "fingerprint": next((f["fingerprint"] for f in fingerprints([f"x {pub}"], run)), "")}


# ------------------------------------------------------------------ registered hosts
def _public(row: dict[str, Any]) -> dict[str, Any]:
    d = {k: (v.isoformat() if isinstance(v, dt.datetime) else v) for k, v in row.items() if k != "host_keys"}
    d["host_key_types"] = [f.get("type") for f in row.get("fingerprints") or []]
    return d


class HostStore:
    """inv_hosts rows (schema 5) plus the SSH plumbing for them."""

    def __init__(self, engine: Engine, secrets_dir: pathlib.Path, run_dir: pathlib.Path,
                 clock: Callable[[], dt.datetime] = utc_now, run: Callable[..., RunResult] = _run,
                 path_prefix: str | None = None, local: LocalHost | None = None):
        self.engine, self.secrets_dir, self.run_dir, self.clock, self._run = engine, secrets_dir, run_dir, clock, run
        self.path_prefix = path_prefix               # tests: mocked SDR tools on remote hosts
        self.local = local or LocalHost()

    # -------------------------------------------------------------- reading
    def rows(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(inv_hosts).order_by(inv_hosts.c.id)).mappings()]

    def get(self, hid: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            r = conn.execute(select(inv_hosts).where(inv_hosts.c.id == hid)).mappings().first()
        if r is None:
            raise InventoryError(f"no receiver host {hid}", 404)
        return dict(r)

    def listing(self) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        with self.engine.connect() as conn:
            for r in conn.execute(select(inv_receivers.c.host)):
                counts[r.host] = counts.get(r.host, 0) + 1
        return [{**_public(r), "receivers": counts.get(r["id"], 0)} for r in self.rows()]

    def host(self, hid: str) -> LocalHost | SshHost:
        if hid == LOCAL:
            return self.local
        row = self.get(hid)
        if not row["enabled"]:
            raise InventoryError(f"receiver host {hid} is disabled", 409)
        return self._ssh(row, self._known_hosts_file(hid, row["host_keys"]))

    def _ssh(self, row: dict[str, Any], known: pathlib.Path) -> SshHost:
        key = pathlib.Path(row.get("key_file") or self.secrets_dir / "ssh" / KEY_NAME)
        return SshHost(row["id"], row["hostname"], row["port"], row["username"], key, known, self.path_prefix)

    def _known_hosts_file(self, hid: str, lines: list[str]) -> pathlib.Path:
        d = self.run_dir / "ssh"
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        f = d / f"known_hosts-{hid}"
        tmp = f.with_suffix(".tmp")
        tmp.write_text("".join(ln + "\n" for ln in lines))
        os.chmod(tmp, 0o600)
        tmp.replace(f)
        return f

    def _event(self, conn: Any, hid: str, actor: str, action: str, detail: dict[str, Any] | None = None) -> None:
        conn.execute(inv_events.insert().values(receiver_id=f"host:{hid}"[:32], at=self.clock(), actor=actor[:128],
                                                action=action, detail=detail))

    # -------------------------------------------------------------- connecting
    def client_key(self) -> dict[str, str]:
        k = client_key(self.secrets_dir, self._run)
        return {"public_key": k["public_key"], "fingerprint": k["fingerprint"],
                "install": "On the remote host, as the user Starlab will log in as, append this line to "
                           "~/.ssh/authorized_keys (mode 600, folder ~/.ssh mode 700)."}

    def scan(self, body: dict[str, Any]) -> dict[str, Any]:
        h, p = check_address(body.get("hostname"), body.get("port"))
        keys = scan_host_keys(h, p, self._run)
        return {"hostname": h, "port": p, "keys": [{"type": k["type"], "fingerprint": k["fingerprint"]} for k in keys],
                "compare": "On the host, run: for f in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf \"$f\"; done "
                           "and accept only if a fingerprint here matches one printed there."}

    def _accepted_lines(self, hostname: str, port: int, accepted: Any) -> tuple[list[str], list[dict[str, str]]]:
        acc = [str(a).strip() for a in (accepted or []) if FPR_RE.match(str(a).strip())]
        if not acc:
            raise InventoryError("accept at least one host key fingerprint (SHA256:...) after comparing it with the host",
                                 400)
        keys = scan_host_keys(hostname, port, self._run)
        chosen = [k for k in keys if k["fingerprint"] in acc]
        if not chosen:
            raise InventoryError("the host is not offering any key you accepted now. Its key may have changed since "
                                 "you looked; check the fingerprints again. Nothing was saved.", 409)
        return [k["line"] for k in chosen], [{"type": k["type"], "fingerprint": k["fingerprint"]} for k in chosen]

    def _test(self, host: SshHost, retry_closed: bool = False) -> dict[str, Any]:
        out = self._test_once(host)
        if retry_closed and not out["ok"] and out.get("kind") == "closed":
            time.sleep(3)                         # right after ssh-keyscan, sshd may briefly turn this address away
            out = self._test_once(host)
        return out

    def _test_once(self, host: SshHost) -> dict[str, Any]:
        from . import snapshot
        at = self.clock().isoformat()
        try:
            snap = snapshot.take(host, timeout=60)
        except snapshot.SnapshotError as exc:
            return {"ok": False, "at": at, "kind": exc.kind, "error": str(exc)}
        return {"ok": True, "at": at, **snap.summary()}

    @staticmethod
    def _reject_password(body: dict[str, Any]) -> None:
        if any(k in body for k in ("password", "passphrase", "ssh_password")):
            raise InventoryError("Starlab does not take or store SSH passwords. Use key authentication: add Starlab's "
                                 "public key to the host (Settings → Receiver hosts shows it).", 400)

    def _key_file(self, body: dict[str, Any]) -> str | None:
        kf = str(body.get("key_file") or "").strip()
        if not kf:
            self.client_key()                                       # make sure Starlab's own key exists
            return None
        p = pathlib.Path(kf)
        if not p.is_absolute() or not p.is_file() or not os.access(p, os.R_OK):
            raise InventoryError(f"key file {kf} must be an absolute path to a private key Starlab can read", 400)
        return str(p)

    def add(self, body: dict[str, Any], actor: str) -> dict[str, Any]:
        """Register a host: accepted host key + successful test connection, or nothing is saved."""
        self._reject_password(body)
        hid = str(body.get("id") or "").strip()
        if not ID_RE.match(hid) or hid == LOCAL:
            raise InventoryError("host ID: 2-32 letters, digits, - or _ (not 'local')", 400)
        h, p = check_address(body.get("hostname"), body.get("port"))
        user = str(body.get("username") or "").strip()
        if not USER_RE.match(user):
            raise InventoryError("user name: a Linux login name (lower-case letters, digits, _ . -)", 400)
        with self.engine.connect() as conn:
            if conn.execute(select(inv_hosts.c.id).where(inv_hosts.c.id == hid)).first():
                raise InventoryError(f"there is already a receiver host {hid}")
            if conn.execute(select(inv_hosts.c.id).where(inv_hosts.c.hostname == h, inv_hosts.c.port == p,
                                                         inv_hosts.c.username == user)).first():
                raise InventoryError(f"{user}@{h}:{p} is already registered")
        key_file = self._key_file(body)
        lines, fprs = self._accepted_lines(h, p, body.get("accept_fingerprints"))
        row = {"id": hid, "name": str(body.get("name") or "").strip()[:120] or hid, "hostname": h, "port": p,
               "username": user, "key_file": key_file, "host_keys": lines, "fingerprints": fprs}
        with tempfile.TemporaryDirectory(dir=self._tmp_dir()) as td:
            kh = pathlib.Path(td) / "known_hosts"
            kh.write_text("".join(ln + "\n" for ln in lines))
            test = self._test(self._ssh(row, kh), retry_closed=True)
        if not test["ok"]:
            raise InventoryError(f"test connection failed: {test['error']}. Nothing was saved.", 409)
        now = self.clock()
        with self.engine.begin() as conn:
            conn.execute(inv_hosts.insert().values(**row, enabled=True, last_test=test, revision=1, updated_by=actor,
                                                   created_at=now, updated_at=now))
            self._event(conn, hid, actor, "host_added", {"hostname": h, "port": p, "username": user,
                                                         "fingerprints": fprs, "test": test})
        return {**_public(self.get(hid)), "test": test}

    def _tmp_dir(self) -> str:
        d = self.run_dir / "ssh"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def test(self, hid: str, actor: str) -> dict[str, Any]:
        row = self.get(hid)
        test = self._test(self._ssh(row, self._known_hosts_file(hid, row["host_keys"])))
        with self.engine.begin() as conn:
            conn.execute(inv_hosts.update().where(inv_hosts.c.id == hid).values(last_test=test))
            if not test["ok"]:
                self._event(conn, hid, actor, "host_test_failed", test)
        return test

    def accept_key(self, hid: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
        """After a host key change: accept the new key (compared by the owner), test, then save."""
        row = self.get(hid)
        if int(body.get("revision") or 0) != row["revision"]:
            raise InventoryError(f"{hid} was changed elsewhere; reload and try again")
        lines, fprs = self._accepted_lines(row["hostname"], row["port"], body.get("accept_fingerprints"))
        with tempfile.TemporaryDirectory(dir=self._tmp_dir()) as td:
            kh = pathlib.Path(td) / "known_hosts"
            kh.write_text("".join(ln + "\n" for ln in lines))
            test = self._test(self._ssh(row, kh), retry_closed=True)
        if not test["ok"]:
            raise InventoryError(f"test connection with the new key failed: {test['error']}. The old key is kept.", 409)
        with self.engine.begin() as conn:
            conn.execute(inv_hosts.update().where(inv_hosts.c.id == hid).values(
                host_keys=lines, fingerprints=fprs, last_test=test, revision=row["revision"] + 1, updated_by=actor,
                updated_at=self.clock()))
            self._event(conn, hid, actor, "host_key_replaced", {"old": row["fingerprints"], "new": fprs})
        return {**_public(self.get(hid)), "test": test}

    def update(self, hid: str, body: dict[str, Any], actor: str) -> dict[str, Any]:
        """Name, enabled and recording settings ({python, work_dir}). A different address or user is a different
        host: remove and add it again."""
        self._reject_password(body)
        bad = set(body) - {"name", "enabled", "revision", "recording"}
        if bad:
            raise InventoryError(f"only the name, enabled and recording settings can be changed; to change "
                                 f"{sorted(bad)}, remove the host and add it again (its key is checked again)", 400)
        row = self.get(hid)
        if int(body.get("revision") or 0) != row["revision"]:
            raise InventoryError(f"{hid} was changed elsewhere; reload and try again")
        ch: dict[str, Any] = {}
        if "name" in body:
            ch["name"] = str(body["name"]).strip()[:120] or hid
        if "enabled" in body:
            ch["enabled"] = bool(body["enabled"])
        if "recording" in body:
            from ..scheduling.backends import BackendError, exec_config
            rec = body["recording"] if isinstance(body["recording"], dict) else {}
            extra = set(rec) - {"python", "work_dir"}
            if extra:
                raise InventoryError(f"recording settings are python and work_dir (not {sorted(extra)})", 400)
            try:
                ch["exec_config"] = exec_config({"id": hid, "exec_config": rec})
            except BackendError as exc:
                raise InventoryError(str(exc), 400) from None
        with self.engine.begin() as conn:
            conn.execute(inv_hosts.update().where(inv_hosts.c.id == hid).values(
                **ch, revision=row["revision"] + 1, updated_by=actor, updated_at=self.clock()))
            self._event(conn, hid, actor, "host_updated", ch)
        return _public(self.get(hid))

    def delete(self, hid: str, revision: int, actor: str) -> dict[str, Any]:
        row = self.get(hid)
        if row["revision"] != revision:
            raise InventoryError(f"{hid} was changed elsewhere; reload and try again")
        with self.engine.begin() as conn:
            rx = [r.id for r in conn.execute(select(inv_receivers.c.id).where(inv_receivers.c.host == hid))]
            if rx:
                raise InventoryError(f"receivers {', '.join(rx)} are configured on {hid}; remove them first")
            conn.execute(inv_hosts.delete().where(inv_hosts.c.id == hid))
            self._event(conn, hid, actor, "host_removed", {"hostname": row["hostname"]})
        f = self.run_dir / "ssh" / f"known_hosts-{hid}"
        f.unlink(missing_ok=True)
        return {"ok": True, "deleted": hid}
