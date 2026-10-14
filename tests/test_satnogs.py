from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "agent"))

from obsagent import satnogs as sn  # noqa: E402
from obsagent import tools  # noqa: E402

NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.timezone.utc)

STATUS = """satnogs_satnogs-client: running since 2026-10-13T23:05:23.712258454Z restarts=0 image=librespace/satnogs-client:2.1.4-694
satnogs_rigctld: running since 2026-10-13T22:19:08.067170353Z restarts=0 image=librespace/hamlib:4.5.4-39
uptime: up 13 hours, 29 minutes
temp:   29.8 C
disk:   11G used / 58G (19%)
sdr:    RTL-SDR present
"""

URL = "https://network.satnogs.org/api/jobs/?ground_station=5254"


def _fail(t: dt.datetime, cause: str) -> str:
    z = t.strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
    return (f"{z} ERROR    satnogsclient.scheduler.tasks   {cause}\n"
            f"{z} ERROR    satnogsclient.scheduler.tasks   Fetching jobs from network failed.\n")


UNREACH = (f"HTTPSConnectionPool(host='network.satnogs.org', port=443): Max retries exceeded with url: {URL} "
           "(Caused by NewConnectionError('...: Failed to establish a new connection: [Errno 101] Network is unreachable'))")


class FakeSsh:
    def __init__(self, logs: str = "", status: str = STATUS, rc: dict[str, int] | None = None):
        self.logs, self.status, self.rc, self.calls = logs, status, rc or {}, []

    def __call__(self, argv, timeout):
        assert argv[0] == "ssh" and "StrictHostKeyChecking=yes" in argv and "BatchMode=yes" in argv
        cmd = argv[argv.index("obsagent@100.127.141.72") + 1:]
        self.calls.append(cmd)
        out = {"status": self.status, "logs": self.logs, "restart-client": "restarted satnogs_satnogs-client\n"}[cmd[0]]
        if cmd[0] == "logs":
            out = "\n".join(self.logs.splitlines()[-int(cmd[1]):]) + "\n"
        return subprocess.CompletedProcess(argv, self.rc.get(cmd[0], 0), out, "")


def gate(fake: FakeSsh, now: dt.datetime = NOW) -> sn.SatnogsGate:
    clock = {"t": now}
    g = sn.SatnogsGate("100.127.141.72", "obsagent", "/home/tommy/.ssh/satnogs_gate", "5254", runner=fake,
                       clock=lambda: clock["t"])
    g._clock = clock  # type: ignore[attr-defined]
    return g


def test_status_parses_containers_and_host_facts():
    s = gate(FakeSsh()).status()
    names = {c["name"]: c for c in s["containers"]}
    assert names["satnogs_satnogs-client"]["state"] == "running" and names["satnogs_rigctld"]["restarts"] == 0
    assert s["temp"] == "29.8 C" and s["sdr"] == "RTL-SDR present" and s["uptime"].startswith("up 13 hours")
    assert s["healthy"] and s["flags"] == [] and s["job_fetch"]["failures_in_log"] == 0


def test_repeated_job_fetch_failures_are_flagged_with_cause():
    logs = "".join(_fail(NOW - dt.timedelta(minutes=m), UNREACH) for m in range(20, 0, -1))
    s = gate(FakeSsh(logs)).status()
    jf = s["job_fetch"]
    assert jf["flag"] == "repeated" and jf["failures_last_30_min"] == 20 and jf["streak_failures"] == 20
    assert "Network is unreachable" in jf["latest_cause"]
    assert not s["healthy"] and "Fetching jobs from network failed" in s["flags"][0]


def test_isolated_or_old_failures_are_not_flagged_as_repeated():
    one = _fail(NOW - dt.timedelta(minutes=5), "500 Server Error: Internal Server Error for url: " + URL)
    jf = gate(FakeSsh(one)).status()["job_fetch"]
    assert jf["flag"] is None and "HTTP 500" in jf["latest_cause"]
    old = "".join(_fail(NOW - dt.timedelta(hours=3, minutes=m), UNREACH) for m in range(10, 0, -1))
    jf = gate(FakeSsh(old)).status()["job_fetch"]
    assert jf["flag"] == "recent" and "none logged" in jf["summary"]


def test_stopped_client_is_flagged():
    st = STATUS.replace("satnogs-client: running", "satnogs-client: exited")
    s = gate(FakeSsh(status=st)).status()
    assert not s["healthy"] and "satnogs-client is exited" in s["flags"]


