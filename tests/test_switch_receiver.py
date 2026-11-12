"""ANTSW-01 in front of a receiver added in the web UI (2026-11-12: R05 is inventory receiver OBS-R08 with serial
OBS-R05; the switch feeds it again, Eggbeater on port 1 and discone on port 2)."""
from __future__ import annotations

import copy
import pathlib

import pytest

from aobs.coordination.hardware import LiveHardware
from aobs.scheduling import rflayout
from aobs.scheduling.store import SchedulerError
from test_rflayout import rf  # noqa: F401
from test_scheduler import env  # noqa: F401


def web_receiver_behind_switch(env, name: str) -> None:
    """observatory.toml no longer puts the switch on OBS-R05; a web-added dongle (ID OBS-R08, serial OBS-R55) gets it."""
    env.cfg.raw["scheduler"]["receivers"]["OBS-R05"]["antenna"] = "DISCONE"
    env.cfg.raw["antenna_switch"]["receiver"] = name
    env.s.inventory.create(rid="OBS-R08", name="OBS-R05", driver="rtlsdr", serial="OBS-R55", identity="serial",
                           usb_path="5-3", usb_vid="0bda", usb_pid="2838", manufacturer="Nooelec", model="NESDR SMArt v5",
                           config={}, capabilities=None, actor="owner (web)")
    env.s.inventory.record_readiness("OBS-R08", True, {"test_id": "t1", "job_id": "m0"}, "test t1")
    env.present.add("OBS-R55")
    env.s.reload_receivers()


def two_antennas(lay: dict) -> dict:
    lay = copy.deepcopy(lay)
    c = lay["components"]
    for cid, name in (("EGGBEATER-2M", "M2 Eggbeater 135-150"), ("DISCONE-TRAM", "Tram discone")):
        c[cid] = {"id": cid, "name": name, "kind": "antenna", "status": "working", "power": "none", "model": "",
                  "notes": "", "gain_db": None, "min_mhz": None, "max_mhz": None}
    c["ANTSW-01"]["ports"] = {str(n): None for n in range(1, 9)}
    c["ANTSW-01"]["ports"].update({"1": "EGGBEATER-2M", "2": "DISCONE-TRAM"})
    lay["receivers"]["OBS-R05"]["chain"] = []
    lay["receivers"]["OBS-R08"] = {"id": "OBS-R08", "chain": ["ANTSW-01"], "notes": ""}
    return lay


@pytest.mark.parametrize("name", ["OBS-R55", "OBS-R08"])
def test_the_setting_names_the_receiver_by_serial_or_id(env, name):
    web_receiver_behind_switch(env, name)
    assert rflayout.switch_receiver(env.s) == "OBS-R08"


def test_a_toml_receiver_on_the_switch_still_wins_and_none_means_none(env):
    assert rflayout.switch_receiver(env.s) == "OBS-R05"          # antenna = "ANTSW-01" in observatory.toml
    env.cfg.raw["scheduler"]["receivers"]["OBS-R05"]["antenna"] = "DISCONE"
    env.cfg.raw["antenna_switch"]["receiver"] = ""
    assert rflayout.switch_receiver(env.s) == ""
    env.cfg.raw["antenna_switch"]["receiver"] = "NO-SUCH-RX"
    assert rflayout.switch_receiver(env.s) == ""


def test_the_layout_puts_the_switch_in_front_of_the_web_receiver(rf, env):
    web_receiver_behind_switch(env, "OBS-R55")
    rf.save(two_antennas(rf.current["layout"]), rf.current["version"], "owner (web)")
    rx = env.s.receivers["OBS-R08"]
    assert rx.switched and rx.antenna == "ANTSW-01"
    assert {n: p["antenna"] for n, p in env.s.switch_ports.items()} == {1: "EGGBEATER-2M", 2: "DISCONE-TRAM"}
    assert rf.port_calls[-1][2]["antenna"] == "DISCONE-TRAM"         # the broker's lease API follows the layout


