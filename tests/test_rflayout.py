from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

from aobs.scheduling import rflayout
from aobs.scheduling.store import SchedulerError
from test_scheduler import NOW, FakeRunnerHw, drain, env, planner, rtl_job, run_job, submit, window  # noqa: F401


@pytest.fixture
def rf(env):
    ports: list[dict[int, Any]] = []
    env.s.rf = rflayout.RfLayout(env.s, on_ports=ports.append)
    env.s.rf.load()
    env.s.rf.port_calls = ports  # type: ignore[attr-defined]
    return env.s.rf


def owner_layout(rf: rflayout.RfLayout) -> dict[str, Any]:
    """The layout the owner described on 2026-10-14, built on top of the seed."""
    lay = copy.deepcopy(rf.current["layout"])
    c = lay["components"]

    def part(cid: str, name: str, kind: str, **kw: Any) -> None:
        c[cid] = {"id": cid, "name": name, "kind": kind, "status": "working", "power": "none", "model": "", "notes": "",
                  "gain_db": None, "min_mhz": None, "max_mhz": None, **kw}
    part("V-DIPOLE-COPPER", "Copper V-dipole", "antenna")
    part("SAWBIRD-NOAA", "Sawbird NOAA", "amplifier", power="external", gain_db=30)
    part("LANA", "Nooelec LaNA", "amplifier", power="external")
    part("AM-BANDSTOP", "AM bandstop", "filter")
    part("FM-BANDSTOP-1", "FM bandstop", "filter")
    part("FM-BANDSTOP-2", "FM bandstop", "filter")
    part("TELESCOPING-V", "Telescoping V-dipole", "antenna")
    part("DISCONE", "Discone", "antenna")
    r = lay["receivers"]
    r["OBS-R01"]["chain"] = ["V-DIPOLE-COPPER", "SAWBIRD-NOAA"]
    r["OBS-R02"]["chain"] = ["DISCONE", "LANA"]
    r["RSP1B"]["chain"] = ["QFH-137", "AM-BANDSTOP", "FM-BANDSTOP-1"]
    r["OBS-R05"]["chain"] = ["ANTSW-01", "FM-BANDSTOP-2"]
    r["OBS-R03"] = {"id": "OBS-R03", "chain": ["TELESCOPING-V"], "label": "OBS-R03 (SatNOGS comet)",
                    "host": "comet", "serial": "OBS-R03", "status": "working"}
    return lay


def test_seed_comes_from_the_config(rf, env):
    lay = rf.current["layout"]
    assert rf.current["version"] == 1 and rf.current["author"].startswith("seed")
    assert lay["receivers"]["OBS-R01"]["chain"] == ["QFH-137"]
    assert lay["receivers"]["OBS-R05"]["chain"] == ["ANTSW-01"]
    assert lay["components"]["ANTSW-01"]["ports"]["1"] == "V-DIPOLE" and lay["components"]["ANTSW-01"]["ports"]["8"] == "MA-30-LOOP"
    assert lay["receivers"]["RSP1B"]["chain"] == []
    assert env.s.receivers["OBS-R05"].switched and env.s.receivers["RSP1B"].antenna == "UNSPECIFIED"
    rf.current = None
    assert rf.load()["version"] == 1                     # a restart reuses the stored version, no reseed


def test_saving_updates_the_broker_receivers(rf, env):
    res = rf.save(owner_layout(rf), 1, "owner (web)", "moved the dipole to R01")
    assert res["version"] == 2 and any("OBS-R01: Copper V-dipole > Sawbird NOAA" in c for c in res["changes"])
    r01 = env.s.receivers["OBS-R01"]
    assert r01.antenna == "V-DIPOLE-COPPER" and r01.signal_path == "Copper V-dipole > Sawbird NOAA > OBS-R01"
    assert r01.amplifier == "Sawbird NOAA" and r01.amplifier_power == "external"
    rsp = env.s.receivers["RSP1B"]
    assert rsp.antenna == "QFH-137" and rsp.filters == ("AM bandstop", "FM bandstop")
    r05 = env.s.receivers["OBS-R05"]
    assert r05.switched and r05.signal_path == "ANTSW-01 port > FM bandstop > OBS-R05"
    assert "OBS-R03" not in env.s.receivers                # external receivers are documented, not scheduled
    hist = rf.history()
    assert [h["version"] for h in hist] == [2, 1] and hist[0]["note"] == "moved the dipole to R01"
    assert rf.save(owner_layout(rf), 2, "owner (web)")["unchanged"]
    with pytest.raises(SchedulerError, match="changed since you loaded it"):
        lay = owner_layout(rf)
        lay["components"]["LANA"]["status"] = "suspect"
        rf.save(lay, 1, "owner (web)")