def test_logs_bounds_and_contains_filter():
    logs = _fail(NOW, UNREACH) + "2026-10-14T08:59:00.000Z INFO     satnogsclient   all good\n"
    fake = FakeSsh(logs)
    ctx = tools.ToolContext(broker=None, session=None, satnogs=gate(fake))  # type: ignore[arg-type]
    out = json.loads(tools.run_tool(ctx, "satnogs_logs", {"lines": 9999, "contains": "all good"}))
    assert out["requested_lines"] == 500 and out["returned_lines"] == 1 and fake.calls[-1] == ["logs", "500"]
    with pytest.raises(ValueError):
        gate(fake).logs(0)


def test_ssh_failures_are_reported_not_raised_as_success():
    g = gate(FakeSsh(rc={"status": 255}))
    with pytest.raises(sn.SatnogsError, match="cannot reach"):
        g.status()
    g = gate(FakeSsh(rc={"logs": 2}))
    assert "error" in g.status()["job_fetch"]


def test_restart_tool_only_requests_confirmation():
    fake = FakeSsh()
    g = gate(fake)
    ctx = tools.ToolContext(broker=None, session=None, satnogs=g)  # type: ignore[arg-type]
    out = tools.run_tool(ctx, "satnogs_restart_client", {})
    code = g._pending.code
    assert json.loads(out)["restarted"] is False and code not in out           # the model never sees the code
    assert not any(c[0] == "restart-client" for c in fake.calls)
    assert f"confirm satnogs restart {code}" in ctx.notices[0]


def test_confirmation_requires_matching_unexpired_code_and_is_single_use():
    fake = FakeSsh()
    g = gate(fake)
    assert "nothing was restarted" in g.confirm_restart("1234")
    p = g.request_restart()
    wrong = f"{(int(p.code) + 1) % 10000:04d}"
    assert "does not match" in g.confirm_restart(wrong)
    assert "Restarted satnogs-client" in g.confirm_restart(p.code)
    assert [c for c in fake.calls if c[0] == "restart-client"] == [["restart-client"]]
    assert "nothing was restarted" in g.confirm_restart(p.code)                  # single use
    p = g.request_restart()
    g._clock["t"] = NOW + sn.CONFIRM_TTL + dt.timedelta(seconds=1)
    assert "expired" in g.confirm_restart(p.code)
    assert len([c for c in fake.calls if c[0] == "restart-client"]) == 1


def test_strict_tool_limits_still_hold():
    strict = [t for t in tools.TOOLS if t["strict"]]
    assert len(strict) <= tools.MAX_STRICT_TOOLS
    assert {"satnogs_status", "satnogs_logs", "satnogs_restart_client"} <= {t["name"] for t in tools.TOOLS}


@pytest.fixture
def agent_module(monkeypatch):
    """agent.py imports slack_bolt and anthropic, which only the bot host has; stub them."""
    bolt = types.ModuleType("slack_bolt")
    bolt.App = object  # type: ignore[attr-defined]
    sock = types.ModuleType("slack_bolt.adapter.socket_mode")
    sock.SocketModeHandler = object  # type: ignore[attr-defined]
    for name, mod in {"slack_bolt": bolt, "slack_bolt.adapter": types.ModuleType("slack_bolt.adapter"),
                      "slack_bolt.adapter.socket_mode": sock, "anthropic": types.ModuleType("anthropic")}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, "obsagent.agent", raising=False)
    import obsagent.agent as agent
    return agent


class FakeSlack:
    def __init__(self):
        self.posts = []

    def chat_postMessage(self, **kw):
        self.posts.append(kw)

    def reactions_add(self, **kw):
        pass

    def reactions_remove(self, **kw):
        pass

    def auth_test(self):
        return {"user_id": "UBOT"}


def _agent(agent_module, g):
    a = object.__new__(agent_module.Agent)
    a.owner, a.satnogs, a._seen = "UOWNER", g, []
    a._converse = lambda key, text: pytest.fail("the model must not handle a confirmation")  # type: ignore[assignment]
    return a


def test_only_the_owners_typed_code_restarts(agent_module):
    fake = FakeSsh()
    g = gate(fake)
    a, slack = _agent(agent_module, g), FakeSlack()
    p = g.request_restart()
    a._handle({"user": "USOMEONE", "text": f"confirm satnogs restart {p.code}", "channel": "C1", "ts": "1.0"},
              slack, key="C1:dm", thread_ts=None)
    assert not any(c[0] == "restart-client" for c in fake.calls) and not slack.posts
    a._handle({"user": "UOWNER", "text": f"<@UBOT> confirm satnogs restart {p.code}", "channel": "C1", "ts": "2.0"},
              slack, key="C1:dm", thread_ts=None)
    assert ["restart-client"] in fake.calls and "Restarted satnogs-client" in slack.posts[-1]["text"]
