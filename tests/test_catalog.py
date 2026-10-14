from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from aobs.scheduling.catalog import Catalog
from aobs.scheduling.store import SchedulerError
from test_scheduler import (NOW, FakeRunnerHw, drain, env, planner, rtl_job, run_job, submit,  # noqa: F401
                            window)


def recorded(env, **kw):
    jid, runner = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}, **kw))
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    return jid, runner.proc, Catalog(env.s, runner.proc)


def test_recordings_list_filters_and_pagination(env):
    jid, proc, cat = recorded(env)
    res = cat.recordings()
    assert res["total"] == 1 and res["recordings"][0]["recording_id"] == jid
    r = res["recordings"][0]
    assert r["decode"] == "none" and r["analysis"] == "done" and r["artifacts"].get("waterfall", 0) == 0
    assert cat.recordings(receiver="OBS-R01")["total"] == 0
    assert cat.recordings(satellite="meteor-m2-4")["total"] == 1
    assert cat.recordings(frequency_hz=137_900_000)["total"] == 1
    assert cat.recordings(frequency_hz=145_800_000)["total"] == 0
    assert cat.recordings(decoded=True)["total"] == 0 and cat.recordings(decoded=False)["total"] == 1
    assert cat.recordings(date="2026-10-13")["total"] == 1 and cat.recordings(date="2026-10-12")["total"] == 0
    assert cat.recordings(per_page=1, page=5)["page"] == 1


def test_results_lists_attempts_artifacts_and_latest_success(env):
    jid, proc, cat = recorded(env)
    a = proc.submit(jid, "analyze", {"analysis": ["waterfall"], "fft_size": 1024}, "slack")
    drain(proc)
    res = cat.results(jid)
    assert [x["attempt_id"] for x in res["attempts"]][-1] == a["attempt_id"]
    assert res["latest_successful"]["analyze"] == a["attempt_id"]
    assert res["checksums_sha256"] and len(res["checksums_sha256"][0]) == 64
    assert {x["kind"] for x in cat.results(jid, "waterfall")["artifacts"]} == {"waterfall"}
    with pytest.raises(SchedulerError, match="type must be"):
        cat.results(jid, "bogus")


def test_cancel_by_state_is_safe_and_idempotent(env):
    cat = Catalog(env.s)
    jid = submit(env, rtl_job("to-cancel"))["results"][0]["job_id"]
    assert cat.cancel(jid)["status"] == "cancelled"
    assert cat.cancel(jid)["already"] is True
    jid2 = submit(env, rtl_job("running", rx="OBS-R05", receiver__antenna="V-DIPOLE", satdump={"enabled": False},
                               postprocess={"decode": False}, target={"type": "frequency", "name": "x"}))["results"][0]["job_id"]
    env.s.transition(jid2, "recording", "", pid=999999)
    first = cat.cancel(jid2)
    assert first["needs_confirmation"] and "partial" in first["warning"]
    assert env.s.get(jid2)["status"] == "recording"                  # nothing happened without the code
    second = cat.cancel(jid2, confirm=first["confirm"])
    assert second["status"] == "terminating"


def test_preparing_job_is_cancelled_before_the_capture_starts(env):
    from aobs.scheduling.runner import Runner
    jid = submit(env, rtl_job())["results"][0]["job_id"]
    env.clock.now = NOW + dt.timedelta(minutes=59, seconds=30)
    hw = FakeRunnerHw()

    def sleep(_):
        env.s.transition(jid, "cancelled", "user", expect=("preparing",))     # cancelled while waiting for start
    Runner(env.s, hw, sleep=sleep).tick(threaded=False)
    assert env.s.get(jid)["status"] == "cancelled"
    assert ("spawn", "OBS-R02") not in hw.calls and hw.calls[-1] == ("release", "OBS-R02")


