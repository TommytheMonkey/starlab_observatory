"""The SatNOGS node `comet` (station 5254), reached over SSH through a locked-down gate.

The obsagent key on comet is forced to /usr/local/bin/satnogs-gate, which accepts only
`status`, `logs [n]` (n 1-500) and `restart-client`. Nothing here builds a shell command from
model input: the only variable argument is the validated line count.

Restarting the client is never done by a tool call. The tool only opens a request; the bot posts a
one-time code, and the restart runs when the owner types `confirm satnogs restart <code>` (see
Agent._handle). The model never sees the code, so it cannot confirm on the owner's behalf.

Config: ~/.config/observatory/satnogs.env with SATNOGS_HOST, SATNOGS_USER, SATNOGS_KEY,
SATNOGS_STATION_ID (optional SATNOGS_NAME).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import secrets
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Callable

LOG = logging.getLogger(__name__)

FETCH_FAILED = "Fetching jobs from network failed"
REPEATED_N = 3                          # this many failures ...
REPEATED_WINDOW = dt.timedelta(minutes=30)  # ... within this window = "repeated"
STREAK_GAP = dt.timedelta(minutes=5)    # the client polls once a minute; a bigger gap ends a streak
CONFIRM_TTL = dt.timedelta(minutes=10)
CONFIRM_RE = re.compile(r"^\s*confirm\s+satnogs\s+restart\s+(\d{4,8})\s*$", re.I)

_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z\s+(\w+)\s+(\S+)\s+(.*)$")
_CONTAINER = re.compile(r"^(\S+):\s+(\w+)(?:\s+since\s+(\S+))?(?:\s+restarts=(\d+))?(?:\s+image=(\S+))?\s*$")


class SatnogsError(RuntimeError):
    pass


def _ts(date: str, frac: str | None) -> dt.datetime:
    t = dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc)
    return t + dt.timedelta(microseconds=int((frac or "0")[:6].ljust(6, "0")))


def parse_status(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {"containers": [], "raw": text.strip()}
    for line in text.splitlines():
        m = _CONTAINER.match(line.strip())
        if m and (m[3] or m[4] or m[5] or m[1].startswith("satnogs_")):
            out["containers"].append({"name": m[1], "state": m[2], "since": m[3],
                                      "restarts": int(m[4]) if m[4] else None, "image": m[5]})
        elif ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def parse_logs(text: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        m = _LINE.match(line.strip())
        if m:
            out.append({"at": _ts(m[1], m[2]), "level": m[3], "logger": m[4], "msg": m[5]})
        elif out and line.strip():
            out[-1]["msg"] += "\n" + line.strip()   # continuation (traceback) lines
    return out


def _cause(msg: str) -> str:
    if "Network is unreachable" in msg or "No route to host" in msg:
        return "comet has no route to the internet (Network is unreachable)"
    if "Name or service not known" in msg or "Temporary failure in name resolution" in msg:
        return "DNS lookup of network.satnogs.org failed"
    if "timed out" in msg.lower() or "Read timed out" in msg:
        return "connection to network.satnogs.org timed out"
    if (m := re.search(r"\b(5\d\d) Server Error", msg)):
        return f"SatNOGS Network returned HTTP {m[1]} (server side)"
    if (m := re.search(r"\b(401|403) Client Error", msg)):
        return f"SatNOGS Network refused the station (HTTP {m[1]}): check the station API key"
    if (m := re.search(r"\b(4\d\d) Client Error", msg)):
        return f"SatNOGS Network returned HTTP {m[1]}"
    if "Connection refused" in msg or "Max retries exceeded" in msg:
        return "could not connect to network.satnogs.org"
    return msg.splitlines()[0][:200] if msg else "unknown"


def job_fetch_failures(entries: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any]:
    """Summarize 'Fetching jobs from network failed' lines. The client logs the reason on the line just before."""
    fails = []
    for i, e in enumerate(entries):
        if FETCH_FAILED in e["msg"]:
            prev = entries[i - 1] if i else None
            why = prev["msg"] if prev and FETCH_FAILED not in prev["msg"] and \
                abs((e["at"] - prev["at"]).total_seconds()) < 5 else ""
            fails.append({"at": e["at"], "cause": _cause(why)})
    out: dict[str, Any] = {"failures_in_log": len(fails),
                           "log_covers_since": entries[0]["at"].isoformat() if entries else None,
                           "flag": None}
    if not fails:
        return out
    last = fails[-1]["at"]
    streak = [fails[-1]]
    for f in reversed(fails[:-1]):
        if streak[0]["at"] - f["at"] > STREAK_GAP:
            break
        streak.insert(0, f)
    recent = [f for f in fails if now - f["at"] <= REPEATED_WINDOW]
    causes: dict[str, int] = {}
    for f in streak:
        causes[f["cause"]] = causes.get(f["cause"], 0) + 1
    out.update({"last_failure": last.isoformat(), "minutes_since_last": round((now - last).total_seconds() / 60, 1),
                "failures_last_30_min": len(recent), "streak_failures": len(streak),
                "streak_started": streak[0]["at"].isoformat(), "latest_cause": fails[-1]["cause"],
                "streak_causes": causes})
    if len(recent) >= REPEATED_N:
        out["flag"] = "repeated"
        out["summary"] = (f"WARNING: {len(recent)} 'Fetching jobs from network failed' errors in the last 30 min "
                          f"({len(streak)} in a row since {streak[0]['at']:%H:%MZ}); latest cause: {fails[-1]['cause']}. "
                          "The station cannot receive new observation jobs while this lasts.")
    elif len(streak) >= REPEATED_N:
        out["flag"] = "recent"
        out["summary"] = (f"{len(streak)} job-fetch failures from {streak[0]['at']:%H:%MZ} to {last:%H:%MZ} "
                          f"({fails[-1]['cause']}); none logged in the {out['minutes_since_last']:.0f} min since.")
    return out


@dataclass
class PendingRestart:
    code: str
    requested_at: dt.datetime
    expires: dt.datetime


Runner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


class SatnogsGate:
    def __init__(self, host: str, user: str, key: str, station_id: str, name: str = "comet",
                 runner: Runner = _run, clock: Callable[[], dt.datetime] | None = None):
        self.host, self.user, self.key, self.station_id, self.name = host, user, key, station_id, name
        self.runner = runner
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self._pending: PendingRestart | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "SatnogsGate | None":
        need = ("SATNOGS_HOST", "SATNOGS_USER", "SATNOGS_KEY", "SATNOGS_STATION_ID")
        if not all(env.get(k) for k in need):
            return None
        return cls(env["SATNOGS_HOST"], env["SATNOGS_USER"], env["SATNOGS_KEY"], env["SATNOGS_STATION_ID"],
                   env.get("SATNOGS_NAME") or "comet")

    # ---------------------------------------------------------------- transport
    def _gate(self, args: list[str], timeout: float = 30) -> str:
        argv = ["ssh", "-n", "-i", self.key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10", f"{self.user}@{self.host}", *args]
        try:
            p = self.runner(argv, timeout)
        except subprocess.TimeoutExpired as exc:
            raise SatnogsError(f"{self.name} did not answer within {timeout:.0f} s") from exc
        if p.returncode == 255:
            raise SatnogsError(f"cannot reach {self.name} over SSH: {(p.stderr or '').strip()[:300]}")
        if p.returncode != 0:
            raise SatnogsError(f"satnogs-gate on {self.name} refused or failed ({p.returncode}): "
                               f"{((p.stderr or '') + (p.stdout or '')).strip()[:300]}")
        return p.stdout

    # ---------------------------------------------------------------- read-only
    def logs(self, lines: int) -> str:
        n = int(lines)
        if not 1 <= n <= 500:
            raise ValueError("lines must be 1-500")
        return self._gate(["logs", str(n)])

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"node": self.name, "station_id": self.station_id,
                               "station_url": f"https://network.satnogs.org/stations/{self.station_id}/"}
        out.update(parse_status(self._gate(["status"])))
        flags = []
        client = next((c for c in out["containers"] if "satnogs-client" in c["name"]), None)
        if client is None:
            flags.append("satnogs-client container not listed")
        elif client["state"] != "running":
            flags.append(f"satnogs-client is {client['state']}")
        if out.get("sdr") and "present" not in out["sdr"].lower():
            flags.append(f"SDR: {out['sdr']}")
        try:
            out["job_fetch"] = job_fetch_failures(parse_logs(self.logs(500)), self.clock())
            if out["job_fetch"].get("flag") == "repeated":
                flags.append(out["job_fetch"]["summary"])
        except SatnogsError as exc:
            out["job_fetch"] = {"error": str(exc)}
        out["flags"] = flags
        out["healthy"] = not flags
        return out

    # ---------------------------------------------------------------- restart (owner-confirmed)
    def request_restart(self) -> PendingRestart:
        with self._lock:
            now = self.clock()
            self._pending = PendingRestart(f"{secrets.randbelow(10_000):04d}", now, now + CONFIRM_TTL)
            LOG.info("satnogs-client restart requested; awaiting owner confirmation until %s", self._pending.expires)
            return self._pending

    def confirm_restart(self, code: str) -> str:
        """Called only from the owner's own Slack message. Returns the text to post."""
        with self._lock:
            p, now = self._pending, self.clock()
            if p is None:
                return "No SatNOGS restart is waiting for confirmation, so nothing was restarted."
            if now > p.expires:
                self._pending = None
                return "That SatNOGS restart request expired, so nothing was restarted. Ask again if it is still needed."
            if not secrets.compare_digest(code, p.code):
                return "That code does not match the pending SatNOGS restart, so nothing was restarted."
            self._pending = None
        LOG.info("owner confirmed satnogs-client restart on %s", self.name)
        out = self._gate(["restart-client"], timeout=120).strip()
        return f":white_check_mark: Restarted satnogs-client on {self.name}." + (f"\n```{out[:1500]}```" if out else "")

    def cancel_restart(self) -> bool:
        with self._lock:
            had, self._pending = self._pending is not None, None
            return had

    def confirmation_prompt(self, p: PendingRestart) -> str:
        return (f":warning: *Restart satnogs-client on {self.name}* (station {self.station_id})? "
                "An observation in progress would be cut short.\n"
                f"To go ahead, reply `confirm satnogs restart {p.code}` within {int(CONFIRM_TTL.total_seconds() // 60)} "
                "minutes. Anything else leaves it alone.")
