"""Brief 6: automation rules, hybrid overrides, reconciliation, job states, element sources, output validation."""

from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import types
from typing import Any

import pytest
from sqlalchemy import select, text

from aobs.db import init_db, make_engine
from aobs.db.schema import sched_jobs
from aobs.satellites import predict
from aobs.scheduling import schedview
from aobs.scheduling.rules import RulePlanner
from aobs.scheduling.runner import validate_output
from aobs.scheduling.store import Scheduler, SchedulerError, lifecycle, statuses_for
from test_scheduler import NOW, Clock, sched_config

UTC = dt.timezone.utc
M23, M24, ISS = 57166, 59051, 25544


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def env(tmp_config, tmp_path):
    cfg = sched_config(tmp_config, tmp_path)
    cfg.raw["scheduler"]["receivers"]["OBS-R02"].pop("auto")          # R02 is free for rules; R01 stays legacy
    engine = make_engine(cfg.database_url)
    init_db(engine)
    clock = Clock()
    present = {"OBS-R01", "OBS-R02", "OBS-R05", "240526D360"}
    s = Scheduler(cfg, engine, clock, disk_free=lambda _: 500 * 10**9, present=lambda rx: rx.serial in present,
                  satdump_pipelines=frozenset({"meteor_m2-x_lrpt", "noaa_apt"}))
    save(s, "observatory", latitude=45.0, longitude=10.0, elevation_m=100)     # not the station's location
    save(s, "scheduling", mode="hybrid")
    return types.SimpleNamespace(s=s, clock=clock, present=present, cfg=cfg, tmp=tmp_path, engine=engine,
                                 planner=RulePlanner(s))


def save(s: Any, doc: str, **values: Any) -> None:
    cur = s.config_store.current(doc)
    s.config_store.save(doc, {**((cur or {}).get("data") or {}), **values}, None, "test")


class Passes:
    """Predictions under the test's control: [(norad, aos minutes from NOW, minutes long, max elevation)]."""

    def __init__(self, monkeypatch: Any, passes: list[tuple[int, float, float, float]]):
        self.passes = list(passes)
        self.shift_s = 0.0
        monkeypatch.setattr(predict, "predict_passes", self)

    def __call__(self, elements, norads, lat, lon, elev, start, hours, min_el=0.0):
        out = []
        for n, at, dur, el in self.passes:
            if n not in norads or el < min_el:
                continue
            aos = NOW + dt.timedelta(minutes=at, seconds=self.shift_s)
            los = aos + dt.timedelta(minutes=dur)
            if aos < start or los > start + dt.timedelta(hours=hours):
                continue
            out.append({"norad": n, "name": predict.CATALOG[n]["name"], "aos": aos.isoformat(), "los": los.isoformat(),
                        "max_elevation_deg": el, "max_at": (aos + (los - aos) / 2).isoformat(),
                        "aos_azimuth_deg": 10.0, "los_azimuth_deg": 190.0, "duration_s": int(dur * 60),
                        "elements_epoch": "2026-10-14T14:44:33+00:00", "elements_source": "test"})
        return sorted(out, key=lambda p: p["aos"])


def rule(**over: Any) -> dict[str, Any]:
    r = {"name": "METEOR on R02", "enabled": True, "satellites": [M23, M24], "min_max_elevation_deg": 20,
         "receiver_id": "OBS-R02", "backup_receivers": [], "frequency_hz": 137_900_000, "sample_rate_hz": 1_024_000,
         "pre_aos_s": 60, "post_los_s": 30, "decode": False, "priority": 50}
    r.update(over)
    return r


def rule_jobs(env: Any, **filters: Any) -> list[dict[str, Any]]:
    with env.engine.connect() as conn:
        rows = conn.execute(select(sched_jobs).where(sched_jobs.c.rule_id.is_not(None))
                            .order_by(sched_jobs.c.record_start)).mappings().all()
    out = [dict(r) for r in rows]
    for k, v in filters.items():
        out = [r for r in out if r[k] == v]
    return out


# ------------------------------------------------------------------ rules: validation and storage
def test_rule_validation_reports_every_problem(env):
    res = env.s.rules.check({"name": "", "satellites": [], "receiver_id": "NOPE", "bogus": 1})
    errs = " | ".join(res["errors"])
    assert not res["ok"]
    for want in ("unknown field 'bogus'", "name is required", "at least one satellite", "'NOPE' does not exist"):
        assert want in errs
    res = env.s.rules.check(rule(satellites=[ISS], decode=True, frequency_hz=None))
    assert any("no decoder for NORAD 25544" in e for e in res["errors"])
    res = env.s.rules.check(rule(raw_iq_policy="after_decode"))
    assert any("only be deleted after decoding" in e for e in res["errors"])
    res = env.s.rules.check(rule(output_root="/etc/x", backup_receivers=["OBS-R02"]))
    errs = " | ".join(res["errors"])
    assert "storage destination" in errs and "listed twice" in errs
    res = env.s.rules.check(rule(satellites=[99999], frequency_hz=None))
    assert any("does not know the downlink of NORAD 99999" in e for e in res["errors"])


