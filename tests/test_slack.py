"""Brief 9: optional Slack integration. Every Slack API call here goes to a fake; nothing reaches slack.com."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pytest

from aobs.slack.api import SlackApi, SlackError
from aobs.slack.commands import RateLimiter
from aobs.slack.credentials import CredentialError, Credentials
from aobs.slack.manifest import manifest, problems, scopes
from aobs.slack.service import SlackService, verify_signature
from test_scheduler import NOW, FakeRunnerHw, drain, env, rtl_job, run_job, submit  # noqa: F401

BOT = "xoxb-1111111111-2222222222-" + "a" * 24
APP = "xapp-1-A0000000000-1111111111-" + "b" * 32
SECRET = "0123456789abcdef0123456789abcdef"


class FakeSlack:
    """Records every call; answers like Slack."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.fail: dict[str, str] = {}
        self.ratelimit_once: set[str] = set()

    def __call__(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, dict[str, str], bytes]:
        if url.startswith("https://files.example/"):
            self.calls.append(("upload", {"bytes": len(body)}, headers))
            return 200, {}, b"OK"
        method = url.rsplit("/", 1)[-1]
        params = json.loads(body or b"{}")
        self.calls.append((method, params, headers))
        if method in self.ratelimit_once:
            self.ratelimit_once.discard(method)
            return 429, {"retry-after": "0"}, b""
        if method in self.fail:
            return 200, {}, json.dumps({"ok": False, "error": self.fail[method]}).encode()
        out: dict[str, Any] = {"ok": True}
        hdr = {}
        if method == "auth.test":
            out.update(team="Test Workspace", user="starlab")
            hdr["x-oauth-scopes"] = "chat:write,commands,files:write"
        elif method == "conversations.info":
            out["channel"] = {"name": "observatory", "is_member": True}
        elif method == "files.getUploadURLExternal":
            out.update(upload_url="https://files.example/upload?x=1", file_id="F123")
        elif method == "apps.connections.open":
            out["url"] = "wss://example/"
        return 200, hdr, json.dumps(out).encode()

    def posts(self) -> list[str]:
        return [p["text"] for m, p, _ in self.calls if m == "chat.postMessage"]


def enable(env: Any, **over: Any) -> None:
    from aobs.settings.store import DOCS
    data = {f.key: f.default for f in DOCS["slack"].fields}
    data.update(enabled=True, channel_id="C0123456", **over)
    cur = env.s.config_store.current("slack")
    env.s.config_store.save("slack", data, cur["version"] if cur else None, "test")


def service(env: Any, fake: FakeSlack | None = None, **kw: Any) -> tuple[SlackService, FakeSlack]:
    fake = fake or FakeSlack()
    sv = SlackService(env.s, transport=fake, **kw)
    sv.creds.update({"bot_token": BOT, "app_token": APP, "signing_secret": SECRET})
    return sv, fake


# ------------------------------------------------------------------ off by default
def test_slack_is_off_by_default_and_starlab_runs_without_it(env):
    sv = SlackService(env.s, transport=FakeSlack())
    assert sv.enabled is False and sv.cfg()["enabled"] is False
    sv.start()
    assert sv._thread is None and sv.tick() == []
    assert sv.handle_http({}, b"command=/starlab&text=status")[0] == 404
    assert sv.handle_command("/starlab", "status", "U1")["text"].startswith("Slack commands are switched off")
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    assert env.s.get(jid)["status"] == "completed"                     # the observatory works normally
    assert not sv.creds.path.exists()


# ------------------------------------------------------------------ manifest
def test_the_manifest_asks_only_for_what_the_enabled_features_need():
    base = {"mode": "socket", "commands_enabled": False, "notify_completed": False, "notify_failed": False,
            "notify_receiver_offline": False, "notify_storage": False, "notify_decoded_images": False}
    assert scopes(base) == [] and problems(base)
    m = manifest({**base, "notify_failed": True}, "Roof")
    assert m["oauth_config"]["scopes"]["bot"] == ["chat:write"] and "slash_commands" not in m["features"]
    assert m["settings"]["socket_mode_enabled"] is True
    m = manifest({**base, "commands_enabled": True, "notify_decoded_images": True, "command": "sky"}, "Roof")
    assert m["oauth_config"]["scopes"]["bot"] == ["chat:write", "files:write", "commands"]
    cmd = m["features"]["slash_commands"][0]
    assert cmd["command"] == "/sky" and "url" not in cmd                 # Socket Mode: no public address
    http = {**base, "mode": "http", "commands_enabled": True, "public_base_url": "https://obs.example.org"}
    m = manifest(http, "Roof")
    assert m["settings"]["socket_mode_enabled"] is False
    assert m["features"]["slash_commands"][0]["url"] == "https://obs.example.org/slack/commands"
    assert m["oauth_config"]["scopes"]["bot"] == ["chat:write", "commands"]
    assert "public https" in problems({**http, "public_base_url": "http://x"})[0]
    assert json.loads(json.dumps(m)) == m