@pytest.mark.parametrize("mutate, message", [
    (lambda l: l["receivers"]["OBS-R02"]["chain"].append("AM-BANDSTOP"), "in two places"),
    (lambda l: l["receivers"]["OBS-R02"].update(chain=["LANA", "DISCONE"]), "must be first"),
    (lambda l: l["receivers"]["OBS-R05"].update(chain=["FM-BANDSTOP-2"]), "keep ANTSW-01 at the start"),
    (lambda l: (l["receivers"]["OBS-R05"].update(chain=["FM-BANDSTOP-2"]),
                l["receivers"]["OBS-R02"].update(chain=["ANTSW-01"])), "cannot feed OBS-R02"),
    (lambda l: l["receivers"].pop("OBS-R02"), "cannot be removed"),
    (lambda l: l["components"]["ANTSW-01"]["ports"].update({"2": "LANA"}), "only antennas go on switch ports"),
    (lambda l: l["components"]["ANTSW-01"]["ports"].update({"2": "DISCONE"}), "in two places"),
    (lambda l: l["receivers"]["OBS-R01"]["chain"].append("NOPE"), "unknown part"),
])
def test_invalid_layouts_are_refused_with_a_reason(rf, mutate, message):
    lay = owner_layout(rf)
    mutate(lay)
    dry = rf.save(lay, 1, "owner (web)", dry_run=True)
    assert not dry["ok"] and any(message in e for e in dry["errors"]), dry["errors"]
    with pytest.raises(SchedulerError, match=message):
        rf.save(lay, 1, "owner (web)")
    assert rf.current["version"] == 1


def test_warnings_for_broken_parts_and_bias_tee_power(rf, env):
    lay = owner_layout(rf)
    lay["components"]["LANA"]["status"] = "broken"
    lay["components"]["NEEDS-DC"] = {"id": "NEEDS-DC", "name": "Mast LNA", "kind": "amplifier", "power": "bias_tee"}
    lay["receivers"]["OBS-R01"]["chain"].append("NEEDS-DC")
    lay["receivers"]["RSP1B"]["chain"] = []
    w = rf.save(lay, 1, "owner (web)", dry_run=True)["warnings"]
    assert "OBS-R02: Nooelec LaNA is marked broken" in w
    assert any("OBS-R01 cannot supply bias-tee power" in x for x in w)
    assert "RSP1B has no antenna in its path" in w


def test_switch_ports_follow_the_layout_and_r05_jobs_follow_their_antenna(rf, env):
    jid = submit(env, rtl_job("r05-dipole", rx="OBS-R05", receiver__antenna="V-DIPOLE"))["results"][0]["job_id"]
    assert env.s.get(jid)["spec"]["resolved"]["switch_port"] == 1
    lay = owner_layout(rf)
    lay["components"]["ANTSW-01"]["ports"].update({"1": None, "3": "V-DIPOLE"})
    res = rf.save(lay, 1, "owner (web)")
    assert sorted(env.s.switch_ports) == [3, 8] and env.s.switch_ports[3]["antenna"] == "V-DIPOLE"
    assert rf.port_calls and sorted(rf.port_calls[-1]) == [3, 8]
    job = env.s.get(jid)
    assert job["spec"]["resolved"]["switch_port"] == 3
    assert job["spec"]["receiver"]["signal_path"] == "V-DIPOLE > ANTSW-01 antenna switch port 3 > FM bandstop > OBS-R05"
    assert any("moved from port 1 to 3" in j for j in res["jobs"])
    lay2 = copy.deepcopy(rf.current["layout"])
    lay2["components"]["ANTSW-01"]["ports"]["3"] = None
    res = rf.save(lay2, 2, "owner (web)")
    assert any("no longer on ANTSW-01" in j for j in res["jobs"])
    note = env.s.notifications(0)[-1]
    assert note["kind"] == "rf_layout" and "no longer on ANTSW-01" in note["text"]


