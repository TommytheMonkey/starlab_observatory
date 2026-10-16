"""Brief 8: recording archive, decoder profiles, separate outcomes, storage, transfer and retention."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import threading
import time
import types
from typing import Any

import pytest

from aobs.scheduling.catalog import Catalog
from aobs.scheduling.retention import Retention, policy, set_keep
from aobs.scheduling.store import SchedulerError
from test_execution import RealClock, ex, pi, sim_job  # noqa: F401  (fixtures)
from test_scheduler import NOW, FakeRunnerHw, drain, env, inline_processor, rtl_job, run_job, submit  # noqa: F401


def fake_satdump(tmp_path: pathlib.Path, good: bool) -> str:
    """A SatDump stand-in: `good` writes 2 CADU frames and a real image (and exits 1 like SatDump 1.2.2);
    otherwise it reports NOSYNC and writes nothing."""
    f = tmp_path / ("satdump-good" if good else "satdump-bad")
    if good:
        import numpy as np
        from PIL import Image
        img = tmp_path / "real.png"
        Image.fromarray((np.random.default_rng(1).random((80, 80)) * 255).astype("uint8")).save(img)
        f.write_text(f'#!/bin/sh\nmkdir -p "$4/MSU-MR"\necho "$@" > "$4/args.txt"\ncp {img} "$4/MSU-MR/rgb.png"\n'
                     'head -c 2048 /dev/zero > "$4/meteor_m2-x_lrpt.cadu"\necho "SNR : 7.5dB SYNCED"\nexit 1\n')
    else:
        f.write_text('#!/bin/sh\nmkdir -p "$4"\necho "$@" > "$4/args.txt"\necho "Deframer : NOSYNC"\nexit 0\n')
    f.chmod(0o755)
    return str(f)


def recorded(env: Any, **kw: Any) -> tuple[str, Any]:
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(**kw))
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc, 40)
    return jid, runner.proc


def outcome_of(env: Any, proc: Any, jid: str) -> dict[str, Any]:
    return Catalog(env.s, proc).recordings(recording_id=jid)["recordings"][0]["outcome"]


# ------------------------------------------------------------------ outcomes
def test_a_failed_decode_leaves_the_recording_completed_and_says_what_failed(env, tmp_path):
    env.s.settings["satdump_path"] = fake_satdump(tmp_path, good=False)
    env.s.profiles.seed()
    jid, proc = recorded(env)                      # rtl_job decodes METEOR after recording
    job = env.s.get(jid)
    assert job["status"] == "completed", job["status_reason"]           # never failed because the decode failed
    dec = [a for a in proc.attempts(jid) if a["kind"] == "decode"]
    assert dec and dec[0]["status"] == "failed" and "no CADU frames" in dec[0]["status_reason"]
    o = outcome_of(env, proc, jid)
    assert o["recording"] == "complete" and o["frames"] == 0 and o["images"] == 0
    assert o["signal"] in ("not detected", "unknown") and o["level"] in ("recorded", "signal")
    assert o["decode"] == "no frames"
    assert Catalog(env.s, proc).recordings(outcome="image")["total"] == 0


def test_a_good_decode_reaches_every_outcome_level_with_its_own_evidence(env, tmp_path):
    env.s.settings["satdump_path"] = fake_satdump(tmp_path, good=True)
    env.s.profiles.seed()
    jid, proc = recorded(env)
    o = outcome_of(env, proc, jid)
    assert o["recording"] == "complete" and o["signal"] == "detected" and "decoder locked" in o["signal_note"]
    assert o["frames"] == 2 and o["images"] == 1 and o["level"] == "image"
    d = Catalog(env.s, proc).detail(jid)
    assert any(f["kind"] == "image" and f["viewable"] for f in d["files"])
    assert any(f["kind"] == "log" and f["readable"] and "satdump" in (f["label"] or "") for f in d["files"])
    dec = next(a for a in d["attempts"] if a["kind"] == "decode")
    assert dec["profile"]["name"].startswith("METEOR") and dec["frames"] == 2      # the seeded profile was used
    whats = [h["what"] for h in d["history"]]
    assert "state" in whats and "decode" in whats and "analyze" in whats


def test_capture_success_alone_is_never_reported_as_decoded(env):
    jid, proc = recorded(env, satdump={"enabled": False}, postprocess={"decode": False})
    o = outcome_of(env, proc, jid)
    assert o["recording"] == "complete" and o["frames"] is None and o["images"] is None and o["decode"] == "not run"
    assert o["level"] in ("recorded", "signal")


def test_partial_recordings_are_kept_processable_and_reported_as_partial(env):
    from aobs.scheduling.runner import Runner
    jid = submit(env, rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))["results"][0]["job_id"]
    env.clock.now = NOW + dt.timedelta(minutes=59, seconds=30)
    hw = FakeRunnerHw()
    real = hw.spawn

    def spawn(argv, log):
        h = real(argv, log)
        run = json.loads(pathlib.Path(argv[-1]).read_text())
        raw = pathlib.Path(run["raw"])
        part = raw.with_name(raw.name + ".part")
        raw.rename(part)
        pathlib.Path(run["result"]).write_text(json.dumps({"status": "interrupted", "bytes": part.stat().st_size,
                                                            "file": str(part)}))
        return h
    hw.spawn = spawn
    proc = inline_processor(env)
    Runner(env.s, hw, sleep=lambda s: None, processor=proc).tick(threaded=False)
    assert env.s.get(jid)["status"] == "partial"
    env.clock.now = NOW + dt.timedelta(minutes=80)
    a = proc.submit(jid, "analyze", {"analysis": ["spectrum", "integrity"]})
    drain(proc)
    assert proc.attempt(a["attempt_id"])["status"] == "completed"
    cat = Catalog(env.s, proc)
    r = cat.recordings(recording_id=jid)["recordings"][0]
    assert r["outcome"]["recording"] == "partial" and r["outcome"]["level"] != "failed"
    assert r["location"]["where"] == "here" and r["location"]["path"].endswith(".part")
    assert cat.recordings(status="partial")["total"] == 1
    # a partial recording is finished: retention may apply to it like to any other (policy permitting)
    env.s.settings.update(manual_raw_policy="hours", manual_raw_hours=1)
    env.clock.now = NOW + dt.timedelta(hours=3)
    assert [d["path"] for d in Retention(env.s).apply()] == [r["location"]["path"]]


def test_library_filters_search_and_details(env):
    jid, proc = recorded(env, satdump={"enabled": False}, postprocess={"decode": False})
    cat = Catalog(env.s, proc)
    r = cat.recordings(q="meteor")["recordings"][0]
    for k in ("target", "receiver", "host", "center_frequency_hz", "sample_rate_hz", "start", "end", "duration_s",
              "format", "size_bytes", "status", "outcome"):
        assert r.get(k) is not None, k
    assert r["host"] == "local" and r["end"] > r["start"]
    assert cat.recordings(q=jid)["total"] == 1 and cat.recordings(q="nothing-like-this")["total"] == 0
    assert cat.recordings(host="local")["total"] == 1 and cat.recordings(host="pi")["total"] == 0
    assert cat.recordings(where="here")["total"] == 1 and cat.recordings(where="remote")["total"] == 0
    assert cat.recordings(since="2026-10-12", until="2026-10-13")["total"] == 1
    assert cat.recordings(since="2026-10-14")["total"] == 0
    with pytest.raises(SchedulerError, match="not a date"):
        cat.recordings(since="last tuesday")


# ------------------------------------------------------------------ decoder profiles
def test_profiles_are_validated_versioned_and_matched_by_satellite_then_mode(env):
    store = env.s.profiles
    made = store.seed()
    assert len(made) == 1 and store.seed() == []                         # built-ins once, never again
    meteor = store.get(made[0])
    assert meteor["pipeline"] == "meteor_m2-x_lrpt" and meteor["satellites"] == [57166, 59051] and meteor["modes"] == ["LRPT"]
    bad = store.check({"name": "", "pipeline": "x y", "satellites": ["abc"], "search_offsets_hz": [50_000],
                       "additional_parameters": {"samplerate": 1}, "colour": "red"})
    assert not bad["ok"] and len(bad["errors"]) >= 5
    warn = store.check({"name": "APT", "pipeline": "not_here", "modes": ["apt"]})
    assert warn["ok"] and "not installed" in warn["warnings"][0] and warn["profile"]["modes"] == ["APT"]
    lrpt = store.create({"name": "Any LRPT", "pipeline": "meteor_m2-x_lrpt", "modes": ["LRPT"], "priority": 90,
                         "search_offsets_hz": [0]}, "owner")
    rec = {"spec": {"target": {"norad_id": 57166}}}
    assert store.match(rec)["profile_id"] == meteor["profile_id"]      # a satellite match beats a mode match
    other = {"spec": {"target": {"norad_id": 12345, "mode": "LRPT (QPSK)"}}}
    assert store.match(other)["profile_id"] == lrpt["profile_id"]
    upd = store.update(lrpt["profile_id"], {**lrpt, "enabled": False}, lrpt["revision"], "owner")
    assert upd["revision"] == 2 and store.match(other)["profile_id"] == meteor["profile_id"]   # METEOR's LRPT mode
    with pytest.raises(SchedulerError, match="changed elsewhere"):
        store.update(lrpt["profile_id"], lrpt, 1, "owner")
    assert [h["action"] for h in store.history(lrpt["profile_id"])] == ["updated", "created"]
    store.delete(meteor["profile_id"], meteor["revision"], "owner")
    assert store.match(rec) is None and store.seed() == []              # a deleted built-in is not re-created


def test_decode_requests_use_the_profile_and_record_its_revision(env):
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    p = env.s.profiles.create({"name": "METEOR quick", "pipeline": "meteor_m2-x_lrpt", "satellites": [59051],
                               "search_offsets_hz": [0, 500], "additional_parameters": {"dc_block": True}}, "owner")
    a = runner.proc.submit(jid, "decode", {})
    assert a["resolved"]["search_offsets_hz"] == [0, 500] and a["resolved"]["additional_parameters"] == {"dc_block": True}
    assert a["resolved"]["profile"] == {"profile_id": p["profile_id"], "name": "METEOR quick", "revision": 1}
    b = runner.proc.submit(jid, "decode", {"profile_id": p["profile_id"], "search_offsets_hz": [0]})
    assert b["resolved"]["search_offsets_hz"] == [0]                    # the request's own settings win
    env.s.profiles.update(p["profile_id"], {**p, "pipeline": "not_installed_here"}, p["revision"], "owner")
    with pytest.raises(SchedulerError, match="not installed"):
        runner.proc.submit(jid, "decode", {"profile_id": p["profile_id"]})


def test_rules_decode_with_a_profile_for_a_satellite_starlab_has_no_decoder_for(env):
    from aobs.scheduling.rules import validate_rule
    rule = {"name": "ISS", "satellites": [25544], "receiver_id": "OBS-R02", "decode": True}
    _, errors = validate_rule(rule, env.s)
    assert any("no decoder for NORAD 25544" in e for e in errors)
    env.s.profiles.create({"name": "ISS SSTV", "pipeline": "noaa_apt", "satellites": [25544]}, "owner")
    _, errors = validate_rule(rule, env.s)
    assert not any("no decoder" in e for e in errors)


# ------------------------------------------------------------------ retention
_slot = iter(())


@pytest.fixture(autouse=True)
def _slots():
    global _slot
    _slot = iter(range(0, 10_000, 30))      # every test books its finished recordings from the same start


def finished(env: Any, name: str, source: str = "manual", **kw: Any) -> str:
    """A finished recording with a small raw file (each in its own time slot on OBS-R05)."""
    n = next(_slot)
    job = rtl_job(name, rx="OBS-R05", start=60 + n, end=76 + n, receiver__antenna="V-DIPOLE", satdump={"enabled": False},
                  postprocess={"decode": False}, target={"type": "frequency", "name": name}, **kw)
    jid = submit(env, job)["results"][0]["job_id"]
    raw = pathlib.Path(env.s.get(jid)["paths"]["raw"])
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"\x80" * 4096)
    if source != "manual":
        env.s.update(jid, source=source)
    env.s.transition(jid, "completed", "")
    return jid


def test_nothing_is_deleted_without_a_policy_that_says_so(env):
    m = finished(env, "manual-one")
    a = finished(env, "auto-one", source="auto")
    env.s.settings["auto_retention_hours"] = 1
    env.clock.now = NOW + dt.timedelta(hours=1, minutes=59)
    r = Retention(env.s)
    plan = r.plan()
    assert [x["recording_id"] for x in plan["due"]] == [] and [x["recording_id"] for x in plan["upcoming"]] == [a]
    env.clock.now = NOW + dt.timedelta(hours=3)
    assert policy(env.s.get(m), env.s.settings)["policy"] == "keep"     # manual recordings: keep by default
    env.s.settings["retention_enabled"] = False                          # switched off: shown, never deleted
    plan = r.plan()
    assert [x["recording_id"] for x in plan["waiting"]] == [a] and "switched off" in plan["waiting"][0]["blocked"]
    assert r.apply() == [] and pathlib.Path(env.s.get(a)["paths"]["raw"]).exists()
    env.s.settings["retention_enabled"] = True
    set_keep(env.s, a, True, "owner")                                    # kept by hand wins over every policy
    assert r.apply() == []
    set_keep(env.s, a, False, "owner")
    done = r.apply()
    assert [d["path"] for d in done] == [env.s.get(a)["paths"]["raw"]] and "automatic recordings" in done[0]["basis"]
    assert not pathlib.Path(env.s.get(a)["paths"]["raw"]).exists() and pathlib.Path(env.s.get(m)["paths"]["raw"]).exists()
    job = env.s.get(a)
    assert job["archive"]["deletions"][0]["bytes"] == 4096
    assert Catalog(env.s).recordings(recording_id=a)["recordings"][0]["location"]["where"] == "gone"


def test_retention_waits_for_processing_and_for_an_image_with_after_decode(env):
    from aobs.db.schema import proc_attempts
    jid = finished(env, "after-decode")
    env.s.settings.update(manual_raw_policy="after_decode")
    env.clock.now = NOW + dt.timedelta(days=30)
    r = Retention(env.s)
    assert r.apply() == [] and "waiting for a decode" in r.plan()["waiting"][0]["blocked"]
    now = env.s.clock()

    def attempt(aid: str, status: str, decoded: bool) -> None:
        with env.s.engine.begin() as conn:
            conn.execute(proc_attempts.insert().values(attempt_id=aid, recording_id=jid, kind="decode", params={},
                                                       status=status, origin="api", output_dir="/x", queued_at=now,
                                                       result={"inputs": [{"decoded": decoded, "images": int(decoded)}]}))
    attempt("p1", "failed", False)
    assert r.apply() == []                               # a decode without an image is not enough
    attempt("p2", "completed", True)
    attempt("p3", "queued", False)
    assert r.apply() == [] and "queued or running" in r.plan()["waiting"][0]["blocked"]
    with env.s.engine.begin() as conn:
        conn.execute(proc_attempts.update().where(proc_attempts.c.attempt_id == "p3").values(status="cancelled"))
    assert len(r.apply()) == 1 and not pathlib.Path(env.s.get(jid)["paths"]["raw"]).exists()


def test_imported_and_out_of_root_files_are_never_deleted(env, tmp_path):
    jid = finished(env, "imported")
    other = finished(env, "elsewhere")
    env.s.update(jid, source="import")
    env.s.settings.update(manual_raw_policy="hours", manual_raw_hours=1)
    env.clock.now = NOW + dt.timedelta(days=30)
    assert policy(env.s.get(jid), env.s.settings)["basis"].startswith("imported files")
    env.s.settings["allowed_output_roots"] = [str(tmp_path / "somewhere-else")]
    plan = Retention(env.s).plan()
    assert [x["recording_id"] for x in plan["waiting"]] == [other] and "outside" in plan["waiting"][0]["blocked"]
    assert Retention(env.s).apply() == [] and pathlib.Path(env.s.get(other)["paths"]["raw"]).exists()
    env.s.settings["allowed_output_roots"] = [str(tmp_path)]
    assert [d["path"] for d in Retention(env.s).apply()] == [env.s.get(other)["paths"]["raw"]]
    assert pathlib.Path(env.s.get(jid)["paths"]["raw"]).exists()        # the imported one stays


def test_rule_recordings_follow_their_rule_and_retain_raw_iq_false_deletes_after_the_image(env, tmp_path):
    env.s.settings["satdump_path"] = fake_satdump(tmp_path, good=True)
    rule_job = finished(env, "from-rule", source="auto")
    jid, proc = recorded(env, postprocess={"retain_raw_iq": False, "decode": True})
    job = env.s.get(jid)
    assert job["status"] == "completed" and not pathlib.Path(job["paths"]["raw"]).exists()
    assert "retain raw IQ: no" in job["archive"]["deletions"][0]["basis"]
    sidecar = json.loads(pathlib.Path(job["paths"]["sidecar"]).read_text())
    assert "retain raw IQ: no" in json.dumps(sidecar)
    spec = env.s.get(rule_job)["spec"]
    spec["metadata"].update(rule_name="METEOR", raw_iq_hours=None)
    env.s.update(rule_job, rule_id="r1", spec=spec)
    env.clock.now = NOW + dt.timedelta(days=60)
    assert policy(env.s.get(rule_job), env.s.settings) == {"policy": "keep", "hours": None, "basis": "rule METEOR"}
    assert Retention(env.s).apply() == []


def test_raw_iq_kept_on_a_receiver_host_is_removed_there_by_retention(env):
    jid = finished(env, "remote-one", source="auto")
    raw = pathlib.Path(env.s.get(jid)["paths"]["raw"])
    raw.unlink()                                                    # the file is only on the host
    removed = []
    env.s.backends = types.SimpleNamespace(for_host=lambda h: types.SimpleNamespace(
        remove_recording=lambda job_id: removed.append((h, job_id))))
    rf = {"host": "pi", "path": f".starlab/recordings/{jid}/recording.cu8", "bytes": 4096, "sha256": "ab" * 32}
    env.s.update(jid, archive={"remote": rf})
    proc = inline_processor(env)
    proc.index_remote(jid, rf)
    with pytest.raises(SchedulerError, match="kept on pi"):
        proc.submit(jid, "analyze", {})
    env.s.settings["auto_retention_hours"] = 1
    env.clock.now = NOW + dt.timedelta(hours=3)
    done = Retention(env.s).apply()
    assert removed == [("pi", jid)] and done[0]["host"] == "pi"
    job = env.s.get(jid)
    assert job["archive"]["remote"]["removed_at"] and Catalog(env.s, proc).detail(jid)["recording"]["location"]["where"] == "gone"
    arts = Catalog(env.s, proc).detail(jid)["files"]
    assert any(f["host"] == "pi" and f["deleted_reason"].startswith("retention") for f in arts)


def test_storage_usage_and_retention_plan(env):
    jid, proc = recorded(env, satdump={"enabled": False}, postprocess={"decode": False})
    from aobs.scheduling.archive import storage_usage
    st = storage_usage(env.s)
    assert {f["label"] for f in st["folders"]} >= {"manual", "automatic", "processing"}
    assert st["by_kind"]["raw_iq"]["files"] == 1 and st["raw_iq_by_source"]["manual"]["files"] == 1
    assert st["disks"] and st["disks"][0]["total_bytes"] > 0


# ------------------------------------------------------------------ remote hosts: keep, then transfer on demand
def test_a_host_can_keep_its_recordings_and_transfer_one_on_demand_verified(ex, pi):
    from aobs.scheduling.runner import Runner
    pi.store.update("pi", {"revision": pi.store.get("pi")["revision"],
                           "recording": {"python": __import__("sys").executable, "work_dir": str(pi.work),
                                         "transfer": "keep_on_host"}}, "owner")
    real = RealClock()
    start = real() + dt.timedelta(seconds=14)
    ex.clock.now = real() - dt.timedelta(minutes=10)
    jid = sim_job(ex, start, 4)
    ex.s.clock = real
    proc = inline_processor(ex)
    r = Runner(ex.s, FakeRunnerHw(), hosts=pi.store, processor=proc)
    t = threading.Thread(target=r.execute, args=(ex.s.get(jid),))
    t.start()
    t.join(90)
    job = ex.s.get(jid)
    assert job["status"] == "completed", (job["status_reason"], job.get("execution"))
    assert "kept on pi" in job["status_reason"]
    rf = job["archive"]["remote"]
    remote_file = pathlib.Path(rf["path"])
    assert remote_file.is_file() and rf["bytes"] == remote_file.stat().st_size and len(rf["sha256"]) == 64
    assert not pathlib.Path(job["paths"]["raw"]).exists()             # nothing crossed the network
    cat = Catalog(ex.s, proc)
    row = cat.recordings(recording_id=jid)["recordings"][0]
    assert row["host"] == "pi" and row["location"]["where"] == "remote" and row["outcome"]["recording"] == "complete"
    with pytest.raises(SchedulerError, match="kept on pi"):
        proc.submit(jid, "analyze", {})
    tr = proc.submit(jid, "transfer", {}, "web", "owner")
    an = proc.submit(jid, "analyze", {"analysis": ["integrity"]}, "web", "owner")   # queued behind the transfer
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and proc.attempt(an["attempt_id"])["status"] not in ("completed", "failed"):
        proc.step()
    done = proc.attempt(tr["attempt_id"])
    assert done["status"] == "completed", done["status_reason"]
    assert done["result"]["inputs"][0]["verified"] == "size and SHA-256"
    assert proc.attempt(an["attempt_id"])["status"] == "completed"
    job = ex.s.get(jid)
    local = pathlib.Path(job["paths"]["raw"])
    assert local.is_file() and local.stat().st_size == rf["bytes"]
    assert not remote_file.exists() and job["archive"]["remote"]["removed_at"]
    assert job["archive"]["transfers"][0]["host_copy_removed"] is True
    assert cat.recordings(recording_id=jid)["recordings"][0]["location"]["where"] == "here"
    with pytest.raises(SchedulerError, match="no raw IQ waiting"):
        proc.submit(jid, "transfer", {})


# ------------------------------------------------------------------ schema and API
def test_migration_8_adds_the_archive_and_keeps_recordings(tmp_path):
    from sqlalchemy import text

    from aobs.db.migrate import current_version, migrate
    from aobs.db.schema import SCHEMA_VERSION
    from aobs.db import init_db, make_engine
    e = make_engine(f"sqlite:///{tmp_path / 'db.sqlite3'}")
    init_db(e)
    with e.begin() as conn:                                                 # wind back to schema 7
        conn.execute(text("DROP TABLE decoder_profiles"))
        conn.execute(text("DROP TABLE decoder_profile_versions"))
        conn.execute(text("ALTER TABLE sched_jobs DROP COLUMN archive"))
        conn.execute(text("ALTER TABLE artifacts DROP COLUMN host"))
        conn.execute(text("ALTER TABLE artifacts DROP COLUMN deleted_reason"))
        conn.execute(text("INSERT INTO sched_jobs (job_id, job_name, source, receiver_id, device_serial, status, executes,"
                          " reservation_start, reservation_end, record_start, record_end, spec, created_at, updated_at)"
                          " VALUES ('m1', 'old', 'manual', 'OBS-R02', 'OBS-R02', 'completed', 1, '2026-10-01 00:00:00',"
                          " '2026-10-01 00:10:00', '2026-10-01 00:01:00', '2026-10-01 00:09:00', '{}',"
                          " '2026-10-01 00:00:00', '2026-10-01 00:00:00')"))
        conn.execute(text("INSERT INTO artifacts (artifact_id, recording_id, kind, path, created_at)"
                          " VALUES ('f1', 'm1', 'raw_iq', '/data/x.cu8', '2026-10-01 00:10:00')"))
        conn.execute(text("UPDATE schema_info SET value='7' WHERE key='schema_version'"))
    out = migrate(e)
    assert out["steps"] == ["8: recording archive and decoder profiles"] and current_version(e) == SCHEMA_VERSION
    with e.connect() as conn:
        assert tuple(conn.execute(text("SELECT job_id, archive FROM sched_jobs")).one()) == ("m1", None)
        assert tuple(conn.execute(text("SELECT artifact_id, host, deleted_reason FROM artifacts")).one()) == ("f1", None, None)
        assert conn.execute(text("SELECT COUNT(*) FROM decoder_profiles")).scalar() == 0
    assert migrate(e)["steps"] == []


class Http:
    """A tiny client for a live broker on a free local port (no extra HTTP test dependency)."""

    def __init__(self, app: Any) -> None:
        import socket

        import uvicorn
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        threading.Thread(target=self.server.run, daemon=True).start()
        while not self.server.started:
            time.sleep(0.02)

    def __call__(self, path: str, method: str = "GET", body: Any = None, auth: bool = True) -> tuple[int, Any, dict[str, str]]:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     headers={"Content-Type": "application/json", **({"Authorization": "Bearer t"} if auth else {})},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                ctype = resp.headers.get("content-type", "")
                return resp.status, json.loads(raw) if "json" in ctype else raw, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}"), dict(exc.headers)


def test_archive_api_round_trip(env, tmp_path):
    from aobs.coordination.api import create_app
    from aobs.coordination.broker import Broker
    from aobs.scheduling.planner import Planner
    from aobs.scheduling.service import SchedulerService
    from test_broker import FakeHw
    env.s.settings["satdump_path"] = fake_satdump(tmp_path, good=True)
    env.s.profiles.seed()
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    env.cfg.raw["broker"] = {"receivers": ["OBS-R05"]}
    svc = SchedulerService(env.s, Planner(env.s, predictor=lambda rx, h: []), runner, processor=runner.proc)
    svc.start = lambda: None
    c = Http(create_app(Broker(env.cfg, FakeHw(), env.clock), "t", scheduler=svc))
    try:
        assert c("/v1/archive/recordings", auth=False)[0] == 401
        code, lst, _ = c("/v1/archive/recordings?q=meteor")
        assert code == 200 and lst["total"] == 1 and lst["recordings"][0]["outcome"]["decode"] == "not run"
        prof = c("/v1/archive/profiles")[1]["profiles"][0]
        code, r, _ = c(f"/v1/archive/recordings/{jid}/process", "POST",
                       {"kind": "decode", "params": {"profile_id": prof["profile_id"], "search_offsets_hz": [0]}})
        assert code == 200, r
        drain(runner.proc)
        d = c(f"/v1/archive/recordings/{jid}")[1]
        assert d["outcome"]["level"] == "image" and d["retention"]["policy"] == "keep"
        img = next(f for f in d["files"] if f["viewable"] and f["kind"] == "image")
        code, body, hdr = c(f"/v1/archive/files/{img['artifact_id']}")
        assert code == 200 and body[:4] == b"\x89PNG" and "inline" in hdr["content-disposition"]
        log = next(f for f in d["files"] if f["readable"] and f["kind"] == "log" and "satdump" in (f["label"] or ""))
        assert "SYNCED" in c(f"/v1/archive/files/{log['artifact_id']}/text")[1]["text"]
        raw = next(f for f in d["files"] if f["kind"] == "raw_iq")
        assert c(f"/v1/archive/files/{raw['artifact_id']}")[0] == 413          # raw IQ: signed link only
        assert "/dl/" in c(f"/v1/archive/files/{raw['artifact_id']}/link", "POST", {})[1]["url"]
        assert c(f"/v1/archive/recordings/{jid}/keep", "POST", {"keep": True})[1]["keep"] is True
        st = c("/v1/archive/storage")[1]
        assert st["retention"]["enabled"] is True and "raw_iq" in st["by_kind"]
        code, bad, _ = c("/v1/archive/profiles", "POST", {"profile": {"name": "x"}})
        assert code == 400 and bad["errors"]
        new = c("/v1/archive/profiles", "POST", {"profile": {"name": "APT", "pipeline": "noaa_apt", "modes": ["APT"]}})[1]["profile"]
        code, up, _ = c(f"/v1/archive/profiles/{new['profile_id']}", "PUT", {"profile": {**new, "priority": 70}, "revision": 1})
        assert code == 200 and up["profile"]["priority"] == 70
        assert c(f"/v1/archive/profiles/{new['profile_id']}", "PUT", {"profile": new, "revision": 1})[0] == 409
        assert c(f"/v1/archive/recordings/{jid}/process", "POST", {"kind": "transfer"})[0] == 409
    finally:
        c.server.should_exit = True


def test_signal_needs_averaged_power_not_a_noise_peak():
    from aobs.scheduling.archive import outcome
    job = {"status": "completed", "status_reason": "", "result": {}}

    def analyzed(mean: float, peak: float) -> list[dict[str, Any]]:
        return [{"kind": "analyze", "status": "completed", "attempt_id": "p1",
                 "result": {"inputs": [{"target": {"mean_over_floor_db": mean, "peak_over_floor_db": peak}}]}}]
    noise = outcome(job, analyzed(1.0, 11.0))          # a whole pass of noise peaks ~10 dB up; its average does not
    assert noise["signal"] == "not detected" and "averaged only 1.0 dB" in noise["signal_note"]
    tone = outcome(job, analyzed(7.3, 9.7))
    assert tone["signal"] == "detected" and tone["level"] == "signal" and tone["frames"] is None
    assert outcome(job, [])["signal"] == "unknown"
    failed = outcome({**job, "status": "failed", "status_reason": "rtl_sdr exit 1"}, analyzed(7.3, 9.7))
    assert failed["recording"] == "failed" and failed["level"] == "failed"


def test_a_decode_that_cannot_start_does_not_hide_the_analysis_or_the_recording(env):
    # 250 kS/s cannot hold the METEOR channel 31 kHz off centre: the decode is refused, the rest goes on
    jid, proc = recorded(env, radio__sample_rate_hz=250_000, radio__center_frequency_hz=137_900_000 - 31_250,
                         satdump__sample_rate_hz=250_000)
    job = env.s.get(jid)
    assert job["status"] == "completed" and "decode not started" in job["status_reason"]
    assert [a["kind"] for a in proc.attempts(jid)] == ["analyze"] and proc.attempts(jid)[0]["status"] == "completed"
    o = outcome_of(env, proc, jid)
    assert o["recording"] == "complete" and o["decode"] == "not started" and "half-band" in o["decode_note"]


def test_every_completed_recording_gets_at_least_a_basic_iq_health_check(env, tmp_path):
    import numpy as np

    from aobs.scheduling import iqhealth
    rng = np.random.default_rng(0)
    good = tmp_path / "ok.cs16"
    (rng.normal(0, 800, 2 * 300_000)).astype(np.int16).tofile(good)
    h = iqhealth.basic(good, "cs16")
    assert h["applicable"] and h["basic"] and h["status"] == "healthy" and -40 < h["rms_dbfs"] < -20 and not h["alert"]
    frozen = tmp_path / "zero.cs16"
    np.zeros(2 * 300_000, np.int16).tofile(frozen)
    assert iqhealth.basic(frozen, "cs16")["status"] == "no_data"
    hot = tmp_path / "hot.cf32"
    np.clip(rng.normal(0, 2, 2 * 300_000), -1, 1).astype(np.float32).tofile(hot)
    assert iqhealth.basic(hot, "cf32")["status"] == "possible_overload"
    # a non-RTL recording (RSP1B, cs16) completes with the basic check on record, never failed or alerted by it
    from aobs.scheduling.runner import Runner
    job = rtl_job("rsp", rx="RSP1B", receiver__serial="240526D360", receiver__driver="sdrplay",
                  recording__engine="soapysdr", recording__format="cs16", radio__sample_rate_hz=2_000_000,
                  radio__bandwidth_hz=1_536_000, satdump={"enabled": False}, postprocess={"decode": False},
                  target={"type": "frequency", "name": "x"})
    job["radio"].update(gain_db=None, gain_elements={"IFGR": 40, "RFGR": 3})
    jid = submit(env, job)["results"][0]
    assert jid["ok"], jid["errors"]
    jid = jid["job_id"]
    env.clock.now = NOW + dt.timedelta(minutes=59, seconds=30)
    hw = FakeRunnerHw()

    def spawn(argv, log):
        run = json.loads(pathlib.Path(argv[-1]).read_text())
        raw = pathlib.Path(run["raw"])
        raw.parent.mkdir(parents=True, exist_ok=True)
        np.zeros(2 * 200_000, np.int16).tofile(raw)                    # a receiver that delivered only zeros
        pathlib.Path(run["result"]).write_text(json.dumps({"status": "recorded", "bytes": raw.stat().st_size,
                                                            "actual_start": run["record_start"]}))
        from test_scheduler import FakeHandle
        return FakeHandle(4242)
    hw.spawn = spawn
    proc = inline_processor(env)
    Runner(env.s, hw, sleep=lambda s: None, processor=proc).tick(threaded=False)
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(proc)
    done = env.s.get(jid)
    iq = done["result"]["capture"]["iq_health"]
    assert iq["basic"] and iq["status"] == "no_data" and done["status"] == "completed"
    assert not [n for n in env.s.notifications() if n["kind"] == "health"]
    assert any(a["label"] == "IQ health (basic check)" for a in proc.artifacts(jid))