def test_rules_persist_with_revisions_and_history(env):
    r = env.s.rules.create(rule(enabled=False), "alice")
    assert r["revision"] == 1 and r["enabled"] is False and r["pre_aos_s"] == 60
    with pytest.raises(SchedulerError, match="already called"):
        env.s.rules.create(rule(enabled=False), "bob")
    r2 = env.s.rules.update(r["rule_id"], rule(enabled=False, priority=80), 1, "alice")
    assert r2["revision"] == 2 and r2["priority"] == 80
    with pytest.raises(SchedulerError) as stale:
        env.s.rules.update(r["rule_id"], rule(priority=10), 1, "bob")
    assert stale.value.status == 409
    env.s.rules.delete(r["rule_id"], 2, "alice")
    assert env.s.rules.list() == []
    assert [h["action"] for h in env.s.rules.history(r["rule_id"])] == ["deleted", "updated", "created"]
    # survives a restart (new Scheduler on the same database)
    s2 = Scheduler(env.cfg, env.engine, env.clock, disk_free=lambda _: 10**12, present=lambda rx: True,
                   satdump_pipelines=frozenset())
    assert s2.rules.get(r["rule_id"], include_deleted=True)["deleted_at"]


def test_toml_automation_is_imported_once_as_disabled_rules_and_never_used(env):
    made = env.s.rules.import_from_toml()
    assert len(made) == 1 and env.s.rules.import_from_toml() == []            # once
    imp = env.s.rules.get(made[0])
    assert imp["receiver_id"] == "OBS-R01" and imp["enabled"] is False and imp["satellites"] == [M23, M24]
    env.s.rules.delete(made[0], imp["revision"], "o")
    assert env.s.rules.import_from_toml() == []                               # deleting it does not bring it back
    res = env.s.rules.check(rule(receiver_id="OBS-R01", name="x"))
    assert res["ok"] and any("legacy mode" in w or "observatory.toml" in w for w in res["warnings"])


# ------------------------------------------------------------------ planning
def test_hybrid_mode_books_passes_once_with_margins_in_utc(env, monkeypatch):
    Passes(monkeypatch, [(M23, 120, 14, 45), (M24, 300, 12, 15), (M24, 600, 13, 60)])
    r = env.s.rules.create(rule(), "o")
    out = env.planner.reconcile()
    assert out["rules"][r["rule_id"]]["inserted"] == 2                       # the 15° pass is below the rule's 20°
    jobs = rule_jobs(env)
    assert [j["status"] for j in jobs] == ["scheduled", "scheduled"] and all(j["executes"] for j in jobs)
    j = jobs[0]
    assert j["record_start"] == NOW + dt.timedelta(minutes=119)              # AOS - 60 s
    assert j["record_end"] == NOW + dt.timedelta(minutes=134, seconds=30)     # LOS + 30 s
    assert j["record_start"].tzinfo is not None and j["source"] == "auto" and j["rule_id"] == r["rule_id"]
    assert j["spec"]["radio"]["sample_rate_hz"] == 1_024_000 and j["spec"]["metadata"]["max_elevation_deg"] == 45
    assert lifecycle(j["status"]) == "SCHEDULED"
    again = env.planner.reconcile()                                          # idempotent: no duplicates
    assert again["rules"][r["rule_id"]]["inserted"] == 0 and len(rule_jobs(env)) == 2


def test_manual_mode_only_predicts_and_holds_no_receiver(env, monkeypatch):
    save(env.s, "scheduling", mode="manual")
    Passes(monkeypatch, [(M23, 120, 14, 45)])
    env.s.rules.create(rule(), "o")
    env.planner.reconcile()
    (j,) = rule_jobs(env)
    assert j["status"] == "predicted" and not j["executes"] and lifecycle(j["status"]) == "PREDICTED"
    assert env.s.next_reservation("OBS-R02", NOW) is None                    # nothing holds the device
    save(env.s, "scheduling", mode="hybrid")                                  # switching on books the same job
    env.planner.reconcile()
    (j2,) = rule_jobs(env)
    assert j2["job_id"] == j["job_id"] and j2["status"] == "scheduled" and j2["executes"]


def test_scheduler_restart_recovers_without_duplicates(env, monkeypatch):
    Passes(monkeypatch, [(M23, 120, 14, 45), (M24, 400, 12, 50)])
    env.s.rules.create(rule(), "o")
    env.planner.reconcile()
    before = {j["job_id"] for j in rule_jobs(env)}
    s2 = Scheduler(env.cfg, env.engine, env.clock, disk_free=lambda _: 500 * 10**9,
                   present=lambda rx: rx.serial in env.present, satdump_pipelines=frozenset({"meteor_m2-x_lrpt"}))
    out = RulePlanner(s2).reconcile()
    assert {j["job_id"] for j in rule_jobs(env)} == before
    assert sum(v["inserted"] for v in out["rules"].values()) == 0