def test_future_rows_show_the_new_path_for_the_calendar(rf, env):
    planner(env, [window(60, 75)]).plan()
    legacy = env.s.jobs(receiver="OBS-R01", source="legacy")[0]
    assert legacy["spec"]["receiver"]["antenna"] == "QFH-137"
    rf.save(owner_layout(rf), 1, "owner (web)")
    spec = env.s.get(legacy["job_id"])["spec"]["receiver"]
    assert spec["antenna"] == "V-DIPOLE-COPPER" and spec["signal_path"] == "Copper V-dipole > Sawbird NOAA > OBS-R01"


def test_a_recording_keeps_the_path_it_was_made_with(rf, env):
    rf.save(owner_layout(rf), 1, "owner (web)")
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    snap = env.s.get(jid)["spec"]["resolved"]["rf_path"]
    assert snap["layout_version"] == 2 and snap["text"] == "Discone > Nooelec LaNA > OBS-R02"
    assert [c["id"] for c in snap["chain"]] == ["DISCONE", "LANA"] and snap["chain"][1]["power"] == "external"
    lay = copy.deepcopy(rf.current["layout"])
    lay["receivers"]["OBS-R02"]["chain"] = ["DISCONE"]
    rf.save(lay, 2, "owner (web)", "LaNA removed")
    assert rf.stamp(env.s.get(jid))["layout_version"] == 2                # never overwritten afterwards
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    side = json.loads(pathlib.Path(env.s.get(jid)["paths"]["sidecar"]).read_text())
    assert side["rf_path"]["text"] == "Discone > Nooelec LaNA > OBS-R02"
    from aobs.scheduling.catalog import Catalog
    assert Catalog(env.s, runner.proc).describe(env.s.get(jid))["signal_path"] == "Discone > Nooelec LaNA > OBS-R02"


def test_legacy_files_get_the_layout_in_effect_when_they_started(rf, env):
    v1_time = rf.current["created_at"]
    env.clock.now = NOW + dt.timedelta(hours=2)
    rf.save(owner_layout(rf), 1, "owner (web)")
    job = {"job_id": "x", "receiver_id": "OBS-R01", "record_start": (v1_time + dt.timedelta(minutes=30)).isoformat()}
    assert rf.snapshot("OBS-R01", dt.datetime.fromisoformat(job["record_start"]))["text"] == "Diamond DP-KE137 QFH > OBS-R01"
    assert rf.snapshot("OBS-R01", v1_time + dt.timedelta(hours=3))["text"] == "Copper V-dipole > Sawbird NOAA > OBS-R01"
    assert rf.snapshot("OBS-R01", v1_time - dt.timedelta(days=1)) is None    # before any layout: nothing invented


# ---------------------------------------------------------------- HTTP: page, sign-in, cookie + CSRF
@pytest.fixture
def http(env, rf):
    import uvicorn

    from aobs.coordination.api import create_app
    from aobs.coordination.broker import Broker
    from aobs.scheduling.runner import Runner
    from aobs.scheduling.service import SchedulerService
    from test_broker import FakeHw

    env.cfg.raw["broker"] = {"receivers": ["OBS-R05"]}
    svc = SchedulerService(env.s, planner(env, []), Runner(env.s, FakeRunnerHw()))
    svc.start = lambda: None
    app = create_app(Broker(env.cfg, FakeHw(), env.clock), "t", scheduler=svc)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)

    def call(path: str, method: str = "GET", body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any, Any]:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=headers or {},
                                     data=json.dumps(body).encode() if body is not None else None)
        if body is not None:
            req.add_header("Content-Type", "application/json")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a: Any, **k: Any) -> None:
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if resp.headers.get("content-type", "").startswith("application/json") else raw.decode()), resp.headers
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b"{}"
            try:
                return exc.code, json.loads(raw), exc.headers
            except ValueError:
                return exc.code, raw.decode(), exc.headers
    try:
        yield call
    finally:
        server.should_exit = True


