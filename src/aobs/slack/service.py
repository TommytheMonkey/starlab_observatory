"""The optional Slack integration (brief 9). Nothing here runs, or is even imported, while it is disabled.

Notifications are derived from Starlab's own records, so nothing has to be wired into the scheduler:
  recording start / completion / failure   job state changes (sched_job_events, read by cursor)
  decoded images                           decode attempts that produced an image (the images are uploaded)
  upcoming passes                          booked recordings starting within N minutes, announced once
  receiver offline                         a receiver that stops answering, then again when it is back
  storage warnings                         a storage disk below 1.5 × the reserve kept free (at most every 6 h)
Quiet jobs (signal-path tests) are never announced. Cursors live in <data>/run/slack-state.json; on the first
start they are set to "now", so no history is replayed.

Slash commands arrive over Socket Mode (outbound connection, App-Level Token; needs the optional slack_sdk
package) or as signed HTTPS requests to /slack/commands (verified with the Signing Secret, ±5 min).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import pathlib
import threading
import time
import urllib.parse
from typing import Any, Callable

from sqlalchemy import and_, select

from ..db.schema import proc_attempts, sched_job_events, sched_jobs
from ..logjson import log_event
from .api import SlackApi, SlackError
from .commands import Commands, RateLimiter
from .credentials import Credentials

LOG = logging.getLogger("aobs.slack")
FAIL_STATES = ("failed", "missed", "partial")


def socket_mode_available() -> bool:
    try:
        import slack_sdk.socket_mode.builtin  # noqa: F401
        return True
    except ImportError:
        return False


def verify_signature(secret: str, timestamp: str, body: bytes, signature: str, now: float | None = None) -> bool:
    """Slack's v0 request signature, with a 5-minute replay window."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - ts) > 300:
        return False
    base = b"v0:" + str(ts).encode() + b":" + body
    want = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature or "")


