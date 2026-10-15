"""Persistent configuration: seeding, restart survival, validation, history, export/import, backups."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
from typing import Any

import pytest
from sqlalchemy import text

from aobs.db import init_db, make_engine
from aobs.db import migrate as m
from aobs.db.schema import SCHEMA_VERSION
from aobs.scheduling.store import Scheduler
from aobs.settings.store import SettingsError
from aobs.settings.transfer import export, import_config
from test_scheduler import env  # noqa: F401
from test_rflayout import http, rf  # noqa: F401


def restart(env: Any) -> Scheduler:
    """A fresh broker on the same database and the same observatory.toml."""
    return Scheduler(env.cfg, make_engine(env.cfg.database_url), env.clock, present=lambda rx: True,
                     satdump_pipelines=frozenset())


def test_settings_are_seeded_once_from_the_toml(env):
    st = env.s.config_store
    obs = st.current("observatory")
    assert obs["version"] == 1 and obs["author"] == "seed (observatory.toml)"
    assert obs["data"]["timezone"] == "America/New_York" and obs["data"]["latitude"] is None
    assert st.current("storage")["data"]["reserve_free_gib"] == env.cfg.raw["scheduler"]["reserve_free_gib"]
    assert st.current("notifications")["data"] == {"disabled_kinds": []}


def test_saved_settings_survive_a_restart_and_are_not_overwritten_by_defaults(env):
    st = env.s.config_store
    res = st.save("observatory", {**st.current("observatory")["data"], "name": "Starlab North", "latitude": 12.5,
                                  "longitude": -45.25, "elevation_m": 210}, 1, "owner (web)", config=env.cfg)
    assert res["version"] == 2 and "name: 'Observatory' → 'Starlab North'" not in res["changes"]
    st.save("storage", {**st.current("storage")["data"], "reserve_free_gib": 80}, 1, "owner (web)", config=env.cfg)
    env.cfg.raw["scheduler"]["reserve_free_gib"] = 64            # the file still says 64
    s2 = restart(env)
    obs = s2.config_store.current("observatory")["data"]
    assert obs["name"] == "Starlab North" and obs["latitude"] == 12.5 and obs["elevation_m"] == 210
    assert s2.settings["reserve_free_gib"] == 80                 # the database wins over the file
    assert s2.config_store.current("observatory")["version"] == 2  # no re-seed on restart
    differs = s2.config_store.describe()["storage"]["toml_differs"]
    assert differs == [{"key": "reserve_free_gib", "toml": "scheduler.reserve_free_gib", "file": 64, "database": 80}]


def test_invalid_settings_are_refused_and_leave_the_current_version_alone(env):
    st = env.s.config_store
    before = st.current("observatory")
    for bad, msg in ((({"timezone": "Mars/Olympus"}), "not a valid tz"), ({"latitude": 91}, "between -90 and 90"),
                     ({"surprise": 1}, "unknown setting"), ({"elevation_m": "high"}, "not a valid float")):
        with pytest.raises(SettingsError, match=msg):
            st.save("observatory", {**before["data"], **bad}, before["version"], "owner (web)", config=env.cfg)
    with pytest.raises(SettingsError, match="allowed output roots"):
        st.save("storage", {**st.current("storage")["data"], "default_output_root": "/etc/evil"}, None, "owner (web)")
    with pytest.raises(SettingsError, match="whole number"):
        st.save("storage", {**st.current("storage")["data"], "reserve_free_gib": 1.5}, None, "owner (web)")
    assert st.current("observatory") == before and env.cfg.display_tz == "America/New_York"
    assert st.save("observatory", {**before["data"], "latitude": 91}, None, "x", dry_run=True)["ok"] is False


def test_stale_edits_are_refused(env):
    st = env.s.config_store
    data = st.current("ui")["data"]
    st.save("ui", {**data, "default_test_duration_s": 30}, 1, "tab A")
    with pytest.raises(SettingsError, match="changed since you loaded it") as e:
        st.save("ui", {**data, "default_test_duration_s": 120}, 1, "tab B")
    assert e.value.status == 409


def test_live_settings_apply_without_restart(env):
    st = env.s.config_store
    st.save("notifications", {"disabled_kinds": ["health", "scheduled"]}, None, "owner (web)", config=env.cfg)
    assert env.s.settings["notifications_disabled"] == ["health", "scheduled"]
    res = st.save("integrations", {**st.current("integrations")["data"], "calendar_enabled": True}, None, "owner (web)",
                  config=env.cfg)
    assert res["needs_restart"] == ["Google Calendar"]
    st.save("observatory", {**st.current("observatory")["data"], "timezone": "Europe/Berlin"}, None, "o", config=env.cfg)
    assert env.cfg.display_tz == "Europe/Berlin"


def test_history_and_restore(env):
    st = env.s.config_store
    d = st.current("ui")["data"]
    st.save("ui", {**d, "default_test_duration_s": 30}, None, "owner (web)", "shorter")
    st.save("ui", {**d, "default_test_duration_s": 120}, None, "owner (web)", "longer")
    hist = st.history("ui")
    assert [h["version"] for h in hist] == [3, 2, 1] and hist[0]["changes"] == ["default_test_duration_s: 30 → 120"]
    res = st.restore("ui", 2, "owner (web)")
    assert res["version"] == 4 and st.current("ui")["data"]["default_test_duration_s"] == 30
    assert st.current("ui")["note"] == "restored version 2"


def test_export_excludes_secrets_and_import_round_trips(env):
    from aobs.scheduling import rflayout
    env.s.rf = rflayout.RfLayout(env.s)
    env.s.rf.load()
    st = env.s.config_store
    st.save("observatory", {**st.current("observatory")["data"], "name": "Starlab North"}, None, "o", config=env.cfg)
    out = export(env.s)
    blob = json.dumps(out)
    assert out["starlab_export"] == 1 and out["settings"]["observatory"]["data"]["name"] == "Starlab North"
    assert out["signal_paths"]["layout"]["receivers"] and "OBS-R02" in blob
    assert not any(w in blob.lower() for w in ("token", "password", "client_secret", "service_account"))
    assert any("observatory.toml" in n for n in out["not_included"])
    # change things, then import the export back
    st.save("observatory", {**st.current("observatory")["data"], "name": "Elsewhere"}, None, "o", config=env.cfg)
    planned = import_config(env.s, out, "owner (web)")
    assert planned["dry_run"] and planned["ok"] and planned["settings"]["observatory"]["changes"] == [
        "name: 'Elsewhere' → 'Starlab North'"]
    assert st.current("observatory")["data"]["name"] == "Elsewhere"            # a dry run writes nothing
    done = import_config(env.s, out, "owner (web)", dry_run=False)
    assert not done["dry_run"] and st.current("observatory")["data"]["name"] == "Starlab North"
    assert done["applied"] == [f"Observatory → version {st.current('observatory')['version']}"]


def test_a_bad_import_writes_nothing(env):
    out = export(env.s)
    out["settings"]["storage"]["data"]["reserve_free_gib"] = -5
    out["settings"]["observatory"]["data"]["name"] = "Changed"
    out["settings"]["observatory"]["data"]["api_token"] = "should never be imported"
    before = {d: env.s.config_store.current(d)["version"] for d in ("storage", "observatory")}
    res = import_config(env.s, out, "owner (web)", dry_run=False)
    assert not res["ok"] and res["dry_run"] and any("between 0" in e for e in res["errors"])
    assert {d: env.s.config_store.current(d)["version"] for d in before} == before
    with pytest.raises(SettingsError, match="not a Starlab configuration export"):
        import_config(env.s, {"hello": 1}, "x")
    newer = {**export(env.s), "schema_version": SCHEMA_VERSION + 1}
    with pytest.raises(SettingsError, match="newer Starlab"):
        import_config(env.s, newer, "x")


def test_migration_from_schema_1_keeps_every_record(env, tmp_path):
    from aobs.db.schema import rf_layouts, sched_jobs
    from test_scheduler import fix_dirs, rtl_job
    env.s.submit([fix_dirs(rtl_job(), env.tmp)])
    with env.s.engine.begin() as conn:
        jobs_before = conn.execute(text("select count(*) from sched_jobs")).scalar()
        conn.execute(text("DROP TABLE settings_docs"))
        conn.execute(text("DROP TABLE rx_sessions"))
        conn.execute(text("DROP TABLE inv_events"))
        conn.execute(text("DROP TABLE inv_receivers"))
        conn.execute(text("UPDATE schema_info SET value='1' WHERE key='schema_version'"))
    assert jobs_before == 1
    out = init_db(env.s.engine)
    assert out["from"] == 1 and len(out["steps"]) == SCHEMA_VERSION - 1 and pathlib.Path(out["backup"]).exists()
    with env.s.engine.connect() as conn:
        assert conn.execute(text("select count(*) from sched_jobs")).scalar() == jobs_before
    s2 = restart(env)
    assert set(s2.receivers) == {"OBS-R01", "OBS-R02", "OBS-R05", "RSP1B"}
    assert s2.config_store.current("observatory")["version"] == 1


def test_backup_and_restore_round_trip(env, tmp_path):
    st = env.s.config_store
    saved = m.backup(env.s.engine, "manual")
    st.save("ui", {"default_test_duration_s": 30}, None, "owner (web)")
    assert [b["file"] for b in m.list_backups(env.s.engine)] == [saved.name]
    res = m.restore(env.s.engine, saved)
    assert pathlib.Path(res["safety_backup"]).exists() and res["schema_version"] == SCHEMA_VERSION
    assert restart(env).config_store.current("ui")["data"]["default_test_duration_s"] == 60
    with pytest.raises(RuntimeError, match="not a Starlab database"):
        bogus = tmp_path / "x.sqlite3"
        sqlite3.connect(bogus).execute("create table t (a)").connection.close()
        m.restore(env.s.engine, bogus)


def test_configuration_api(http, env):
    auth = {"Authorization": "Bearer t"}
    code, cfg, _ = http("/v1/config", headers=auth)
    assert code == 200 and set(cfg["documents"]) == {"observatory", "storage", "notifications", "integrations", "simulation",
                                                   "satellites", "scheduling", "ui"}
    assert cfg["managed"]["scheduling"]["edit"] == "observatory.toml" and cfg["bootstrap"]["schema_version"] == SCHEMA_VERSION
    doc = cfg["documents"]["ui"]["current"]
    code, res, _ = http("/v1/config/ui", "PUT", {"data": {"default_test_duration_s": 9999}, "base_version": doc["version"]},
                        auth)
    assert code == 400 and "between 5 and 600" in res["error"]
    code, res, _ = http("/v1/config/ui", "PUT", {"data": {"default_test_duration_s": 30}, "base_version": doc["version"]},
                        auth)
    assert code == 200 and res["version"] == doc["version"] + 1
    code, _, headers = http("/v1/config/export", headers=auth)
    assert code == 200 and "attachment" in headers["content-disposition"]
    assert http("/v1/config/nope", headers=auth)[0] == 404
    code, hist, _ = http("/v1/config/ui/history", headers=auth)
    assert [v["version"] for v in hist["versions"]][:2] == [doc["version"] + 1, doc["version"]]