# ------------------------------------------------------------------ credentials
def test_credentials_are_checked_private_write_only_and_never_logged_or_exported(env, caplog):
    c = Credentials(env.s.config.secrets_dir)
    with pytest.raises(CredentialError, match="Bot User OAuth Token"):
        c.update({"bot_token": "not-a-token"})
    with pytest.raises(CredentialError, match="unknown"):
        c.update({"password": "x"})
    caplog.set_level(logging.DEBUG)
    st = c.update({"bot_token": BOT, "signing_secret": SECRET})
    assert st["bot_token"]["set"] and st["signing_secret"]["set"] and not st["app_token"]["set"]
    assert BOT not in json.dumps(st)
    assert os.stat(c.path).st_mode & 0o777 == 0o600
    assert c.update({"bot_token": ""})["bot_token"]["set"]              # empty keeps
    assert not c.update({"bot_token": None})["bot_token"]["set"]        # null removes
    c.update({"bot_token": BOT})
    sv, fake = service(env)
    enable(env)
    sv.test()
    assert BOT not in caplog.text and SECRET not in caplog.text
    from aobs.settings.transfer import export
    exp = json.dumps(export(env.s), default=str)
    assert BOT not in exp and SECRET not in exp and APP not in exp and '"slack"' in exp


# ------------------------------------------------------------------ request verification and commands
def sign(body: bytes, ts: int | None = None, secret: str = SECRET) -> dict[str, str]:
    ts = ts or int(time.time())
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": sig}


def test_http_commands_need_a_fresh_valid_signature(env):
    body = urllib.parse.urlencode({"command": "/starlab", "text": "help", "user_id": "U1"}).encode()
    assert verify_signature(SECRET, sign(body)["X-Slack-Request-Timestamp"], body, sign(body)["X-Slack-Signature"])
    old = sign(body, int(time.time()) - 600)
    assert not verify_signature(SECRET, old["X-Slack-Request-Timestamp"], body, old["X-Slack-Signature"])
    sv, _ = service(env)
    enable(env, mode="http", public_base_url="https://obs.example.org")
    assert sv.handle_http(sign(body, secret="f" * 32), body)[0] == 401
    assert sv.handle_http(sign(body + b"x"), body)[0] == 401           # body tampered after signing
    code, out = sv.handle_http(sign(body), body)
    assert code == 200 and out["response_type"] == "ephemeral" and "/starlab status" in out["text"]


def test_read_only_commands_answer_from_starlab(env):
    sv, _ = service(env)
    enable(env)
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    st = sv.handle_command("/starlab", "status", "U1")["text"]
    assert "OBS-R02" in st and "connected" in st
    rec = sv.handle_command("/starlab", "recordings 3", "U1")["text"]
    assert jid in rec and "recorded" in rec
    assert "OBS-R05" in sv.handle_command("/starlab", "receivers", "U1")["text"]
    passes = sv.handle_command("/starlab", "passes", "U1")["text"]
    assert "latitude" in passes or "Passes" in passes or "No passes" in passes
    assert "This app answers /starlab" in sv.handle_command("/other", "status", "U1")["text"]
    assert "/starlab passes" in sv.handle_command("/starlab", "", "U1")["text"]


def test_recording_commands_need_the_switch_and_an_authorized_user(env):
    sv, _ = service(env)
    enable(env)
    jid = submit(env, rtl_job("to-cancel", satdump={"enabled": False}, postprocess={"decode": False}))["results"][0]["job_id"]
    assert "switched off" in sv.handle_command("/starlab", f"cancel {jid}", "U1")["text"]
    enable(env, recording_commands=True, authorized_users="U2, u3")
    denied = sv.handle_command("/starlab", f"cancel {jid}", "U1")["text"]
    assert "not allowed" in denied and "U1" in denied and env.s.get(jid)["status"] == "scheduled"
    ok = sv.handle_command("/starlab", f"cancel {jid}", "U3", "pat")["text"]
    assert "Cancelled" in ok and env.s.get(jid)["status"] == "cancelled"
    assert "slack:pat" in env.s.get(jid)["status_reason"]