class SlackService:
    def __init__(self, s: Any, status: Any = None, catalog: Any = None, transport: Any = None,
                 socket_factory: Callable[..., Any] | None = None):
        self.s, self.status, self.catalog = s, status, catalog
        self.creds = Credentials(s.config.secrets_dir)
        self.transport = transport
        self.socket_factory = socket_factory
        self.state_path = s.config.data_root / "run" / "slack-state.json"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: Any = None
        self.last_error: str | None = None
        self.last_post: str | None = None
        self.connected: bool | None = None
        self._limiter = RateLimiter(int(self.cfg().get("rate_limit_per_minute") or 20))
        self.commands = Commands(s, self.cfg, self._limiter, status, catalog)
        self._present: dict[str, bool] = {}
        self._storage_warned_at = 0.0
        self._post_times: list[float] = []

    # ------------------------------------------------------------ configuration
    def cfg(self) -> dict[str, Any]:
        store = getattr(self.s, "config_store", None)
        cur = store.current("slack") if store is not None else None
        from ..settings.store import DOCS
        base = {f.key: f.default for f in DOCS["slack"].fields}
        return {**base, **((cur or {}).get("data") or {})}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg().get("enabled"))

    def api(self) -> SlackApi:
        tok = self.creds.load().get("bot_token")
        if not tok:
            raise SlackError("auth", "no_bot_token", "add the Bot User OAuth Token in Settings → Slack")
        return SlackApi(tok, self.transport)

    def overview(self) -> dict[str, Any]:
        from .manifest import problems
        cfg = self.cfg()
        return {"enabled": bool(cfg.get("enabled")), "settings": cfg, "credentials": self.creds.status(),
                "running": self._thread is not None and self._thread.is_alive(), "socket_connected": self.connected,
                "socket_mode_available": socket_mode_available(), "last_error": self.last_error,
                "last_post": self.last_post, "problems": problems(cfg)}

    # ------------------------------------------------------------ connection test
    def test(self, post_message: bool = False) -> dict[str, Any]:
        cfg, have = self.cfg(), self.creds.load()
        out: dict[str, Any] = {"checks": []}

        def add(name: str, ok: bool, detail: str) -> None:
            out["checks"].append({"check": name, "ok": ok, "detail": detail})
        if not have.get("bot_token"):
            add("bot token", False, "not set")
        else:
            try:
                a = self.api().call("auth.test")
                add("bot token", True, f"workspace {a.get('team')}, bot user {a.get('user')}")
                granted = {x.strip() for x in (a.get("_scopes") or "").split(",") if x.strip()}
                from .manifest import scopes
                missing = [x for x in scopes(cfg) if granted and x not in granted]
                add("scopes", not missing, "all granted" if not missing else
                    f"missing {', '.join(missing)}: update the app from the manifest and reinstall it")
            except SlackError as exc:
                add("bot token", False, exc.error)
        if cfg.get("channel_id") and have.get("bot_token"):
            try:
                c = self.api().call("conversations.info", channel=cfg["channel_id"])
                ch = c.get("channel") or {}
                add("channel", True, f"#{ch.get('name', cfg['channel_id'])}" +
                    ("" if ch.get("is_member", True) else " — invite the app to the channel (/invite @app)"))
            except SlackError as exc:
                add("channel", False, exc.error + (" — invite the app to the channel" if exc.error in
                                                   ("channel_not_found", "not_in_channel") else ""))
        elif not cfg.get("channel_id"):
            add("channel", False, "no channel ID set (notifications need one)")
        if cfg.get("mode", "socket") == "socket" and cfg.get("commands_enabled"):
            if not have.get("app_token"):
                add("app-level token", False, "Socket Mode needs the App-Level Token (scope connections:write)")
            else:
                try:
                    SlackApi(have["app_token"], self.transport).call("apps.connections.open")
                    add("app-level token", True, "Socket Mode connection allowed")
                except SlackError as exc:
                    add("app-level token", False, exc.error)
                if not socket_mode_available():
                    add("socket mode library", False, "install the optional package: pip install 'starlab[slack]'")
        if cfg.get("mode") == "http" and cfg.get("commands_enabled"):
            add("signing secret", bool(have.get("signing_secret")), "set" if have.get("signing_secret")
                else "HTTP mode needs the Signing Secret to verify Slack's requests")
        if post_message and all(c["ok"] for c in out["checks"] if c["check"] in ("bot token", "channel")):
            try:
                self._post(f":satellite: Starlab is connected to this channel ({self.s.config.section('site').get('name') or 'observatory'}).")
                add("test message", True, "posted")
            except SlackError as exc:
                add("test message", False, exc.error)
        out["ok"] = all(c["ok"] for c in out["checks"])
        return out

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.enabled or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._init_state()
        self._thread = threading.Thread(target=self._loop, name="slack", daemon=True)
        self._thread.start()
        cfg = self.cfg()
        if cfg.get("commands_enabled") and cfg.get("mode", "socket") == "socket":
            self._start_socket()
        log_event(LOG, "slack integration started", mode=cfg.get("mode"), commands=bool(cfg.get("commands_enabled")))

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:  # noqa: BLE001
                pass
            self._socket = None
            self.connected = None
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def reload(self) -> dict[str, Any]:
        """Apply saved settings and credentials now (stop, then start if enabled)."""
        self.stop()
        self._limiter.per_minute = max(1, int(self.cfg().get("rate_limit_per_minute") or 20))
        self.start()
        return self.overview()

    def _start_socket(self) -> None:
        have = self.creds.load()
        if not have.get("app_token") or not have.get("bot_token"):
            self.last_error = "Socket Mode needs the App-Level Token and the Bot User OAuth Token"
            return
        try:
            if self.socket_factory is not None:
                client = self.socket_factory(have["app_token"], have["bot_token"])
            else:
                from slack_sdk import WebClient
                from slack_sdk.socket_mode.builtin import SocketModeClient
                client = SocketModeClient(app_token=have["app_token"], web_client=WebClient(token=have["bot_token"]),
                                          logger=logging.getLogger("aobs.slack.socket"))
        except ImportError:
            self.last_error = "Socket Mode needs the optional package slack_sdk: pip install 'starlab[slack]'"
            return
        client.socket_mode_request_listeners.append(self._on_socket_request)
        try:
            client.connect()
            self.connected = True
        except Exception as exc:  # noqa: BLE001
            self.connected, self.last_error = False, f"Socket Mode connection failed: {type(exc).__name__}"
            return
        self._socket = client

    def _on_socket_request(self, client: Any, req: Any) -> None:
        payload: dict[str, Any] = {}
        if getattr(req, "type", None) == "slash_commands":
            p = req.payload or {}
            payload = self.handle_command(p.get("command", ""), p.get("text", ""), p.get("user_id", ""),
                                          p.get("user_name", ""), p.get("team_id", ""))
        try:
            from slack_sdk.socket_mode.response import SocketModeResponse
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id, payload=payload))
        except ImportError:                  # a test double without slack_sdk
            client.send_socket_mode_response({"envelope_id": req.envelope_id, "payload": payload})

    # ------------------------------------------------------------ commands (both modes)
    def handle_command(self, command: str, text: str, user_id: str, user_name: str = "", team_id: str = "") -> dict[str, Any]:
        cfg = self.cfg()
        want = "/" + str(cfg.get("command") or "starlab").lstrip("/")
        if not cfg.get("enabled") or not cfg.get("commands_enabled"):
            return {"response_type": "ephemeral", "text": "Slack commands are switched off on this observatory."}
        if command and command != want:
            return {"response_type": "ephemeral", "text": f"This app answers {want}."}
        log_event(LOG, "slack command", user=user_id, sub=(text or "help").split()[0][:20] if text else "help")
        return self.commands.handle(text, user_id, user_name)

    def handle_http(self, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        cfg = self.cfg()
        if not cfg.get("enabled") or cfg.get("mode") != "http" or not cfg.get("commands_enabled"):
            return 404, {"error": "Slack commands over HTTP are not enabled"}
        secret = self.creds.load().get("signing_secret")
        h = {k.lower(): v for k, v in headers.items()}
        if not secret or not verify_signature(secret, h.get("x-slack-request-timestamp", ""), body,
                                              h.get("x-slack-signature", "")):
            log_event(LOG, "slack request rejected: bad or old signature")
            return 401, {"error": "invalid signature"}
        form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode(errors="replace")).items()}
        return 200, self.handle_command(form.get("command", ""), form.get("text", ""), form.get("user_id", ""),
                                        form.get("user_name", ""), form.get("team_id", ""))

    # ------------------------------------------------------------ notifications
    def _init_state(self) -> None:
        st = self._state()
        if st.get("event_id") is None:
            with self.s.engine.connect() as conn:
                top = conn.execute(select(sched_job_events.c.id).order_by(sched_job_events.c.id.desc()).limit(1)).scalar()
            st.update(event_id=int(top or 0), decode_at=self.s.clock().isoformat(), announced=[])
            self._save(st)

    def _state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, st: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(".slack-state.json.tmp")
        tmp.write_text(json.dumps(st))
        os.replace(tmp, self.state_path)

    def _loop(self) -> None:
        while not self._stop.wait(20):
            self.tick()

    def tick(self) -> list[str]:
        """One pass over every notification source. Returns what was posted (for tests)."""
        cfg = self.cfg()
        if not cfg.get("enabled") or not cfg.get("channel_id"):
            return []
        posted: list[str] = []
        st = self._state()
        for fn in (self._job_events, self._decoded_images, self._upcoming, self._receivers, self._storage):
            try:
                posted += fn(cfg, st)
            except SlackError as exc:
                self.last_error = str(exc)
                LOG.warning("slack notification failed: %s", exc)
                break
            except Exception:  # noqa: BLE001
                LOG.exception("slack notifications: %s failed", fn.__name__)
        self._save(st)
        return posted

    def _post(self, text: str) -> None:
        now = time.monotonic()            # stay well under Slack's ~1 message/second per channel
        self._post_times = [t for t in self._post_times if now - t < 60]
        if len(self._post_times) >= 30:
            raise SlackError("chat.postMessage", "local_rate_limit", "more than 30 messages in a minute")
        self.api().post(self.cfg()["channel_id"], text)
        self._post_times.append(now)
        self.last_post = self.s.clock().isoformat()

    def _quiet(self, spec: Any) -> bool:
        return bool(((spec or {}).get("metadata") or {}).get("quiet"))

    def _job_events(self, cfg: dict[str, Any], st: dict[str, Any]) -> list[str]:
        with self.s.engine.connect() as conn:
            rows = conn.execute(select(sched_job_events, sched_jobs.c.target, sched_jobs.c.job_name, sched_jobs.c.receiver_id,
                                       sched_jobs.c.spec, sched_jobs.c.source)
                                .select_from(sched_job_events.join(sched_jobs, sched_jobs.c.job_id == sched_job_events.c.job_id))
                                .where(sched_job_events.c.id > int(st.get("event_id") or 0))
                                .order_by(sched_job_events.c.id).limit(200)).mappings().all()
        out = []
        for r in rows:
            st["event_id"] = r["id"]
            if self._quiet(r["spec"]):
                continue
            name = r["target"] or r["job_name"]
            new = r["new_status"]
            text = None
            if new == "recording" and cfg.get("notify_recording_start"):
                text = f":red_circle: Recording *{name}* on {r['receiver_id']} (`{r['job_id']}`)"
            elif new == "completed" and cfg.get("notify_completed"):
                text = f":white_check_mark: *{name}* on {r['receiver_id']} completed — {r['reason'] or ''} (`{r['job_id']}`)"
            elif new in FAIL_STATES and cfg.get("notify_failed"):
                icon = ":warning:" if new == "partial" else ":x:"
                text = f"{icon} *{name}* on {r['receiver_id']} {new}: {r['reason'] or 'no reason recorded'} (`{r['job_id']}`)"
            if text:
                self._post(text[:3000])
                out.append(text)
        return out

    def _decoded_images(self, cfg: dict[str, Any], st: dict[str, Any]) -> list[str]:
        since = dt.datetime.fromisoformat(st.get("decode_at") or self.s.clock().isoformat())
        with self.s.engine.connect() as conn:
            rows = conn.execute(select(proc_attempts, sched_jobs.c.target, sched_jobs.c.receiver_id, sched_jobs.c.spec)
                                .select_from(proc_attempts.join(sched_jobs, sched_jobs.c.job_id == proc_attempts.c.recording_id))
                                .where(and_(proc_attempts.c.kind == "decode", proc_attempts.c.status == "completed",
                                            proc_attempts.c.finished_at > since))
                                .order_by(proc_attempts.c.finished_at).limit(20)).mappings().all()
        out = []
        for r in rows:
            st["decode_at"] = r["finished_at"].isoformat()
            ins = (r["result"] or {}).get("inputs") or []
            if not cfg.get("notify_decoded_images") or self._quiet(r["spec"]) or not any(i.get("decoded") for i in ins):
                continue
            frames = sum(int(i.get("cadu_frames") or 0) for i in ins)
            text = f":frame_with_picture: *{r['target'] or r['recording_id']}* decoded on {r['receiver_id']}: {frames} frames (`{r['recording_id']}`)"
            self._post(text)
            out.append(text)
            from ..db.schema import artifacts
            with self.s.engine.connect() as conn:
                imgs = conn.execute(select(artifacts.c.path, artifacts.c.label).where(and_(
                    artifacts.c.attempt_id == r["attempt_id"], artifacts.c.kind == "image",
                    artifacts.c.deleted_at.is_(None)))).all()
            real = [i for i in imgs if "(blank)" not in (i.label or "") and pathlib.Path(i.path).is_file()
                    and pathlib.Path(i.path).stat().st_size < 50 * 1024**2]
            for img in real[:3]:                       # the best few; the archive has them all
                self.api().upload(cfg["channel_id"], img.path, f"{r['target'] or ''} {img.label or ''}".strip())
        return out

    def _upcoming(self, cfg: dict[str, Any], st: dict[str, Any]) -> list[str]:
        if not cfg.get("notify_upcoming"):
            return []
        now = self.s.clock()
        lead = dt.timedelta(minutes=int(cfg.get("upcoming_minutes") or 15))
        done = set(st.get("announced") or [])
        out = []
        for j in self.s.jobs(status=["scheduled"], start=now, end=now + lead, limit=50):
            if j["job_id"] in done or self._quiet(j.get("spec")) or not j.get("executes", True):
                continue
            start = dt.datetime.fromisoformat(j["record_start"])
            mins = max(0, round((start - now).total_seconds() / 60))
            meta = (j.get("spec") or {}).get("metadata") or {}
            el = f", max {meta['max_elevation_deg']:.0f}°" if meta.get("max_elevation_deg") is not None else ""
            text = f":satellite_antenna: *{j['target'] or j['job_name']}* in {mins} min on {j['receiver_id']}{el} (`{j['job_id']}`)"
            self._post(text)
            out.append(text)
            done.add(j["job_id"])
        st["announced"] = sorted(done)[-500:]
        return out

    def _receivers(self, cfg: dict[str, Any], st: dict[str, Any]) -> list[str]:
        if not cfg.get("notify_receiver_offline"):
            return []
        out = []
        for rx in list(self.s.receivers.values()):
            if getattr(rx, "simulated", False):
                continue
            try:
                here = bool(self.s._present(rx))
            except Exception:  # noqa: BLE001
                continue
            before = self._present.get(rx.id)
            self._present[rx.id] = here
            if before is None or before == here:
                continue                        # the first look only learns the state
            text = (f":electric_plug: *{rx.id}* is connected again." if here else
                    f":rotating_light: *{rx.id}* is not connected (no longer seen on {rx.host or 'local'}). "
                    "Its recordings will fail until it is back.")
            self._post(text)
            out.append(text)
        return out

    def _storage(self, cfg: dict[str, Any], st: dict[str, Any]) -> list[str]:
        if not cfg.get("notify_storage") or time.monotonic() - self._storage_warned_at < 6 * 3600:
            return []
        from ..scheduling.archive import storage_usage
        reserve = float(self.s.settings.get("reserve_free_gib", 64)) * 1024**3
        low = [d for d in storage_usage(self.s)["disks"] if d["free_bytes"] < 1.5 * reserve]
        if not low:
            return []
        d = low[0]
        text = (f":floppy_disk: Storage low: {d['free_bytes'] / 1e9:.0f} GB free on the disk holding "
                f"{', '.join(d['folders'])} (Starlab keeps {reserve / 1024**3:.0f} GiB free and refuses recordings below "
                "that). Review Recordings → Storage & retention.")
        self._post(text)
        self._storage_warned_at = time.monotonic()
        return [text]