def test_refresh_moves_planned_jobs_but_never_overrides_or_running_ones(env, monkeypatch):
    pp = Passes(monkeypatch, [(M23, 120, 14, 45), (M24, 300, 12, 50), (M23, 500, 12, 40)])
    env.s.rules.create(rule(), "o")
    env.planner.reconcile()
    a, b, c = rule_jobs(env)
    env.s.modify(b["job_id"], {"start": (b["record_start"] - dt.timedelta(minutes=2)).isoformat(),
                               "note": "start early"}, "owner")
    with env.engine.begin() as conn:                                         # pretend c is being recorded now
        conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == c["job_id"]).values(status="recording"))
    pp.shift_s = 45                                                          # new elements: AOS 45 s later
    env.planner.reconcile()
    a2, b2, c2 = (next(j for j in rule_jobs(env) if j["job_id"] == x["job_id"]) for x in (a, b, c))
    assert a2["record_start"] == a["record_start"] + dt.timedelta(seconds=45)  # updated in place
    assert b2["record_start"] == b["record_start"] - dt.timedelta(minutes=2)   # the owner's change is kept
    assert b2["override"]["fields"] == ["note", "start"] and "kept as you set it" in b2["override"]["planner_note"]
    assert c2["record_start"] == c["record_start"] and c2["status"] == "recording"
    assert len(rule_jobs(env)) == 3


def test_passes_that_disappear_are_skipped_not_deleted(env, monkeypatch):
    pp = Passes(monkeypatch, [(M23, 120, 14, 45), (M24, 300, 12, 50)])
    env.s.rules.create(rule(), "o")
    env.planner.reconcile()
    pp.passes = pp.passes[:1]
    env.planner.reconcile()
    gone = rule_jobs(env)[1]
    assert gone["status"] == "skipped" and "no longer predicted" in gone["status_reason"]
    assert lifecycle(gone["status"]) == "SKIPPED"


def test_overlapping_passes_go_to_the_backup_receiver(env, monkeypatch):
    Passes(monkeypatch, [(M23, 120, 14, 45), (M24, 125, 12, 60)])
    env.s.rules.create(rule(backup_receivers=["RSP1B"], sample_rate_hz=None), "o")
    env.planner.reconcile()
    jobs = {j["spec"]["metadata"]["norad"]: j for j in rule_jobs(env)}
    assert jobs[M24]["receiver_id"] == "OBS-R02"                             # the higher pass gets the preferred
    assert jobs[M23]["receiver_id"] == "RSP1B" and jobs[M23]["status"] == "scheduled"
    assert "backup receiver RSP1B" in jobs[M23]["status_reason"] and "OBS-R02 is busy" in jobs[M23]["status_reason"]


def test_higher_priority_rule_displaces_a_lower_one_which_tries_its_backups(env, monkeypatch):
    Passes(monkeypatch, [(M23, 120, 14, 45), (ISS, 122, 10, 70)])
    low = env.s.rules.create(rule(name="low", satellites=[M23], priority=10, backup_receivers=["RSP1B"],
                                  sample_rate_hz=None), "o")
    env.planner.reconcile()
    (m,) = rule_jobs(env)
    assert m["receiver_id"] == "OBS-R02"
    high = env.s.rules.create(rule(name="high", satellites=[ISS], frequency_hz=None, priority=90), "o")
    env.planner.reconcile()
    jobs = {j["rule_id"]: j for j in rule_jobs(env)}
    assert jobs[high["rule_id"]]["receiver_id"] == "OBS-R02" and jobs[high["rule_id"]]["status"] == "scheduled"
    moved = jobs[low["rule_id"]]
    assert moved["job_id"] == m["job_id"] and moved["receiver_id"] == "RSP1B" and moved["status"] == "scheduled"
    events = [e["reason"] for e in env.s.get(m["job_id"])["events"]]
    assert any("displaced by the higher-priority rule 'high'" in e for e in events)


def test_the_same_pass_is_booked_once(env, monkeypatch):
    Passes(monkeypatch, [(M23, 120, 14, 45)])
    env.s.rules.create(rule(name="a", priority=60), "o")
    env.s.rules.create(rule(name="b", receiver_id="RSP1B", sample_rate_hz=None, priority=40), "o")
    out = env.planner.reconcile()
    assert len(rule_jobs(env)) == 1 and rule_jobs(env)[0]["receiver_id"] == "OBS-R02"
    assert sum(v["duplicates"] for v in out["rules"].values()) == 1
    job_form = {"name": "again", "norad": M23, "receiver_id": "RSP1B", "start": (NOW + dt.timedelta(minutes=120)).isoformat(),
                "end": (NOW + dt.timedelta(minutes=134)).isoformat(), "frequency_hz": 137_900_000}
    with pytest.raises(SchedulerError, match="already being recorded"):
        schedview.book(env.s, job_form, "owner")
    assert schedview.book(env.s, {**job_form, "allow_duplicate": True}, "owner")["ok"]