def test_terminated_capture_becomes_partial_and_is_processable(env):
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
    from test_scheduler import inline_processor
    proc = inline_processor(env)
    Runner(env.s, hw, sleep=lambda s: None, processor=proc).tick(threaded=False)
    job = env.s.get(jid)
    assert job["status"] == "partial" and "kept" in job["status_reason"]
    env.clock.now = NOW + dt.timedelta(minutes=80)
    a = proc.submit(jid, "analyze", {"analysis": ["integrity"]})
    drain(proc)
    assert proc.attempt(a["attempt_id"])["status"] == "completed"


def test_batch_cancel_previews_then_cancels_exactly_those(env):
    cat = Catalog(env.s)
    ids = [submit(env, rtl_job(f"b-{i}", start=60 + 20 * i, end=70 + 20 * i))["results"][0]["job_id"] for i in range(3)]
    other = submit(env, rtl_job("keep", rx="OBS-R05", receiver__antenna="V-DIPOLE", satdump={"enabled": False},
                                postprocess={"decode": False}, target={"type": "frequency", "name": "x"}))["results"][0]["job_id"]
    prev = cat.cancel_batch(receiver="OBS-R02")
    assert prev["needs_confirmation"] and prev["matched"] == 3
    assert {p["job_id"] for p in prev["preview"]} == set(ids)
    assert cat.cancel_batch(receiver="OBS-R02", confirm="wrong")["needs_confirmation"]
    done = cat.cancel_batch(receiver="OBS-R02", confirm=prev["confirm"])
    assert sorted(done["cancelled"]) == sorted(ids)
    assert env.s.get(other)["status"] == "scheduled"


def test_clone_copies_settings_to_a_new_window(env):
    cat = Catalog(env.s)
    src = submit(env, rtl_job("orig"))["results"][0]["job_id"]
    res = cat.clone(src, (NOW + dt.timedelta(hours=3)).isoformat(), duration_seconds=600, dry_run=True)
    entry = res["results"][0]
    assert entry["ok"] and not entry["stored"]
    assert entry["job"]["radio"]["gain_db"] == 33.8 and entry["job"]["metadata"]["cloned_from"] == src
    booked = cat.clone(src, (NOW + dt.timedelta(hours=3)).isoformat(), duration_seconds=600, dry_run=False)
    assert booked["stored"] == 1
    clash = cat.clone(src, (NOW + dt.timedelta(minutes=65)).isoformat(), duration_seconds=600, dry_run=True)
    assert "conflict" in " ".join(clash["results"][0]["errors"])


def test_import_is_dry_run_first_and_never_guesses(env, tmp_path):
    root = tmp_path / "imports"
    (root / "meteor").mkdir(parents=True)
    named = root / "meteor" / "rsp1b_1791882300_137500000_2000000.cs16"
    named.write_bytes(b"\0" * 4000)
    bare = root / "meteor" / "mystery.cu8"
    bare.write_bytes(b"\0" * 2000)
    side = root / "OBS-R02_20261013T073119.844382Z.cu8"
    side.write_bytes(b"\0" * 4800)
    side.with_suffix(".json").write_text(json.dumps({"receiver": "OBS-R02", "frequency_hz": 137500000, "sample_rate": 2400000,
                                                     "started_at": "2026-10-13T07:31:19+00:00",
                                                     "pass": {"satellites": [{"name": "METEOR M2-4"}]}}))
    import os
    old = (NOW - dt.timedelta(hours=2)).timestamp()
    for f in (named, bare, side):
        os.utime(f, (old, old))
    env.s.settings["import_roots"] = [str(root)]
    from test_scheduler import inline_processor
    cat = Catalog(env.s, inline_processor(env))
    with pytest.raises(SchedulerError, match="not under a configured import root"):
        cat.import_recordings(["/etc"])
    dry = cat.import_recordings()
    assert dry["dry_run"] and dry["found"] == 3 and env.s.jobs(source="import") == []
    by = {pathlib.Path(f["path"]).name: f for f in dry["files"]}
    assert by[named.name]["metadata"]["sample_rate_hz"] == 2_000_000 and by[named.name]["uncertain"] == []
    assert set(by[bare.name]["uncertain"]) >= {"center_frequency_hz", "sample_rate_hz"}
    assert by[side.name]["metadata"]["target"] == "METEOR M2-4"
    done = cat.import_recordings(dry_run=False)
    assert len(done["created"]) == 3
    assert cat.import_recordings(dry_run=False)["found"] == 0               # no duplicates
    mystery = next(j for j in env.s.jobs(source="import", limit=10) if j["job_name"] == "mystery.cu8")
    with pytest.raises(SchedulerError, match="center frequency unknown"):
        cat.p.submit(mystery["job_id"], "analyze", {})
    cat.correct_metadata(mystery["job_id"], {"center_frequency_hz": 137_500_000, "sample_rate_hz": 1_000_000}, "owner")
    fixed = env.s.get(mystery["job_id"])
    assert fixed["spec"]["import"]["corrections"][0]["by"] == "owner"
    assert set(fixed["spec"]["import"]["uncertain"]) == {"start", "receiver"}
    with pytest.raises(SchedulerError, match="broker recordings are exact"):
        jid = submit(env, rtl_job())["results"][0]["job_id"]
        cat.correct_metadata(jid, {"sample_rate_hz": 1}, "x")


