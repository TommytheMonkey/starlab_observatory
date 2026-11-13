"""/starlab slash commands. Plain rules, no language model.

  status                  receivers, storage, what is recording and what is next
  passes [hours]          predicted passes (default 24 h, at most 72)
  receivers               every receiver: connected, mode, antenna, last recording
  recordings [n]          the latest recordings with their outcomes (at most 15)
  record <satellite> [receiver]   book the next predicted pass        (authorized users only)
  cancel <job id>                 cancel a booked recording            (authorized users only)
  help

Recording commands are off unless Settings → Slack enables them, and then only the Slack user IDs listed
there may use them. Every user is rate limited (Settings → Slack). Replies are visible only to the person
who asked.
"""

from __future__ import annotations

import collections
import datetime as dt
import threading
import time
import zoneinfo
from typing import Any, Callable

PRIVILEGED = ("record", "cancel")


class RateLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic):
        self.per_minute, self.clock = max(1, int(per_minute)), clock
        self.hits: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            q = self.hits.setdefault(key, collections.deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


def authorized_users(cfg: dict[str, Any]) -> set[str]:
    return {u.strip().upper() for u in str(cfg.get("authorized_users") or "").replace(";", ",").split(",") if u.strip()}


class Commands:
    def __init__(self, s: Any, cfg: Callable[[], dict[str, Any]], limiter: RateLimiter | None = None,
                 status: Any = None, catalog: Any = None):
        self.s, self.cfg = s, cfg
        self.limiter = limiter or RateLimiter(int(cfg().get("rate_limit_per_minute") or 20))
        self.status, self.catalog = status, catalog

    # ------------------------------------------------------------ entry point
    def handle(self, text: str, user_id: str, user_name: str = "") -> dict[str, Any]:
        cfg = self.cfg()
        words = (text or "").strip().split()
        sub = words[0].lower() if words else "help"
        args = words[1:]
        if not self.limiter.allow(user_id or "?"):
            return self._reply("Too many commands; wait a minute and try again.")
        try:
            if sub in PRIVILEGED:
                if not cfg.get("recording_commands"):
                    return self._reply("Recording commands are switched off on this observatory (Settings → Slack).")
                if (user_id or "").upper() not in authorized_users(cfg):
                    return self._reply(f"You are not allowed to {sub} recordings here. An owner can add your Slack "
                                       f"user ID ({user_id}) in Settings → Slack → Authorized users.")
            fn = {"status": self.cmd_status, "passes": self.cmd_passes, "receivers": self.cmd_receivers,
                  "recordings": self.cmd_recordings, "record": self.cmd_record, "cancel": self.cmd_cancel}.get(sub)
            if fn is None:
                return self._reply(self.help(cfg))
            return self._reply(fn(args, f"slack:{user_name or user_id}"))
        except Exception as exc:  # noqa: BLE001 - every failure becomes a readable reply
            return self._reply(f"That did not work: {exc}")

    @staticmethod
    def _reply(text: str) -> dict[str, Any]:
        return {"response_type": "ephemeral", "text": text[:3900]}

    def help(self, cfg: dict[str, Any]) -> str:
        c = "/" + str(cfg.get("command") or "starlab").lstrip("/")
        lines = [f"`{c} status` — receivers, storage, what is recording and next",
                 f"`{c} passes [hours]` — predicted passes", f"`{c} receivers` — every receiver",
                 f"`{c} recordings [n]` — the latest recordings and what they achieved"]
        if cfg.get("recording_commands"):
            lines += [f"`{c} record <satellite> [receiver]` — book the next pass (authorized users)",
                      f"`{c} cancel <job id>` — cancel a booked recording (authorized users)"]
        return "\n".join(lines)

    # ------------------------------------------------------------ helpers
    def _tz(self) -> zoneinfo.ZoneInfo:
        try:
            return zoneinfo.ZoneInfo(self.s.config.display_tz)
        except Exception:  # noqa: BLE001
            return zoneinfo.ZoneInfo("America/New_York")

    def _t(self, iso: str | None) -> str:
        if not iso:
            return "—"
        t = dt.datetime.fromisoformat(str(iso))
        return t.astimezone(self._tz()).strftime("%a %-I:%M %p")

    def _passes(self) -> Any:
        from ..satellites.passes import PassService
        return PassService(self.s)

    # ------------------------------------------------------------ read-only commands
    def cmd_status(self, args: list[str], who: str) -> str:
        st = self.status.status() if self.status is not None else None
        if st is None:
            from ..scheduling.status import StatusService
            st = StatusService(self.s).status()
        lines = [f"*{self.s.config.section('site').get('name') or 'Observatory'}* — {self._t(st['at'])} station time"]
        for r in st["receivers"]:
            cur = r.get("current_job")
            lines.append(f"• *{r['receiver_id']}* {'connected' if r.get('connected') else '*not connected*'}, "
                         f"{r['state']}" + (f" — recording {cur['target'] or cur['job_name']}" if cur else ""))
        stg = st.get("storage") or {}
        if stg.get("free_gb") is not None:
            lines.append(f"Storage: {stg['free_gb']} GB free ({stg.get('state')})")
        if st.get("next_jobs"):
            n = st["next_jobs"][0]
            lines.append(f"Next: {n['target']} at {self._t(n['start'])} on {', '.join(n['receivers'])}")
        if st.get("recent_failures"):
            f = st["recent_failures"][0]
            lines.append(f"Last failure: {f['target'] or f['job_name']} {self._t(f['record_start'])} — {f.get('reason') or ''}"[:300])
        return "\n".join(lines)

    def cmd_passes(self, args: list[str], who: str) -> str:
        hours = 24.0
        if args:
            try:
                hours = min(max(float(args[0]), 1), 72)
            except ValueError:
                return "Give the number of hours, e.g. `passes 12`."
        got = self._passes().predicted(hours)
        if got.get("warning"):
            return got["warning"]
        rows = got["passes"][:15]
        if not rows:
            return f"No passes of the selected satellites in the next {hours:g} h."
        out = [f"*Passes in the next {hours:g} h* (station time)"]
        for p in rows:
            booked = ", ".join(f"{b['receiver_id']} ({b['status']})" for b in p.get("booked") or [])
            out.append(f"• {self._t(p['aos'])}–{self._t(p['los'])[4:]} *{p['name']}* max {p['max_elevation_deg']:.0f}°"
                       + (f" — booked on {booked}" if booked else ""))
        return "\n".join(out)

    def cmd_receivers(self, args: list[str], who: str) -> str:
        from ..scheduling.status import StatusService
        st = self.status if self.status is not None else StatusService(self.s)
        out = ["*Receivers*"]
        for rx in self.s.receivers.values():
            r = st.receiver(rx)
            last = r.get("last_successful_recording")
            out.append(f"• *{rx.id}* ({rx.driver}{', ' + rx.host if getattr(rx, 'host', 'local') not in (None, 'local') else ''})"
                       f" — {'connected' if r.get('connected') else 'not connected'}, mode {r['mode'].lower()}, "
                       f"antenna {rx.antenna or '—'}" + (f"; last recording {self._t(last['record_start'])}" if last else ""))
        return "\n".join(out)

    def cmd_recordings(self, args: list[str], who: str) -> str:
        n = 5
        if args and args[0].isdigit():
            n = min(max(int(args[0]), 1), 15)
        from ..scheduling.catalog import Catalog
        cat = self.catalog or Catalog(self.s)
        rows = cat.recordings(per_page=n)["recordings"]
        if not rows:
            return "No recordings yet."
        out = ["*Latest recordings*"]
        for r in rows:
            o = r.get("outcome") or {}
            parts = [{"complete": "recorded", "partial": "partial", "failed": "FAILED"}.get(o.get("recording"), r["status"])]
            if o.get("signal") == "detected":
                parts.append("signal")
            if o.get("frames") is not None:
                parts.append(f"{o['frames']} frames")
            if o.get("images"):
                parts.append(f"{o['images']} image(s)")
            out.append(f"• {r['start_local'][5:16]} *{r.get('target') or r.get('job_name')}* on {r['receiver']} — "
                       f"{', '.join(parts)} (`{r['recording_id']}`)")
        return "\n".join(out)

    # ------------------------------------------------------------ privileged
    def cmd_record(self, args: list[str], who: str) -> str:
        if not args:
            return "Which satellite? e.g. `record METEOR-M2 4` or `record 59051 <receiver>`."
        rx_id = args[-1] if args[-1].upper() in {r.upper() for r in self.s.receivers} and len(args) > 1 else None
        name = " ".join(args[:-1] if rx_id else args).upper().replace("-", " ")
        got = self._passes().predicted(48)
        if got.get("warning"):
            return got["warning"]
        now = self.s.clock()
        cand = [p for p in got["passes"] if (name == str(p["norad"]) or name in p["name"].upper().replace("-", " "))
                and dt.datetime.fromisoformat(p["aos"]) > now + dt.timedelta(minutes=3)]
        if not cand:
            return f"No pass of {name} in the next 48 h that starts more than 3 minutes from now."
        p = cand[0]
        if rx_id is None:
            rx_id = next((r.id for r in self.s.receivers.values() if r.mode in ("broker", "BROKER")), None)
            if rx_id is None:
                return "Name the receiver to record on, e.g. `record METEOR-M2 4 <receiver>`."
        rx_id = next(r for r in self.s.receivers if r.upper() == rx_id.upper())
        res = self._passes().schedule(p["norad"], p["aos"], rx_id, who)
        jid = res.get("job_id") or (res.get("result") or {}).get("job_id") or ""
        return f"Booked *{p['name']}* {self._t(p['aos'])}–{self._t(p['los'])[4:]} (max {p['max_elevation_deg']:.0f}°) on {rx_id}" + \
            (f" as `{jid}`" if jid else "") + "."

    def cmd_cancel(self, args: list[str], who: str) -> str:
        if not args:
            return "Which recording? Give its job id, e.g. `cancel m20261016…`."
        job = self.s.get(args[0])
        if job is None:
            return f"No job {args[0]}."
        if job["status"] not in ("scheduled",):
            return f"{args[0]} is {job['status']}; only a booked recording that has not started can be cancelled here."
        self.s.cancel(args[0], f"cancelled from Slack by {who}", who)
        return f"Cancelled {job.get('target') or job['job_name']} {self._t(job['record_start'])} on {job['receiver_id']}."
