"""Recordings on a receiver that is removed: listed with checked move options, moved or cancelled by a person,
never crashing the runner or ending as a misleading "missed"."""
from __future__ import annotations

import datetime as dt

from aobs.db.schema import inv_events
from aobs.scheduling import reassign
from aobs.scheduling.runner import Runner
from aobs.scheduling.store import Scheduler
from test_rflayout import http, rf  # noqa: F401
from test_scheduler import NOW, FakeRunnerHw, env, rtl_job, submit  # noqa: F401


def book(env, rx="OBS-R02", start=60):
    res = submit(env, rtl_job(rx=rx, start=start, end=start + 16))
    assert res["results"][0]["ok"], res
    return res["results"][0]["job_id"]


def without(env, rid):
    """The broker restarted with `rid` taken out of observatory.toml (it is disabled, its jobs stay)."""
    del env.cfg.raw["scheduler"]["receivers"][rid]
    return Scheduler(env.cfg, env.s.engine, env.clock, present=lambda rx: rx.serial in env.present,
                     disk_free=lambda _: env.free["bytes"], satdump_pipelines=frozenset({"meteor_m2-x_lrpt", "noaa_apt"}))


def test_bookings_before_removal_offer_the_other_receivers(env):
    jid = book(env)
    p = reassign.plan(env.s, "OBS-R02")
    assert p["present"] and [j["job_id"] for j in p["jobs"]] == [jid]
    opts = {o["receiver_id"]: o for o in p["jobs"][0]["options"]}
    assert "OBS-R02" not in opts and opts["OBS-R01"]["ok"]
    assert p["jobs"][0]["suggested"] == "OBS-R01"           # same kind of hardware first
    assert env.s.get(jid)["receiver_id"] == "OBS-R02"        # checking changes nothing


def test_stranded_recordings_are_listed_and_moved_by_a_person(env):
    jid = book(env)
    s2 = without(env, "OBS-R02")
    assert reassign.stranded_counts(s2) == {"OBS-R02": 1}
    [group] = reassign.stranded(s2)
    assert group["receiver_id"] == "OBS-R02" and not group["present"]
    res = reassign.apply(s2, [{"job_id": jid, "action": "move", "receiver_id": "OBS-R01"}], "owner (web)")
    assert res["ok"], res
    job = s2.get(jid)
    assert job["receiver_id"] == "OBS-R01" and job["status"] == "scheduled"
    assert reassign.stranded(s2) == []


def test_cancel_and_bad_choices_are_reported_one_by_one(env):
    a, b = book(env, start=60), book(env, start=120)
    s2 = without(env, "OBS-R02")
    res = reassign.apply(s2, [{"job_id": a, "action": "cancel"},
                              {"job_id": b, "action": "move", "receiver_id": "OBS-R02"}], "owner (web)")
    assert not res["ok"]
    assert res["results"][0]["ok"] and s2.get(a)["status"] == "cancelled"
    assert "OBS-R02" in s2.get(a)["status_reason"]
    assert not res["results"][1]["ok"] and "no receiver OBS-R02" in res["results"][1]["error"]
    assert s2.get(b)["status"] == "scheduled"                # the failed choice changed nothing


def test_replacement_is_recognised_by_serial(env):
    serial = env.s.receivers["OBS-R01"].serial
    with env.s.engine.begin() as conn:
        conn.execute(inv_events.insert().values(receiver_id="OBS-R09", at=NOW, actor="owner (web)", action="deleted",
                                                detail={"serial": serial, "driver": "rtlsdr", "usb_path": "1-1",
                                                        "host": "local"}))
    assert reassign.replacement(env.s, "OBS-R09") == {"receiver_id": "OBS-R01", "why": f"same hardware (serial {serial})"}
    assert reassign.replacement(env.s, "OBS-R77") is None


def test_runner_fails_a_stranded_recording_with_the_real_reason(env):
    jid = book(env)
    s2 = without(env, "OBS-R02")
    env.clock.now = NOW + dt.timedelta(minutes=59, seconds=30)
    hw = FakeRunnerHw()
    Runner(s2, hw, sleep=lambda s: None).tick(threaded=False)
    job = s2.get(jid)
    assert job["status"] == "failed" and "OBS-R02 was removed or disabled" in job["status_reason"]
    assert hw.calls == []                                    # nothing was opened


def test_http_lists_bookings_and_applies_choices(http, env):
    jid = book(env)
    auth = {"Authorization": "Bearer t", "X-AOBS-RF": "1"}
    code, plan, _ = http("/v1/scheduler/receivers/OBS-R02/bookings", headers=auth)
    assert code == 200 and plan["jobs"][0]["job_id"] == jid and plan["jobs"][0]["suggested"] == "OBS-R01"
    code, view, _ = http("/v1/rf", headers=auth)
    assert code == 200 and view["stranded"] == {}             # OBS-R02 still exists
    code, res, _ = http("/v1/scheduler/reassign", "POST", {"decisions": [
        {"job_id": jid, "action": "move", "receiver_id": "OBS-R01"}]}, auth)
    assert code == 200 and res["ok"] and env.s.get(jid)["receiver_id"] == "OBS-R01"
    assert http("/v1/scheduler/reassign", "POST", {"decisions": "x"}, auth)[0] == 400


def test_slack_says_once_that_recordings_need_a_receiver(env):
    from test_slack import enable, service
    sv, fake = service(env)
    enable(env, notify_upcoming=False, notify_completed=False, notify_failed=False, notify_storage=False)
    sv._init_state()
    jid = book(env)
    sv.tick()
    env.s.receivers.pop("OBS-R02")                            # disabled: no longer a receiver the scheduler uses
    got = [p for p in sv.tick() if "was removed" in p]
    assert len(got) == 1 and "*OBS-R02*" in got[0] and "Choose a receiver" in got[0]
    assert not [p for p in sv.tick() if "was removed" in p]   # once
    reassign.apply(env.s, [{"job_id": jid, "action": "cancel"}], "owner (web)")
    assert not [p for p in sv.tick() if "was removed" in p]
