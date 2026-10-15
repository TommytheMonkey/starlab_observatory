"""SDR++ listening sessions, with simulated receivers and a mock SDR++ (no radio is ever opened)."""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import shutil
import socket
import sys
import time
from typing import Any

import pytest

from aobs.inventory.discovery import UsbDevice
from aobs.scheduling.store import SchedulerError
from aobs.sessions import procs, sdrpp
from aobs.sessions.backends import LocalSdrppBackend, SimSdrppBackend
from aobs.sessions.manager import SessionManager
from test_firstrun import sim  # noqa: F401
from test_inventory import fake_discover  # noqa: F401
from test_scheduler import FakeRunnerHw, env, fix_dirs, rtl_job  # noqa: F401
from test_testlab import lab  # noqa: F401

BWRAP = shutil.which("bwrap")
PORTS = [25259, 25299]


def wait_for(cond, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


@pytest.fixture
def mgr(env, lab, sim, tmp_path):
    env.cfg.raw["sessions"] = {"port_range": PORTS}
    sim.add({"usb_path": "sim-1", "id": "SIM-A"}, "o")
    m = SessionManager(env.s, {"local": LocalSdrppBackend(sdrpp.Paths(sdrpp="/nonexistent/sdrpp")),
                               "sim": SimSdrppBackend()}, hw=FakeRunnerHw(),
                       interfaces=lambda: {"lo": ["127.0.0.1"], "tailscale0": ["127.0.0.2"]})
    env.s.sessions = m
    env.clock.now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)   # real processes, real time
    yield m
    for r in m.rows(active=True):
        m.stop(r["session_id"], "test cleanup")


def test_eligibility_explains_every_refusal(env, mgr):
    assert mgr.eligibility("SIM-A")["eligible"]
    r01 = mgr.eligibility("OBS-R01")
    assert not r01["eligible"] and any("independent service" in x for x in r01["reasons"])
    assert any("antenna switch" in x for x in mgr.eligibility("OBS-R05")["reasons"])
    rsp = mgr.eligibility("RSP1B")["reasons"]
    assert any("SDRplay API service" in x for x in rsp)
    r02 = mgr.eligibility("OBS-R02")["reasons"]
    assert any("SDR++ is not installed" in x for x in r02)
    binds = [b["address"] for b in mgr.eligibility("SIM-A")["binds"]]
    assert binds == ["127.0.0.1", "127.0.0.2"]                   # loopback and Tailscale only, never 0.0.0.0


def test_session_lifecycle_owns_the_receiver_and_stops_only_its_own_process(env, mgr):
    lookalike = mgr.backends["sim"].launch("s-lookalike", env.s.receivers["SIM-A"], "127.0.0.1", PORTS[1],
                                           env.tmp / "lookalike", 120)["_proc"]   # same program, another session
    try:
        s = mgr.start("SIM-A", "owner (web)", minutes=30)
        assert s["status"] == "running" and s["simulated"] and s["connect_to"] == f"127.0.0.1:{s['port']}"
        assert os.getpgid(s["pid"]) == s["pgid"] == s["pid"] and procs.session_of(s["pid"]) == s["session_id"]
        with pytest.raises(SchedulerError, match="already in session"):
            mgr.start("SIM-A", "someone else")
        conn: list[socket.socket] = []

        def connect() -> bool:
            try:
                conn.append(socket.create_connection(("127.0.0.1", s["port"]), timeout=1))
                return True
            except OSError:
                return False
        wait_for(connect)                                            # the server needs a moment to listen
        c = conn[0]
        wait_for(lambda: (mgr.tick() or True) and mgr.get(s["session_id"])["clients"] == 1)
        row = mgr.get(s["session_id"])
        assert row["last_client_at"] is not None and row["detail"]["served"]["simulated"]
        c.close()
        out = mgr.stop(s["session_id"], "owner (web)")
        assert out["status"] == "ended" and "server stopped" in out["end_reason"]
        assert procs.session_of(s["pid"]) is None and lookalike.poll() is None
        assert mgr.eligibility("SIM-A")["eligible"]                  # free again
    finally:
        lookalike.kill()
        lookalike.wait()