def test_unavailable_receiver_uses_backup_or_records_why_it_was_skipped(env, monkeypatch):
    Passes(monkeypatch, [(M23, 120, 14, 45), (M24, 400, 12, 50)])
    env.present.discard("OBS-R02")
    r = env.s.rules.create(rule(), "o")
    env.planner.reconcile()
    jobs = rule_jobs(env)
    assert [j["status"] for j in jobs] == ["skipped", "skipped"]
    assert "OBS-R02 is not connected" in jobs[0]["status_reason"]
    assert env.s.next_reservation("OBS-R02", NOW) is None
    env.s.rules.update(r["rule_id"], rule(backup_receivers=["RSP1B"], sample_rate_hz=None), 1, "o")
    env.planner.reconcile()
    assert {j["receiver_id"] for j in rule_jobs(env)} == {"RSP1B"}
    env.present.add("OBS-R02")                         # back: jobs that have not started return to the preferred
    env.planner.reconcile()
    jobs = rule_jobs(env)
    assert {j["receiver_id"] for j in jobs} == {"OBS-R02"} and len(jobs) == 2
    assert any("(was on RSP1B)" in e["reason"] for e in env.s.get(jobs[0]["job_id"])["events"])


def test_cancelled_by_a_person_is_never_rebooked_until_handed_back(env, monkeypatch):
    Passes(monkeypatch, [(M23, 120, 14, 45)])
    env.s.rules.create(rule(), "o")
    env.planner.reconcile()
    (j,) = rule_jobs(env)
    env.s.cancel(j["job_id"], "not this one", "owner")
    env.planner.reconcile()
    (j2,) = rule_jobs(env)
    assert j2["status"] == "cancelled" and j2["override"]["fields"] == ["cancelled"]
    env.s.revert(j["job_id"], "owner")
    env.planner.reconcile()
    (j3,) = rule_jobs(env)
    assert j3["status"] == "scheduled" and j3["override"] is None and j3["job_id"] == j["job_id"]


def test_disabling_a_rule_skips_its_future_jobs_and_enabling_restores_them(env, monkeypatch):
    Passes(monkeypatch, [(M23, 120, 14, 45)])
    r = env.s.rules.create(rule(), "o")
    env.planner.reconcile()
    env.s.rules.update(r["rule_id"], rule(enabled=False), 1, "o")
    env.planner.reconcile()
    (j,) = rule_jobs(env)
    assert j["status"] == "skipped" and "disabled" in j["status_reason"]
    env.s.rules.update(r["rule_id"], rule(enabled=True), 2, "o")
    env.planner.reconcile()
    assert rule_jobs(env)[0]["status"] == "scheduled"


def test_manual_jobs_still_preempt_rule_jobs_which_then_move_to_a_backup(env, monkeypatch):
    from test_scheduler import fix_dirs, rtl_job
    Passes(monkeypatch, [(M23, 120, 14, 45)])
    env.s.rules.create(rule(backup_receivers=["RSP1B"], sample_rate_hz=None), "o")
    env.planner.reconcile()
    (j,) = rule_jobs(env)
    res = env.s.submit([fix_dirs(copy.deepcopy(rtl_job("mine", start=118, end=130)), env.tmp)])
    assert res["results"][0]["stored"] and res["results"][0]["preempts"][0]["job_id"] == j["job_id"]
    env.planner.reconcile()
    (j2,) = rule_jobs(env)
    assert j2["job_id"] == j["job_id"] and j2["receiver_id"] == "RSP1B" and j2["status"] == "scheduled"


def test_missing_and_stale_orbital_data(env, monkeypatch):
    real = predict.predict_passes
    r = env.s.rules.create(rule(satellites=[M23, 99999]), "o")
    out = env.planner.reconcile()
    warns = " | ".join(out["rules"][r["rule_id"]]["warnings"])
    assert "NORAD 99999: no orbital elements" in warns
    env.clock.now = NOW + dt.timedelta(days=30)
    out = env.planner.reconcile()
    assert "days old" in " | ".join(out["rules"][r["rule_id"]]["warnings"])
    assert predict.predict_passes is real
    save(env.s, "observatory", latitude=None, longitude=None)
    out = env.planner.reconcile()
    assert "location is not set" in out["warnings"][0]


def test_real_predictions_drive_rule_jobs(env):
    r = env.s.rules.create(rule(min_max_elevation_deg=10), "o")
    out = env.planner.reconcile()
    jobs = rule_jobs(env)
    assert out["rules"][r["rule_id"]]["passes"] >= 2 and jobs
    for j in jobs:
        m = j["spec"]["metadata"]
        assert m["max_elevation_deg"] >= 10 and m["aos"] < m["los"] and m["elements_source"]