def test_editor_page_sign_in_and_csrf(http, env):
    code, page, _ = http("/rf")
    assert code == 200 and "<title>Signal Paths</title>" in page and "Parts bin" in page
    assert http("/v1/rf")[0] == 401
    code, view, _ = http("/v1/rf", headers={"Authorization": "Bearer t"})
    assert code == 200 and view["version"] == 1 and "OBS-R05" in view["status"] and view["viewer"] == "api"
    code, link, _ = http("/v1/rf/link", headers={"Authorization": "Bearer t"})
    path = re.sub(r"^https?://[^/]+", "", link["url"])
    bad = re.sub(r"/[0-9a-f]+$", "/" + "0" * 40, path)
    assert http(bad)[0] == 403
    code, _, headers = http(path)
    assert code == 303 and headers["location"] == "/rf"
    cookie = headers["set-cookie"].split(";")[0]
    assert "HttpOnly" in headers["set-cookie"] and "SameSite=strict" in headers["set-cookie"]
    code, view, _ = http("/v1/rf", headers={"Cookie": cookie})
    assert code == 200 and view["viewer"] == "owner (web)"
    lay = view["layout"]
    lay["receivers"]["RSP1B"]["notes"] = "QFH on the roof"
    body = {"layout": lay, "base_version": 1, "note": "test"}
    assert http("/v1/rf", "POST", body, {"Cookie": cookie})[0] == 403          # no X-AOBS-RF header: refused
    code, res, _ = http("/v1/rf", "POST", body, {"Cookie": cookie, "X-AOBS-RF": "1"})
    assert code == 200 and res["version"] == 2
    assert env.s.rf.current["author"] == "owner (web)"
    code, hist, _ = http("/v1/rf/history", headers={"Cookie": cookie})
    assert [v["version"] for v in hist["versions"]] == [2, 1]
    assert http("/v1/rf", headers={"Cookie": "aobs_rf=99999999999.deadbeef"})[0] == 401


# ---------------------------------------------------------------- pushing names to the ANTSW-01 board
class FakeBoard:
    """The board's /api/ports with deliberately non-trivial hardware codes."""
    def __init__(self, reachable: bool = True):
        self.reachable, self.puts = reachable, []
        self.state = {"switch_id": "ANTSW-01", "receiver": "OBS-R05", "ports": [
            {"port": n, "code": c, "antenna": a, "label": l} for n, c, a, l in
            [(1, 0, "V-DIPOLE", "Tunable"), (2, 1, None, "Empty"), (3, 2, None, "Empty"), (4, 3, None, "Empty"),
             (5, 4, None, "Empty"), (6, 5, "ADS", "Airband"), (7, 6, "TELE-WHIP", "whip"), (8, 7, "MA-30-LOOP", "HF")]]}

    def get(self, base_url, timeout=5.0):
        if not self.reachable:
            raise OSError("timed out")
        return copy.deepcopy(self.state)

    def put(self, base_url, token, ports, switch_id, receiver, timeout=5.0):
        self.puts.append({"ports": ports, "switch_id": switch_id, "receiver": receiver})
        self.state["ports"] = copy.deepcopy(ports)
        return {"ok": True}


@pytest.fixture
def board(env, monkeypatch):
    from aobs.hardware import antsw
    b = FakeBoard()
    monkeypatch.setattr(antsw, "get_ports", b.get)
    monkeypatch.setattr(antsw, "put_ports", b.put)
    env.s.rf = rflayout.RfLayout(env.s, board_read=lambda: antsw.get_ports("x"),
                                 board_push=lambda ports: antsw.push_names("x", "tok", ports))
    env.s.rf.load()
    return b


def test_board_status_shows_where_the_board_differs(board, env):
    st = env.s.rf.board_status()
    assert st["reachable"] and not st["in_sync"]
    assert {(d["port"], d["board"], d["layout"]) for d in st["differences"]} == {(6, "ADS", None), (7, "TELE-WHIP", None)}