def test_a_session_never_overlaps_a_booking_silently(env, mgr):
    job = fix_dirs(rtl_job("pass", rx="SIM-A"), env.tmp)
    job["receiver"].update(driver="sim", serial="SIM-0001")
    job["radio"].update(gain_db=None, gain_elements={"GAIN": 25}, sample_rate_hz=250_000,
                        center_frequency_hz=137_900_000 - 31_250)
    job["recording"].update(engine="simulator", format="cs16")
    job["satdump"] = {"enabled": False, "operation": "none"}
    job["postprocess"]["decode"] = False
    start = env.clock.now + dt.timedelta(minutes=40)
    job["schedule"].update(start=start.isoformat(), end=(start + dt.timedelta(minutes=10)).isoformat())
    res = env.s.submit([job])["results"][0]
    assert res["ok"], res["errors"]
    with pytest.raises(SchedulerError, match="booked on SIM-A before this session would end"):
        mgr.start("SIM-A", "o", minutes=60, end_before_passes=False)
    s = mgr.start("SIM-A", "o", minutes=60, end_before_passes=True)
    resv = dt.datetime.fromisoformat(env.s.get(res["job_id"])["reservation_start"])
    assert dt.datetime.fromisoformat(s["ends_at"]) == resv - dt.timedelta(seconds=120)
    assert "before" in s["detail"]["note"]
    mgr.stop(s["session_id"], "o")
    env.clock.now = resv - dt.timedelta(minutes=4)
    with pytest.raises(SchedulerError, match="less than 5 minutes are free"):
        mgr.start("SIM-A", "o", minutes=30)


def test_bookings_during_a_session_respect_its_policy(env, mgr):
    def booking(minutes_ahead: int) -> dict[str, Any]:
        job = fix_dirs(rtl_job(f"b{minutes_ahead}", rx="SIM-A"), env.tmp)
        job["receiver"].update(driver="sim", serial="SIM-0001")
        job["radio"].update(gain_db=None, gain_elements={"GAIN": 25}, sample_rate_hz=250_000,
                            center_frequency_hz=137_900_000 - 31_250)
        job["recording"].update(engine="simulator", format="cs16")
        job["satdump"] = {"enabled": False, "operation": "none"}
        job["postprocess"]["decode"] = False
        st = env.clock.now + dt.timedelta(minutes=minutes_ahead)
        job["schedule"].update(start=st.isoformat(), end=(st + dt.timedelta(minutes=5)).isoformat())
        return env.s.submit([job])["results"][0]
    keep = mgr.start("SIM-A", "o", minutes=120, end_before_passes=False)
    res = booking(30)
    assert not res["ok"] and any("set not to end for bookings" in e for e in res["errors"])
    mgr.stop(keep["session_id"], "o")
    yielding = mgr.start("SIM-A", "o", minutes=120, end_before_passes=True, warn_minutes=10)
    res = booking(30)
    assert res["ok"] and any("will be ended at" in w for w in res["warnings"])
    mgr.tick()
    row = mgr.get(yielding["session_id"])
    resv = dt.datetime.fromisoformat(env.s.get(res["job_id"])["reservation_start"])
    assert row["ends_at"] == resv - dt.timedelta(seconds=120) and row["status"] == "running"
    env.clock.now = row["ends_at"] - dt.timedelta(minutes=9)                 # inside the warning window
    mgr.tick()
    assert mgr.get(yielding["session_id"])["warned_at"] is not None
    assert any(n["kind"] == "session" and "ends at" in n["text"] for n in env.s.notifications(0))
    env.clock.now = row["ends_at"] + dt.timedelta(seconds=1)
    mgr.tick()
    ended = mgr.get(yielding["session_id"])
    assert ended["status"] == "ended" and "ended for a booking" in ended["end_reason"]


def test_a_crashed_server_frees_the_receiver(env, mgr, monkeypatch):
    monkeypatch.setenv("MOCK_SDRPP_EXIT_AFTER", "0.5")
    s = mgr.start("SIM-A", "o", minutes=30)
    wait_for(lambda: (mgr.tick() or True) and mgr.get(s["session_id"])["status"] == "failed")
    assert "exited by itself" in mgr.get(s["session_id"])["end_reason"]
    monkeypatch.delenv("MOCK_SDRPP_EXIT_AFTER")
    assert mgr.eligibility("SIM-A")["eligible"]


def test_idle_sessions_end_and_stale_ones_are_closed_after_a_restart(env, mgr):
    s = mgr.start("SIM-A", "o", minutes=120)
    env.clock.now += dt.timedelta(minutes=31)
    mgr.tick()
    assert "no client connected for 30 minutes" in mgr.get(s["session_id"])["end_reason"]
    s2 = mgr.start("SIM-A", "o", minutes=60)
    procs.stop_group(s2["pid"], s2["pgid"], s2["session_id"])                # the server dies while the broker is down
    mgr._procs.pop(s2["session_id"]).wait()
    SessionManager(env.s, mgr.backends).recover()
    row = mgr.get(s2["session_id"])
    assert row["status"] == "failed" and row["end_reason"].startswith("stale")