# ------------------------------------------------------------------ overrides
def test_override_is_validated_like_a_booking(env, monkeypatch):
    Passes(monkeypatch, [(M23, 120, 14, 45)])
    env.s.rules.create(rule(), "o")
    env.planner.reconcile()
    (j,) = rule_jobs(env)
    with pytest.raises(SchedulerError, match="Not changed"):
        env.s.modify(j["job_id"], {"sample_rate_hz": 5_000_000}, "owner")
    with pytest.raises(SchedulerError, match="cannot change"):
        env.s.modify(j["job_id"], {"job_name": "x"}, "owner")
    res = env.s.modify(j["job_id"], {"sample_rate_hz": 2_048_000, "receiver_id": "OBS-R02"}, "owner")
    stored = res["job"]
    assert stored["spec"]["radio"]["sample_rate_hz"] == 2_048_000 and stored["override"]["by"] == "owner"
    assert stored["status"] == "scheduled" and "changed by owner" in stored["status_reason"]
    assert stored["override"]["previous"]["status"] == "scheduled"
    with env.engine.begin() as conn:
        conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == j["job_id"]).values(status="recording"))
    with pytest.raises(SchedulerError, match="have not started"):
        env.s.modify(j["job_id"], {"note": "x"}, "owner")


def test_a_predicted_job_can_be_booked_by_hand_in_manual_mode(env, monkeypatch):
    save(env.s, "scheduling", mode="manual")
    Passes(monkeypatch, [(M23, 120, 14, 45)])
    env.s.rules.create(rule(), "o")
    env.planner.reconcile()
    (j,) = rule_jobs(env)
    env.s.modify(j["job_id"], {"note": "record this one"}, "owner")
    env.planner.reconcile()
    (j2,) = rule_jobs(env)
    assert j2["status"] == "scheduled" and j2["executes"] and j2["override"]


# ------------------------------------------------------------------ manual recordings and time zones
def test_arbitrary_recording_in_local_time_is_stored_in_utc(env):
    save(env.s, "observatory", timezone="America/New_York")
    form = {"name": "Night FM survey", "receiver_id": "OBS-R02", "start": "2026-10-14T09:00", "end": "2026-10-14T09:10",
            "frequency_hz": 98_100_000, "sample_rate_hz": 2_048_000, "gain_db": 20.7, "gain_mode": "manual"}
    res = schedview.book(env.s, form, "owner")
    job = env.s.get(res["job_id"])
    assert job["record_start"] == "2026-10-14T13:00:00+00:00" and job["record_end"] == "2026-10-14T13:10:00+00:00"
    assert job["job_name"] == "Night-FM-survey" and job["spec"]["metadata"]["title"] == "Night FM survey"
    assert job["spec"]["target"]["type"] == "frequency" and job["spec"]["radio"]["gain_db"] == 20.7
    # across the end of daylight saving time (1 Nov 2026): 09:00 EST is 14:00 UTC
    res = schedview.book(env.s, {**form, "name": "after-dst", "start": "2026-11-02T09:00", "end": "2026-11-02T09:05"},
                         "owner")
    assert env.s.get(res["job_id"])["record_start"] == "2026-11-02T14:00:00+00:00"
    with pytest.raises(SchedulerError, match="unknown form fields"):
        schedview.book(env.s, {**form, "evil": 1}, "owner")


def test_pass_form_is_prefilled_and_can_be_changed_before_booking(env):
    found = __import__("aobs.satellites.passes", fromlist=["PassService"]).PassService(env.s).predicted(48)
    p = next(x for x in found["passes"] if x["norad"] in (M23, M24))
    form = schedview.form_for_pass(env.s, p["norad"], p["aos"], "OBS-R02")
    assert form["frequency_hz"] == 137_900_000 and form["pass"]["max_elevation_deg"] == p["max_elevation_deg"]
    assert abs((dt.datetime.fromisoformat(form["start"]) - dt.datetime.fromisoformat(p["aos"])).total_seconds()) < 1
    assert form["sample_rate_hz"] == 1_024_000
    form.update(sample_rate_hz=2_048_000, pre_roll_s=60, name="my pass")
    res = schedview.book(env.s, form, "owner")
    job = env.s.get(res["job_id"])
    assert job["spec"]["radio"]["sample_rate_hz"] == 2_048_000 and job["spec"]["metadata"]["booked_from"] == "predicted pass"


# ------------------------------------------------------------------ states, calendar, validation of outputs
def test_lifecycle_states_cover_every_status():
    assert lifecycle("preparing") == "RESERVED" and lifecycle("recorded") == "PROCESSING"
    assert lifecycle("recorded", source="legacy") == "COMPLETED"
    assert lifecycle("missed") == "FAILED" and lifecycle("preempted") == "SKIPPED" and lifecycle("shadow") == "PREDICTED"
    assert set(statuses_for(["SKIPPED"])) == {"skipped", "preempted"}
    from aobs.scheduling.store import STATES
    assert all(lifecycle(s) in ("PREDICTED", "SCHEDULED", "RESERVED", "RECORDING", "PROCESSING", "COMPLETED",
                                "SKIPPED", "CANCELLED", "FAILED") for s in STATES)