def test_saving_port_changes_pushes_names_and_keeps_hardware_codes(board, env):
    lay = copy.deepcopy(env.s.rf.current["layout"])
    lay["components"]["TELE-WHIP"] = {"id": "TELE-WHIP", "name": "Telescoping whip", "kind": "antenna",
                                      "notes": "All-purpose 25 MHz-1.7 GHz, best 88-500 MHz plus a long tail of extra words"}
    lay["components"]["ANTSW-01"]["ports"].update({"7": "TELE-WHIP", "3": "MA-30-LOOP", "8": None})
    res = env.s.rf.save(lay, 1, "owner (web)", "whip on 7, loop on 3")
    sb = res["switch_board"]
    assert sb["verified"] and sb["codes_unchanged"] and "ANTSW-01 board updated" in sb["text"]
    sent = {p["port"]: p for p in board.puts[-1]["ports"]}
    assert [sent[n]["code"] for n in range(1, 9)] == [0, 1, 2, 3, 4, 5, 6, 7]            # copied from the board
    assert sent[7]["antenna"] == "TELE-WHIP" and len(sent[7]["label"]) <= 48
    assert sent[3]["antenna"] == "MA-30-LOOP" and sent[8] == {"port": 8, "code": 7, "antenna": None, "label": "Empty"}
    assert board.puts[-1]["switch_id"] == "ANTSW-01" and board.puts[-1]["receiver"] == "OBS-R05"
    assert env.s.rf.board_status()["in_sync"]
    assert "ANTSW-01 board updated" in env.s.notifications(0)[-1]["text"]
    n = len(board.puts)
    lay = copy.deepcopy(env.s.rf.current["layout"])
    lay["receivers"]["OBS-R02"]["notes"] = "unrelated edit"
    assert env.s.rf.save(lay, 2, "owner (web)")["switch_board"] is None and len(board.puts) == n   # no port change, no push


def test_unreachable_board_does_not_lose_the_save(board, env):
    board.reachable = False
    lay = copy.deepcopy(env.s.rf.current["layout"])
    lay["components"]["ANTSW-01"]["ports"]["8"] = None
    res = env.s.rf.save(lay, 1, "owner (web)")
    assert res["version"] == 2 and not res["switch_board"]["pushed"] and "NOT updated" in res["switch_board"]["text"]
    assert env.s.rf.board_status() == {"available": True, "reachable": False, "error": "timed out"}
    board.reachable = True
    assert env.s.rf.push_board("owner (web)")["verified"]                      # the editor's retry button


def test_switch_port_antenna_ids_fit_the_board(board, env):
    lay = copy.deepcopy(env.s.rf.current["layout"])
    lay["components"]["A-VERY-LONG-ANTENNA-ID"] = {"id": "A-VERY-LONG-ANTENNA-ID", "name": "Long", "kind": "antenna"}
    lay["components"]["ANTSW-01"]["ports"]["2"] = "A-VERY-LONG-ANTENNA-ID"
    with pytest.raises(SchedulerError, match="at most 16"):
        env.s.rf.save(lay, 1, "owner (web)")
    assert board.puts == []


def test_renaming_a_switch_antenna_updates_ports_jobs_and_board(board, env):
    jid = submit(env, rtl_job("r05-dipole", rx="OBS-R05", receiver__antenna="V-DIPOLE"))["results"][0]["job_id"]
    res = env.s.rf.rename("V-DIPOLE", "TELE-WHIP", 1, "owner (web)")
    lay = env.s.rf.current["layout"]
    assert "V-DIPOLE" not in lay["components"] and lay["components"]["TELE-WHIP"]["former_ids"] == ["V-DIPOLE"]
    assert lay["components"]["ANTSW-01"]["ports"]["1"] == "TELE-WHIP" and env.s.switch_ports[1]["antenna"] == "TELE-WHIP"
    job = env.s.get(jid)
    assert job["spec"]["receiver"]["antenna"] == "TELE-WHIP" and job["spec"]["resolved"]["switch_port"] == 1
    assert res["renamed"]["jobs_updated"] and res["switch_board"]["verified"]
    sent = {p["port"]: p for p in board.puts[-1]["ports"]}
    assert sent[1]["antenna"] == "TELE-WHIP" and sent[1]["code"] == 0
    with pytest.raises(SchedulerError, match="at most 16"):
        env.s.rf.rename("TELE-WHIP", "A-MUCH-TOO-LONG-NAME", None, "owner (web)")
    with pytest.raises(SchedulerError, match="already used"):
        env.s.rf.rename("TELE-WHIP", "MA-30-LOOP", None, "owner (web)")
    with pytest.raises(SchedulerError, match="fixed"):
        env.s.rf.rename("ANTSW-01", "SWITCH", None, "owner (web)")
    with pytest.raises(SchedulerError, match="changed since"):
        env.s.rf.rename("TELE-WHIP", "WHIP", 1, "owner (web)")
    assert env.s.rf.aliases("V-DIPOLE") == {"TELE-WHIP", "V-DIPOLE"}