def test_a_recording_start_ends_a_yielding_session(env, mgr):
    from aobs.scheduling.runner import Runner
    s = mgr.start("SIM-A", "o", minutes=60)
    job = {"job_id": "x", "job_name": "meteor", "reservation_end": (env.clock.now + dt.timedelta(minutes=5)).isoformat()}
    Runner(env.s, FakeRunnerHw(), sleep=lambda x: None)._acquire(job, env.s.receivers["SIM-A"], {})
    assert mgr.get(s["session_id"])["end_reason"].startswith("recording meteor is starting")


def test_addresses_outside_the_allowed_list_are_refused(env, mgr):
    with pytest.raises(SchedulerError, match="not an allowed address"):
        mgr.start("SIM-A", "o", bind="0.0.0.0")
    s = mgr.start("SIM-A", "o", bind="127.0.0.2")
    assert s["bind_addr"] == "127.0.0.2"


@pytest.mark.skipif(not BWRAP, reason="bubblewrap not installed")
def test_sandbox_lets_sdrpp_see_and_open_only_the_selected_receiver(env, mgr, tmp_path):
    """The real bubblewrap sandbox around the mock server: one USB node, one sysfs entry."""
    sysfs = tmp_path / "sysfs"
    (sysfs / "5-4").mkdir(parents=True)
    node_stand_in = tmp_path / "node"
    node_stand_in.write_text("")
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "rtl_sdr_source.so").write_text("")
    dev = UsbDevice("5-4", "0bda", "2838", "Realtek", "RTL2838UHIDIR", "OBS-R02", "0100", 5, 4)
    backend = LocalSdrppBackend(
        sdrpp.Paths(sdrpp=sys.executable, plugins=str(plugins), bwrap=BWRAP), sysfs=sysfs,
        find=lambda serial, sysfs=None, usb_path=None: dev if serial == "OBS-R02" else None,
        program=lambda bind, port, root: [sys.executable, "-m", "aobs.sessions.mock_sdrpp", "--server", "--addr", bind,
                                          "--port", str(port), "--root", str(root), "--autostart"],
        node_source=lambda d: str(node_stand_in))
    mgr.backends["local"] = backend
    assert backend.problems(env.s.receivers["OBS-R02"]) == []
    s = mgr.start("OBS-R02", "o", minutes=30)
    report = pathlib.Path(s["detail"]["sandbox"]["device_node"] and mgr.workroot / s["session_id"] / "sdrpp-root"
                          / "mock-report.json")
    wait_for(report.exists)
    r = json.loads(report.read_text())
    assert r["usb_nodes"] == ["/dev/bus/usb/005/004"] and r["sysfs_usb"] == ["5-4"]
    assert r["session"] == s["session_id"] and r["argv"][r["argv"].index("--addr") + 1] == "127.0.0.1"
    assert s["detail"]["sandbox"]["visible_devices"] == ["5-4"]
    cfg = json.loads((mgr.workroot / s["session_id"] / "sdrpp-root" / "config.json").read_text())
    assert list(cfg["moduleInstances"]) == ["RTL-SDR Source"] and cfg["source"] == "RTL-SDR Source"
    mgr.stop(s["session_id"], "o")


def test_a_session_ends_even_without_the_broker(env, mgr):
    """The server's own hard lifetime (coreutils timeout) ends it if nothing else does."""
    from aobs.sessions.backends import SimSdrppBackend
    launched = SimSdrppBackend().launch("s-orphan", env.s.receivers["SIM-A"], "127.0.0.1", PORTS[1] - 1,
                                        env.tmp / "orphan", 1)
    assert launched["command"][:3] == ["timeout", "--kill-after=10", "1"]
    launched["_proc"].wait(timeout=15)
    assert procs.session_of(launched["pid"]) is None


def test_install_checks():
    p = sdrpp.Paths(sdrpp="/nonexistent/sdrpp", bwrap="/nonexistent/bwrap", plugins="/nonexistent")
    probs = sdrpp.install_problems("rtlsdr", p)
    assert any("SDR++ is not installed" in x for x in probs) and any("bubblewrap" in x for x in probs)
    assert any("rtl_sdr_source module is missing" in x for x in probs)
    assert "SDRplay API service" in sdrpp.install_problems("sdrplay", p)[0]
    cmd = sdrpp.sandbox_command(sdrpp.Paths(), pathlib.Path("/r"), "/dev/bus/usb/005/004", "/sys/devices/x/5-4", "5-4",
                                "127.0.0.1", 5259)
    assert cmd[cmd.index("--addr") + 1] == "127.0.0.1" and "--unshare-ipc" in cmd and "--die-with-parent" in cmd
    assert cmd[cmd.index("--dev-bind") + 1:cmd.index("--dev-bind") + 3] == ["/dev/bus/usb/005/004"] * 2
