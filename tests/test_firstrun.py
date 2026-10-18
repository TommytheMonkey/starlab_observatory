"""Fresh installs: config defaults, built-in predictor, simulation mode, setup wizard, pass booking."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any

import pytest

from aobs.config import ConfigError, load_config
from aobs.satellites import predict
from aobs.satellites.passes import PassService
from aobs.scheduling.store import SchedulerError
from aobs.settings.setup import Setup
from aobs.settings.store import SettingsError
from test_inventory import SoapyHw, fake_discover
from test_scheduler import NOW, env  # noqa: F401
from test_testlab import lab, run_test  # noqa: F401


# ------------------------------------------------------------------ bootstrap configuration
def test_minimal_config_gets_safe_defaults(tmp_path):
    cfg_file = tmp_path / "starlab.toml"
    cfg_file.write_text(f'[paths]\ndata_root = "{tmp_path}/data"\n')
    cfg = load_config(cfg_file)
    assert not cfg.legacy_enabled and cfg.display_tz == "America/New_York"
    assert cfg.database_url == f"sqlite:///{tmp_path}/data/db/starlab.sqlite3"
    b = cfg.section("broker")
    assert b["host"] == "127.0.0.1" and b["receivers"] == [] and b["token_file"].endswith("/secrets/api_token")
    s = cfg.section("scheduler")
    assert s["allowed_output_roots"] == [f"{tmp_path}/data"] and s["receivers"] == {}
    assert cfg.section("legacy").get("adsb_serial", "") == ""        # no station's ADS-B dongle assumed


def test_existing_station_config_keeps_its_legacy_integration(tmp_path):
    f = tmp_path / "observatory.toml"
    f.write_text(f'[paths]\ndata_root = "{tmp_path}"\n[legacy]\nstation_config = "/etc/x.json"\n')
    assert load_config(f).legacy_enabled


def test_missing_or_broken_config_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setattr("aobs.config.DEFAULT_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(ConfigError, match="Run the installer"):
        load_config()
    bad = tmp_path / "bad.toml"
    bad.write_text("[paths\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(bad)


# ------------------------------------------------------------------ predictor
def test_tle_parsing_rejects_bad_checksums():
    good = predict.SAMPLE_TLE.read_text()
    assert set(predict.parse_tle(good, "x")) == {57166, 59051, 25544}
    lines = good.splitlines()
    broken = "\n".join([lines[0], lines[1][:68] + str((int(lines[1][68]) + 1) % 10), lines[2]])
    assert predict.parse_tle(broken, "x") == {}


def test_predictor_finds_meteor_passes_offline():
    el = predict.load_elements(None)
    start = dt.datetime(2026, 10, 15, 12, tzinfo=dt.timezone.utc)
    passes = predict.predict_passes(el, [57166, 59051], 51.4779, -0.0015, 46, start, 24, 10)
    assert len(passes) >= 6 and all(400 < p["duration_s"] < 1100 for p in passes)
    assert all(p["aos"] < p["max_at"] < p["los"] and p["max_elevation_deg"] >= 10 for p in passes)
    assert passes == sorted(passes, key=lambda p: p["aos"])


def test_tle_refresh_merges_and_reports_failures(tmp_path):
    sample = predict.SAMPLE_TLE.read_text().splitlines()
    def fetch(url: str) -> str:
        if "25544" in url:
            raise OSError("network down")
        return "\n".join(sample[0:3]) + "\n"
    res = predict.refresh_tles([57166, 25544], tmp_path, fetch)
    assert res["updated"] == [57166] and "network down" in res["failed"][25544]
    assert predict.load_elements(tmp_path)[57166].source == "downloaded from celestrak.org"
    assert predict.load_elements(tmp_path)[57166].fetched_at is not None
    assert predict.load_elements(tmp_path)[25544].source == "sample elements shipped with Starlab"


# ------------------------------------------------------------------ simulation
@pytest.fixture
def sim(env, lab):
    from aobs.inventory.service import InventoryService
    st = env.s.config_store
    st.save("simulation", {"enabled": True}, None, "o")
    svc = InventoryService(env.s, discover=fake_discover(), run_probe=lambda *a: pytest.fail("no probe"),
                           sysfs=env.tmp / "empty-sysfs")
    (env.tmp / "empty-sysfs").mkdir()
    return svc


def test_simulated_devices_appear_only_in_simulation_mode(env, lab, sim):
    found = {d["usb_path"]: d for d in sim.overview()["discovered"]}
    assert set(found) == {"sim-1", "sim-2"} and all(d["simulated"] for d in found.values())
    env.s.config_store.save("simulation", {"enabled": False}, None, "o")
    assert sim.overview()["discovered"] == []


def test_a_simulated_test_never_makes_a_receiver_verified(env, lab, sim):
    sim.add({"usb_path": "sim-1", "id": "SIM-A"}, "o")
    rx = env.s.receivers["SIM-A"]
    assert rx.simulated and rx.schedulable and rx.readiness == "untested"
    lab.runner.hw = SoapyHw(good=True)                         # the fake writes noise like the simulator does
    run_test(env, lab, {**sim.verify_body(lab.options("SIM-A")), "receiver_id": "SIM-A"})
    row = env.s.inventory.public("SIM-A")
    assert row["readiness"] == "simulated" and row["state"] == "simulated"
    assert "no hardware was tested" in row["readiness_detail"]["note"]
    job = env.s.get(row["readiness_detail"]["job_id"])
    assert job["spec"]["metadata"]["simulated"] is True


def test_simulated_jobs_are_labelled_on_the_calendar(env, lab, sim):
    from aobs.scheduling.calendar import render
    sim.add({"usb_path": "sim-1", "id": "SIM-A"}, "o")
    st = env.s.config_store
    st.save("observatory", {**st.current("observatory")["data"], "latitude": 51.4779, "longitude": -0.0015,
                            "elevation_m": 46}, None, "o")
    env.clock.now = dt.datetime(2026, 10, 15, 12, tzinfo=dt.timezone.utc)
    ps = PassService(env.s)
    p = ps.predicted(48)["passes"][0]
    res = ps.schedule(p["norad"], p["aos"], "SIM-A", "o")
    assert res["ok"] and res["simulated"]
    body = render(env.s.get(res["job_id"]), "UTC")
    assert body["summary"].startswith("[SIMULATED]") and body["description"].startswith("SIMULATED:")
    assert ps.predicted(48)["passes"][0]["booked"][0]["receiver_id"] == "SIM-A"


def test_booking_a_pass_on_an_unverified_real_receiver_is_refused(env, lab):
    from test_inventory import add_airspy, inv  # noqa: F401
    st = env.s.config_store
    st.save("observatory", {**st.current("observatory")["data"], "latitude": 51.4779, "longitude": -0.0015}, None, "o")
    env.clock.now = dt.datetime(2026, 10, 15, 12, tzinfo=dt.timezone.utc)
    ps = PassService(env.s)
    p = ps.predicted(48)["passes"][0]
    with pytest.raises(SchedulerError, match="no receiver"):
        ps.schedule(p["norad"], p["aos"], "NOPE", "o")


def test_passes_need_a_location(env):
    out = PassService(env.s).predicted(24)
    assert out["location_set"] is False and out["passes"] == [] and "latitude" in out["warning"]
    with pytest.raises(SchedulerError, match="downloading orbital elements is off"):
        PassService(env.s).refresh()


# ------------------------------------------------------------------ setup wizard
def test_existing_station_is_never_sent_into_the_wizard(env):
    assert env.s.config_store.current("setup")["data"]["completed"] is True      # TOML receivers = existing station


def test_wizard_progress_persists_and_required_steps_cannot_be_skipped(env):
    st = env.s.config_store
    st.save("setup", {"completed": False, "step": "welcome", "done": [], "skipped": []}, None, "o")
    w = Setup(env.s)
    with pytest.raises(SettingsError, match="cannot be skipped"):
        w.step("identity", "skip", "o")
    w.step("welcome", "done", "o")
    st.save("observatory", {**st.current("observatory")["data"], "name": ""}, None, "o")
    with pytest.raises(SettingsError, match="give the observatory a name"):
        w.step("identity", "done", "o")
    st.save("observatory", {**st.current("observatory")["data"], "name": "Test"}, None, "o")
    w.step("identity", "done", "o")
    s2 = w.step("hosts", "skip", "o")
    assert s2["step"] == "discovery"
    states = {x["id"]: x["state"] for x in Setup(env.s).state()["steps"]}       # a new object: read back from the DB
    assert states["welcome"] == "done" and states["hosts"] == "skipped" and states["discovery"] == "current"


def test_validation_and_finish(env):
    w = Setup(env.s)
    res = w.validate()
    names = {c["check"]: c for c in res["checks"]}
    assert names["Database"]["level"] == "pass" and names["Location"]["level"] == "warn"
    assert "OBS-R01, OBS-R02" in names["Automatic recording"]["detail"]          # this test station has auto rules
    assert w.finish("o")["completed"] is True


def test_web_link_signs_in(env, tmp_path):
    from aobs.web.auth import login_url, sign
    url = login_url(env.cfg, "http://example:8751")
    exp, sig = url.rsplit("/", 2)[-2:]
    assert sig == sign(env.cfg, "login", int(exp)) and (env.cfg.data_root / "secrets" / "rf_ui_key").stat().st_mode & 0o077 == 0