def test_the_switch_cannot_feed_another_receiver(rf, env):
    web_receiver_behind_switch(env, "OBS-R55")
    lay = two_antennas(rf.current["layout"])
    lay["receivers"]["OBS-R08"]["chain"] = ["DISCONE-TRAM"]
    lay["components"]["ANTSW-01"]["ports"]["2"] = None
    lay["receivers"]["OBS-R05"]["chain"] = ["ANTSW-01"]
    with pytest.raises(SchedulerError) as e:
        rf.save(lay, rf.current["version"], "owner (web)")
    assert "keep ANTSW-01 at the start of its path" in str(e.value) and "controlled for OBS-R08 only" in str(e.value)


def test_the_settings_page_names_the_switched_receiver(env):
    st = env.s.config_store
    res = st.save("integrations", {**st.current("integrations")["data"], "antenna_switch_receiver": "OBS-R08"},
                  None, "owner (web)", config=env.cfg)
    assert res["needs_restart"] == ["Receiver the antenna switch feeds"]
    assert env.cfg.section("antenna_switch")["receiver"] == "OBS-R08"


def test_hardware_rereads_the_switch_address_and_token(env, tmp_path: pathlib.Path):
    env.cfg.raw["antenna_switch"] = {"base_url": ""}
    hw = LiveHardware(env.cfg)
    assert hw.switch_url == "" and hw.switch_token is None
    secret = tmp_path / "antswitch.env"
    secret.write_text("ANTSW_URL=http://antswitch.local\nANTSW_API_TOKEN=abc123\n")
    env.cfg.raw["antenna_switch"] = {"base_url": "http://antswitch.local", "token_file": str(secret)}
    hw.configure_switch(env.cfg)                                 # the Settings page's values, applied after start
    assert hw.switch_url == "http://antswitch.local" and hw.switch_token == "abc123"


def test_jobs_lease_the_switch_by_serial_and_cap_the_watch(rf, env):
    """The broker, the pirate watch and the runner all name the dongle by serial (OBS-R55 here), not by its ID."""
    import datetime as dt

    from aobs.coordination.broker import Broker
    from test_broker import FakeHw
    from test_scheduler import NOW, rtl_job, submit

    web_receiver_behind_switch(env, "OBS-R55")
    rf.save(two_antennas(rf.current["layout"]), rf.current["version"], "owner (web)")
    env.cfg.raw["broker"] = {"receivers": ["OBS-R55"]}
    env.cfg.raw["legacy"]["adsb_serial"] = "OBS-R55"                 # the shared ADS-B dongle pauses for leases
    env.cfg.raw["antenna_switch"]["receiver"] = "OBS-R55"
    broker, hw = Broker(env.cfg, (fake := FakeHw()), env.clock), fake
    broker.ports.clear()
    broker.ports.update(env.s.switch_ports)
    broker.reservation_guard = env.s.next_reservation
    job = submit(env, rtl_job("iss", rx="OBS-R08", receiver__antenna="EGGBEATER-2M", receiver__serial="OBS-R55",
                              start=20, end=30, satdump={"enabled": False}, postprocess={"decode": False},
                              target={"type": "frequency", "name": "x"}))["results"][0]
    assert "job_id" in job, job
    assert env.s.get(job["job_id"])["spec"]["resolved"]["switch_port"] == 1
    watch = broker.lease("piratewatch", ttl_s=3600, receiver="OBS-R55", port=2)
    assert hw.port == 2
    assert dt.datetime.fromisoformat(watch["expires_at"]) <= NOW + dt.timedelta(minutes=20)   # capped before the job
    broker.take_for_job(env.s.receivers["OBS-R08"].serial, job["job_id"], 1, NOW + dt.timedelta(minutes=31))
    lease = broker.state()["leases"]["OBS-R55"]
    assert lease["owner"] == f"scheduler:{job['job_id']}" and hw.port == 1