def test_calendar_filters_by_satellite_receiver_and_state(env, monkeypatch):
    Passes(monkeypatch, [(M23, 120, 14, 45), (M24, 300, 12, 50)])
    env.s.rules.create(rule(), "o")
    env.planner.reconcile()
    rows = schedview.calendar(env.s, NOW, NOW + dt.timedelta(days=1))["jobs"]
    assert len(rows) == 2 and rows[0]["lifecycle"] == "SCHEDULED" and rows[0]["rule_name"] == "METEOR on R02"
    assert len(schedview.calendar(env.s, NOW, NOW + dt.timedelta(days=1), satellite=str(M24))["jobs"]) == 1
    assert schedview.calendar(env.s, NOW, NOW + dt.timedelta(days=1), receiver="RSP1B")["jobs"] == []
    assert schedview.calendar(env.s, NOW, NOW + dt.timedelta(days=1), states=["FAILED"])["jobs"] == []


def test_completed_only_after_the_output_is_validated(tmp_path):
    job = {"paths": {"raw": str(tmp_path / "a.cu8")}, "spec": {"recording": {"format": "cu8"}}}
    assert "does not exist" in validate_output(job, {"status": "recorded", "bytes": 10})[0]
    f = tmp_path / "a.cu8"
    f.write_bytes(b"\x80" * 1001)
    probs = validate_output(job, {"file": str(f), "bytes": 1001, "expected_bytes": 2000})
    assert any("1001 of 2000" in p for p in probs) and any("whole number" in p for p in probs)
    f.write_bytes(b"\x80" * 2000)
    assert validate_output(job, {"file": str(f), "bytes": 1990, "expected_bytes": 2000}) == \
        ["the file holds 2000 bytes but the recorder reported 1990"]
    assert validate_output(job, {"file": str(f), "bytes": 2000, "expected_bytes": 2000}) == []
    flat = {"applicable": True, "status": "suspicious_flat", "label": "suspiciously flat", "summary": "stuck"}
    assert "IQ health" in validate_output(job, {"file": str(f), "bytes": 2000, "iq_health": flat})[0]


def test_raw_iq_retention_follows_the_rule(env, monkeypatch):
    Passes(monkeypatch, [(M23, 120, 14, 45)])
    env.s.rules.create(rule(raw_iq_policy="hours", raw_iq_hours=2), "o")
    env.planner.reconcile()
    (j,) = rule_jobs(env)
    raw = pathlib.Path(j["paths"]["raw"])
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"x" * 10)
    with env.engine.begin() as conn:
        conn.execute(sched_jobs.update().where(sched_jobs.c.job_id == j["job_id"]).values(status="completed"))
    env.clock.now = j["record_end"] + dt.timedelta(hours=1, minutes=30)
    assert env.planner.prune() == 0 and raw.exists()
    env.clock.now = j["record_end"] + dt.timedelta(hours=2, minutes=1)
    assert env.planner.prune() == 1 and not raw.exists()


# ------------------------------------------------------------------ orbital elements
def _omm_answer(norad: int) -> str:
    e = predict.load_elements(None)[norad]
    return json.dumps([predict.tle_to_omm(e)])


def test_omm_sources_cache_and_fetch_times(tmp_path):
    def fetch(url: str) -> str:
        assert "FORMAT=JSON" in url
        return _omm_answer(int(url.split("CATNR=")[1].split("&")[0]))
    now = NOW
    res = predict.refresh_tles([M23, ISS], tmp_path, fetch, sources=predict.source_list({}), now=now)
    assert res["updated"] == [ISS, M23] and res["formats"] == {M23: "omm", ISS: "omm"}
    el = predict.load_elements(tmp_path)
    assert el[M23].format == "omm" and el[M23].fetched_at == now and el[M23].source == "downloaded from celestrak.org"
    st = predict.element_status(el, [M23], NOW + dt.timedelta(days=20), stale_days=7)[0]
    assert st["stale"] and st["format"] == "omm" and "limit 7" in st["warning"]
    assert predict.last_refresh(tmp_path) == now
    # OMM gives the same passes as the TLE it came from
    tle = predict.load_elements(None)
    a = predict.predict_passes(tle, [M23], 45.0, 10.0, 100, NOW, 24, 10)
    b = predict.predict_passes(el, [M23], 45.0, 10.0, 100, NOW, 24, 10)
    assert [p["aos"] for p in a] == [p["aos"] for p in b] and b[0]["elements_format"] == "omm"


def test_omm_failure_falls_back_to_tle_and_custom_sources_need_a_placeholder(tmp_path):
    sample = predict.SAMPLE_TLE.read_text().splitlines()

    def fetch(url: str) -> str:
        if "FORMAT=JSON" in url:
            raise OSError("no JSON today")
        return "\n".join(sample[0:3]) + "\n"
    res = predict.refresh_tles([M23], tmp_path, fetch, sources=predict.source_list({"elements_source": "celestrak_omm"}))
    assert res["formats"] == {M23: "tle"}
    with pytest.raises(ValueError, match="norad"):
        predict.source_list({"elements_source": "custom", "custom_url": "https://example.org/x"})
    assert predict.source_list({"elements_source": "custom", "custom_url": "https://e.org/{norad}.json",
                                "custom_format": "omm"}) == [("https://e.org/{norad}.json", "omm")]