def test_commands_are_rate_limited_per_user():
    t = [0.0]
    rl = RateLimiter(3, clock=lambda: t[0])
    assert [rl.allow("U1") for _ in range(4)] == [True, True, True, False]
    assert rl.allow("U2")
    t[0] = 61
    assert rl.allow("U1")


# ------------------------------------------------------------------ notifications
def test_notifications_follow_the_switches_and_never_replay_history(env):
    old = submit(env, rtl_job("before-slack", satdump={"enabled": False}, postprocess={"decode": False}))["results"][0]["job_id"]
    env.s.transition(old, "failed", "an old failure")
    sv, fake = service(env)
    enable(env, notify_recording_start=True, notify_receiver_offline=False, notify_storage=False)
    sv._init_state()
    assert sv.tick() == []                                              # nothing from before Slack was enabled
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    posted = sv.tick()
    assert any("Recording" in p and jid in p for p in posted) and any("completed" in p and jid in p for p in posted)
    assert all(m == "chat.postMessage" and p["channel"] == "C0123456" for m, p, _ in fake.calls)
    assert all(h["Authorization"] == f"Bearer {BOT}" for *_, h in fake.calls)
    assert sv.tick() == []                                              # each event once
    enable(env, notify_recording_start=False, notify_completed=False, notify_failed=True,
           notify_receiver_offline=False, notify_storage=False)
    test = submit(env, rtl_job("quiet-test", rx="OBS-R05", receiver__antenna="V-DIPOLE", start=100, end=110,
                               satdump={"enabled": False}, postprocess={"decode": False}, metadata={"quiet": True},
                               target={"type": "frequency", "name": "t"}))["results"][0]["job_id"]
    env.s.transition(test, "failed", "quiet failure")
    assert sv.tick() == []                                              # signal-path tests stay quiet


def test_upcoming_passes_are_announced_once_and_receivers_going_offline(env):
    sv, fake = service(env)
    enable(env, notify_upcoming=True, upcoming_minutes=15, notify_completed=False, notify_failed=False, notify_storage=False)
    sv._init_state()
    jid = submit(env, rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))["results"][0]["job_id"]
    assert not [p for p in sv.tick() if jid in p]                       # 60 min away
    env.clock.now = NOW + dt.timedelta(minutes=50)
    got = [p for p in sv.tick() if jid in p]
    assert len(got) == 1 and "in 10 min" in got[0]
    assert not [p for p in sv.tick() if jid in p]
    env.present.discard("OBS-R02")
    assert any("OBS-R02* is not connected" in p for p in sv.tick())
    env.present.add("OBS-R02")
    assert any("OBS-R02* is connected again" in p for p in sv.tick())


def test_decoded_images_are_posted_and_uploaded(env, tmp_path):
    from test_archive import fake_satdump
    sv, fake = service(env)
    enable(env, notify_completed=False, notify_receiver_offline=False, notify_storage=False)
    env.s.settings["satdump_path"] = fake_satdump(tmp_path, good=True)      # after the save (it reloads settings)
    sv._init_state()
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    runner.proc.submit(jid, "decode", {"search_offsets_hz": [0]})
    drain(runner.proc)
    posted = sv.tick()
    assert any("decoded" in p and "2 frames" in p for p in posted), (posted, runner.proc.attempts(jid)[-1]["status_reason"])
    methods = [m for m, *_ in fake.calls]
    assert methods.count("files.getUploadURLExternal") == 1 and "upload" in methods
    done = next(p for m, p, _ in fake.calls if m == "files.completeUploadExternal")
    assert done["channel_id"] == "C0123456" and done["files"][0]["id"] == "F123"