def test_download_links_are_signed_and_expire(env):
    jid, proc, cat = recorded(env)
    art = proc.artifacts(jid, kind="raw_iq")[0]
    link = cat.link(art["artifact_id"], 600, "http://100.64.49.37:8751/")
    _, _, aid, exp, sig = link["url"].rsplit("/", 4)
    assert cat.resolve_link(aid, int(exp), sig).is_file()             # raw IQ allowed through a signed link
    with pytest.raises(SchedulerError, match="invalid"):
        cat.resolve_link(aid, int(exp), "0" * 32)
    with pytest.raises(SchedulerError, match="invalid"):
        cat.resolve_link(aid, int(exp) + 3600, sig)                  # extending the expiry breaks the signature
    env.clock.now += dt.timedelta(minutes=11)
    with pytest.raises(SchedulerError, match="expired"):
        cat.resolve_link(aid, int(exp), sig)


def test_jobs_view_and_detail(env):
    planner(env, [window(60, 75)]).plan()
    jid = submit(env, rtl_job("detail", start=70, end=80))["results"][0]["job_id"]
    cat = Catalog(env.s)
    rows = cat.jobs()
    auto = next(r for r in rows if r["source"] == "auto")
    assert auto["status"] == "preempted" and auto["conflict"].startswith("preempted by")
    assert next(r for r in rows if r["job_id"] == jid)["priority"].startswith("manual")
    d = cat.job_detail(jid)
    assert d["calendar"]["state"] == "not published" and d["spec"]["radio"]["gain_db"] == 33.8


def test_artifact_text_is_read_only_scoped_and_bounded(env):
    jid, proc, cat = recorded(env)
    logs = [a for a in proc.artifacts(jid) if a["kind"] in ("diagnostics",) and a["path"].endswith("result.json")]
    res = proc.artifact_text(logs[0]["artifact_id"], tail_lines=5, contains="")
    assert res["returned_lines"] <= 5 and res["file"] == "result.json"
    filt = proc.artifact_text(logs[0]["artifact_id"], 2000, contains="sha256")
    assert filt["matching_lines"] >= 1 and all("sha256" in ln.lower() for ln in filt["text"].splitlines())
    raw = proc.artifacts(jid, kind="raw_iq")[0]
    with pytest.raises(SchedulerError, match="not a text artifact"):
        proc.artifact_text(raw["artifact_id"])
    png = proc.artifacts(jid, kind="spectrum") or proc.artifacts(jid, kind="diagnostics")
    pngs = [a for a in png if a["path"].endswith(".png")]
    with pytest.raises(SchedulerError, match="not a text artifact"):
        proc.artifact_text(pngs[0]["artifact_id"])
    with pytest.raises(SchedulerError, match="not an artifact id"):
        proc.artifact_text("../../etc/passwd")
    with pytest.raises(SchedulerError, match="no artifact f20990101000000abcdef in the index"):
        proc.artifact_text("f20990101000000abcdef")