def test_elements_refresh_automatically_when_due_and_recalculates(env, monkeypatch):
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return _omm_answer(int(url.split("CATNR=")[1].split("&")[0]))
    save(env.s, "satellites", refresh_online=True, refresh_hours=6)
    env.s.rules.create(rule(satellites=[M23]), "o")
    planner = RulePlanner(env.s, fetch=fetch)
    out = planner.reconcile()
    assert out["refresh"]["updated"] and calls
    n = len(calls)
    planner.reconcile()
    assert len(calls) == n                                                   # not due yet
    env.clock.now = NOW + dt.timedelta(hours=7)
    planner.reconcile()
    assert len(calls) > n


# ------------------------------------------------------------------ database migration
def test_migration_6_adds_rules_and_keeps_jobs(tmp_path):
    from aobs.db.migrate import current_version, migrate
    e = make_engine(f"sqlite:///{tmp_path / 'db.sqlite3'}")
    init_db(e)
    with e.begin() as conn:                                                 # wind back to schema 5
        conn.execute(text("DROP TABLE sched_rules"))
        conn.execute(text("DROP TABLE sched_rule_versions"))
        conn.execute(text("DROP INDEX ix_sched_jobs_rule"))
        conn.execute(text("ALTER TABLE sched_jobs DROP COLUMN rule_id"))
        conn.execute(text("ALTER TABLE sched_jobs DROP COLUMN override"))
        conn.execute(text("INSERT INTO sched_jobs (job_id, job_name, source, receiver_id, device_serial, status, executes,"
                          " reservation_start, reservation_end, record_start, record_end, spec, created_at, updated_at)"
                          " VALUES ('m1', 'old', 'manual', 'OBS-R02', 'OBS-R02', 'completed', 1, '2026-10-01 00:00:00',"
                          " '2026-10-01 00:10:00', '2026-10-01 00:01:00', '2026-10-01 00:09:00', '{}',"
                          " '2026-10-01 00:00:00', '2026-10-01 00:00:00')"))
        conn.execute(text("UPDATE schema_info SET value='5' WHERE key='schema_version'"))
    out = migrate(e)
    assert out["steps"][0] == "6: automation rules" and current_version(e) >= 6 and out["backup"]
    with e.connect() as conn:
        row = conn.execute(text("SELECT job_id, rule_id, override FROM sched_jobs")).one()
        assert tuple(row) == ("m1", None, None)
        assert conn.execute(text("SELECT COUNT(*) FROM sched_rules")).scalar() == 0
    assert migrate(e)["steps"] == []


# ------------------------------------------------------------------ the HTTP API
def test_scheduler_api_round_trip(env, monkeypatch):
    import socket
    import threading
    import time
    import urllib.error
    import urllib.request

    import uvicorn

    from aobs.coordination.api import create_app
    from aobs.coordination.broker import Broker
    from aobs.scheduling.planner import Planner
    from aobs.scheduling.runner import Runner
    from aobs.scheduling.service import SchedulerService
    from test_broker import FakeHw
    from test_scheduler import FakeRunnerHw

    Passes(monkeypatch, [(M23, 120, 14, 45), (M24, 300, 12, 50)])
    env.cfg.raw["broker"] = {"receivers": ["OBS-R05"]}
    svc = SchedulerService(env.s, Planner(env.s, predictor=lambda rx, h: []), Runner(env.s, FakeRunnerHw()))
    svc.start = lambda: None
    app = create_app(Broker(env.cfg, FakeHw(), env.clock), "t", scheduler=svc)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)

    def call(path: str, method: str = "GET", body: Any = None) -> tuple[int, Any]:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     headers={"Authorization": "Bearer t", "Content-Type": "application/json"},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")
    try:
        code, ov = call("/v1/scheduler/overview")
        assert code == 200 and ov["mode"] == "hybrid" and "SKIPPED" in ov["states"]
        assert {r["id"]: r["owned_elsewhere"] is not None for r in ov["receivers"]}["OBS-R01"]
        code, bad = call("/v1/scheduler/rules", "POST", {"rule": {"name": "x"}})
        assert code == 400 and any("satellite" in e for e in bad["errors"])
        code, made = call("/v1/scheduler/rules", "POST", {"rule": rule()})
        assert code == 200 and made["reconcile"]["rules"][made["rule"]["rule_id"]]["inserted"] == 2
        code, cal = call("/v1/scheduler/calendar?state=SCHEDULED")
        assert code == 200 and len(cal["jobs"]) == 2
        jid = cal["jobs"][0]["job_id"]
        code, ch = call(f"/v1/scheduler/jobs/{jid}/change", "POST", {"changes": {"note": "keep"}})
        assert code == 200 and ch["job"]["override"]["note"] == "keep"
        code, det = call(f"/v1/scheduler/jobs/{jid}")
        assert code == 200 and det["override"] and det["events"][-1]["actor"] == "api"
        code, rev = call(f"/v1/scheduler/jobs/{jid}/revert", "POST", {})
        assert code == 200 and rev["job"]["override"] is None and rev["job"]["status"] == "scheduled"
        code, pv = call("/v1/scheduler/rules/preview", "POST", {"rule": rule(name="other", receiver_id="RSP1B",
                                                                             sample_rate_hz=None)})
        assert code == 200 and all(p["duplicate_of"] for p in pv["passes"])
        code, upd = call(f"/v1/scheduler/rules/{made['rule']['rule_id']}", "PUT",
                         {"rule": rule(enabled=False), "revision": 99})
        assert code == 409
        code, _ = call(f"/v1/scheduler/jobs/{jid}/cancel", "POST", {"reason": "no"})
        assert code == 200
        code, hist = call(f"/v1/scheduler/calendar?state=CANCELLED")
        assert [j["job_id"] for j in hist["jobs"]] == [jid]
        code, rec = call("/v1/scheduler/manual", "POST", {"form": {
            "name": "fm", "receiver_id": "OBS-R02", "start": (NOW + dt.timedelta(hours=10)).isoformat(),
            "end": (NOW + dt.timedelta(hours=10, minutes=5)).isoformat(), "frequency_hz": 98_100_000}})
        assert code == 200 and rec["job_id"].startswith("m"), rec
        code, st = call("/v1/scheduler/status")
        assert code == 200 and "last_rules" in st
    finally:
        server.should_exit = True