def test_slack_errors_and_rate_limits_are_handled(env):
    fake = FakeSlack()
    api = SlackApi(BOT, fake, sleep=lambda s: None)
    fake.ratelimit_once.add("chat.postMessage")
    assert api.post("C1", "hi")["ok"]                                   # retried once after Retry-After
    fake.fail["chat.postMessage"] = "not_in_channel"
    with pytest.raises(SlackError, match="not_in_channel"):
        api.post("C1", "hi")
    sv, fake = service(env, fake)
    enable(env)
    res = sv.test()
    chk = {c["check"]: c for c in res["checks"]}
    assert chk["bot token"]["ok"] and "Test Workspace" in chk["bot token"]["detail"] and chk["scopes"]["ok"]
    assert chk["app-level token"]["ok"]
    fake.fail["conversations.info"] = "channel_not_found"
    res = sv.test()
    assert not res["ok"] and "invite the app" in {c["check"]: c for c in res["checks"]}["channel"]["detail"]


# ------------------------------------------------------------------ socket mode
class FakeSocket:
    def __init__(self, app_token: str, bot_token: str) -> None:
        self.tokens = (app_token, bot_token)
        self.socket_mode_request_listeners: list[Any] = []
        self.sent: list[Any] = []
        self.closed = False

    def connect(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def send_socket_mode_response(self, r: Any) -> None:
        self.sent.append(r)


def test_socket_mode_answers_slash_commands_and_stops_cleanly(env):
    made: list[FakeSocket] = []

    def factory(app: str, bot: str) -> FakeSocket:
        made.append(FakeSocket(app, bot))
        return made[-1]
    sv, _ = service(env, socket_factory=factory)
    enable(env)
    sv.start()
    try:
        assert sv.connected and made[0].tokens == (APP, BOT) and sv.overview()["running"]
        import types
        req = types.SimpleNamespace(type="slash_commands", envelope_id="E1",
                                    payload={"command": "/starlab", "text": "help", "user_id": "U1"})
        made[0].socket_mode_request_listeners[0](made[0], req)
        sent = made[0].sent[0]
        payload = sent["payload"] if isinstance(sent, dict) else sent.payload
        assert (sent["envelope_id"] if isinstance(sent, dict) else sent.envelope_id) == "E1"
        assert "/starlab status" in payload["text"]
    finally:
        sv.stop()
    assert made[0].closed and not sv.overview()["running"]


def test_slack_api_round_trip_and_signed_http_endpoint(env):
    from aobs.coordination.api import create_app
    from aobs.coordination.broker import Broker
    from aobs.scheduling.planner import Planner
    from aobs.scheduling.runner import Runner
    from aobs.scheduling.service import SchedulerService
    from test_archive import Http
    from test_broker import FakeHw
    env.cfg.raw["broker"] = {"receivers": ["OBS-R05"]}
    svc = SchedulerService(env.s, Planner(env.s, predictor=lambda rx, h: []), Runner(env.s, FakeRunnerHw()))
    svc.start = lambda: None
    svc.slack.transport = FakeSlack()
    c = Http(create_app(Broker(env.cfg, FakeHw(), env.clock), "t", scheduler=svc))
    try:
        assert c("/v1/slack", auth=False)[0] == 401
        ov = c("/v1/slack")[1]
        assert ov["enabled"] is False and not ov["credentials"]["bot_token"]["set"]
        assert c("/v1/slack/credentials", "PUT", {"bot_token": "nope"})[0] == 400
        code, out, _ = c("/v1/slack/credentials", "PUT", {"bot_token": BOT, "signing_secret": SECRET})
        assert code == 200 and out["credentials"]["bot_token"]["set"] and BOT not in json.dumps(out)
        assert BOT not in json.dumps(c("/v1/slack")[1])
        m = c("/v1/slack/manifest")[1]["manifest"]
        assert m["settings"]["socket_mode_enabled"] is True
        code, raw, hdr = c("/v1/slack/manifest?download=1")
        assert code == 200 and "attachment" in hdr["content-disposition"]
        body = urllib.parse.urlencode({"command": "/starlab", "text": "help", "user_id": "U1"}).encode()
        assert c("/slack/commands", "POST", None, auth=False)[0] == 404        # HTTP mode not enabled
        enable(env, mode="http", public_base_url="https://obs.example.org")
        req = urllib.request.Request(f"http://127.0.0.1:{c.port}/slack/commands", data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded", **sign(body)})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 200 and "/starlab status" in json.loads(r.read())["text"]
        bad = urllib.request.Request(f"http://127.0.0.1:{c.port}/slack/commands", data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad, timeout=10)
        assert e.value.code == 401
        assert c("/v1/slack/test", "POST", {})[1]["checks"]
    finally:
        c.server.should_exit = True