# ------------------------------------------------------------------ execution: scheduled is not recorded
def _execute(env: Any, hw: Any, job: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    from aobs.scheduling.runner import Runner
    from test_scheduler import drain, inline_processor
    env.clock.now = job["reservation_start"] + dt.timedelta(seconds=5)
    proc = inline_processor(env)
    Runner(env.s, hw, sleep=lambda s: None, processor=proc).tick(threaded=False)
    env.clock.now = job["record_end"] + dt.timedelta(minutes=5)
    drain(proc)
    return env.s.get(job["job_id"]), proc


def test_a_rule_recording_runs_and_completes_only_after_validation(env, monkeypatch):
    from test_scheduler import FakeRunnerHw
    Passes(monkeypatch, [(M23, 120, 14, 45)])
    env.s.rules.create(rule(), "o")
    env.planner.reconcile()
    (j,) = rule_jobs(env)
    done, _ = _execute(env, FakeRunnerHw(), j)
    assert done["status"] == "completed" and done["lifecycle"] == "COMPLETED", done["status_reason"]
    assert [lifecycle(e["new_status"]) for e in done["events"]] == ["SCHEDULED", "RESERVED", "RECORDING", "PROCESSING",
                                                                     "PROCESSING", "COMPLETED"]
    assert done["result"]["capture"]["validation"]["ok"] is True
    assert "recorded and validated" in " ".join(e["reason"] or "" for e in done["events"])


def test_a_recording_whose_file_is_cut_is_failed_not_completed(env, monkeypatch):
    from test_scheduler import FakeRunnerHw

    class CutHw(FakeRunnerHw):
        def spawn(self, argv, log):
            h = super().spawn(argv, log)
            run = json.loads(pathlib.Path(argv[-1]).read_text())
            raw = pathlib.Path(run["raw"])
            raw.write_bytes(raw.read_bytes()[:-1])              # one byte short: not a whole number of samples
            res = json.loads(pathlib.Path(run["result"]).read_text())
            res["bytes"] = raw.stat().st_size
            pathlib.Path(run["result"]).write_text(json.dumps(res))
            return h
    Passes(monkeypatch, [(M23, 120, 14, 45)])
    env.s.rules.create(rule(), "o")
    env.planner.reconcile()
    (j,) = rule_jobs(env)
    done, _ = _execute(env, CutHw(), j)
    assert done["status"] == "failed" and done["lifecycle"] == "FAILED"
    assert "failed validation" in done["status_reason"] and "whole number" in done["status_reason"]
    assert pathlib.Path(done["paths"]["raw"]).is_file()                # kept for inspection


def test_google_calendar_gets_booked_rule_jobs_only(env, monkeypatch):
    from aobs.scheduling import calendar as schedcal
    Passes(monkeypatch, [(M23, 120, 14, 45), (M24, 300, 12, 50)])
    save(env.s, "scheduling", mode="manual")
    env.s.rules.create(rule(), "o")
    env.planner.reconcile()

    from test_service_and_calendar import FakeCalendar
    fake = FakeCalendar()
    schedcal.sync(env.s, fake, "cal")
    assert fake.events == {}                                                 # PREDICTED only: nothing published
    env.present.discard("OBS-R02")
    save(env.s, "scheduling", mode="hybrid")
    env.planner.reconcile()                                                  # SKIPPED (not connected): not published
    schedcal.sync(env.s, fake, "cal")
    assert fake.events == {}
    env.present.add("OBS-R02")
    env.planner.reconcile()
    schedcal.sync(env.s, fake, "cal")
    assert len(fake.events) == 2
