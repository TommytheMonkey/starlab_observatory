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
    assert res["needs_restart"] == []                            # the broker follows it (cli.follow_switch_receiver)
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


def per_port_gains(lay: dict) -> dict:
    lay = two_antennas(lay)
    lay["receivers"]["OBS-R08"]["gain_profiles"] = [
        {"name": "Discone + LaNA", "gain_mode": "manual", "gain_db": 16.6, "antenna": "DISCONE-TRAM", "default": True},
        {"name": "Eggbeater 2m", "gain_mode": "manual", "gain_db": 38.6, "antenna": "EGGBEATER-2M", "default": True},
        {"name": "General", "gain_mode": "manual", "gain_db": 29.7, "default": True}]
    return lay


def test_each_switch_port_gets_its_own_default_gain(rf, env):
    """The discone path has a LaNA before the switch; the Eggbeater path has none: 2 m bookings need more gain."""
    from aobs.scheduling.hardware import default_radio
    from test_scheduler import rtl_job, submit

    web_receiver_behind_switch(env, "OBS-R55")
    rf.save(per_port_gains(rf.current["layout"]), rf.current["version"], "owner (web)")
    rx = env.s.receivers["OBS-R08"]
    assert rx.antenna_gains["EGGBEATER-2M"]["gain_db"] == 38.6 and rx.default_gain["gain_db"] == 29.7

    def booked(antenna: str, start: float) -> dict:
        job = rtl_job(f"j-{antenna.lower()}", rx="OBS-R08", receiver__antenna=antenna, receiver__serial="OBS-R55",
                      start=start, end=start + 10, satdump={"enabled": False}, postprocess={"decode": False},
                      target={"type": "frequency", "name": "x"})
        job["radio"].pop("gain_db"); job["radio"].pop("gain_mode")        # no gain asked for: the port's default
        res = submit(env, job)["results"][0]
        assert res.get("job_id"), res
        return env.s.get(res["job_id"])

    egg = booked("EGGBEATER-2M", 60)
    assert egg["spec"]["radio"]["gain_db"] == 38.6 and egg["spec"]["resolved"]["switch_port"] == 1
    disc = booked("DISCONE-TRAM", 90)
    assert disc["spec"]["radio"]["gain_db"] == 16.6 and disc["spec"]["resolved"]["switch_port"] == 2
    assert default_radio(rx, 145_800_000, antenna="EGGBEATER-2M")["gain_db"] == 38.6
    assert default_radio(rx, 145_800_000)["gain_db"] == 29.7                   # no antenna: the receiver's default


def test_a_gain_that_does_not_fit_the_port_is_flagged(rf, env):
    from aobs.scheduling import spec as specmod
    from test_scheduler import rtl_job

    web_receiver_behind_switch(env, "OBS-R55")
    rf.save(per_port_gains(rf.current["layout"]), rf.current["version"], "owner (web)")
    job = rtl_job("iss-low", rx="OBS-R08", receiver__antenna="EGGBEATER-2M", receiver__serial="OBS-R55",
                  satdump={"enabled": False}, postprocess={"decode": False}, target={"type": "frequency", "name": "x"})
    job["radio"]["gain_db"] = 16.6                                             # the discone's gain on the Eggbeater
    checked = specmod.check_job(job, env.s.context())
    assert any("differs from the default for EGGBEATER-2M" in w for w in checked.warnings)


def test_gain_profile_antennas_are_checked(rf, env):
    web_receiver_behind_switch(env, "OBS-R55")
    lay = per_port_gains(rf.current["layout"])
    lay["receivers"]["OBS-R08"]["gain_profiles"].append(
        {"name": "Second egg", "gain_mode": "manual", "gain_db": 40.2, "antenna": "EGGBEATER-2M", "default": True})
    with pytest.raises(SchedulerError, match="only one gain profile can be the default for EGGBEATER-2M"):
        rf.save(lay, rf.current["version"], "owner (web)")
    lay = per_port_gains(rf.current["layout"])
    lay["receivers"]["OBS-R08"]["gain_profiles"][0]["antenna"] = "QFH-137"     # not on the switch
    with pytest.raises(SchedulerError, match="not on a port of the switch"):
        rf.save(lay, rf.current["version"], "owner (web)")


def test_the_broker_puts_the_switch_back_on_the_leased_port(tmp_config):
    """The board boots to port 1; a lease that asked for port 2 must not stay on port 1 after a reboot."""
    from aobs.coordination.broker import Broker
    from test_broker import Clock, FakeHw

    tmp_config.raw["antenna_switch"].update(receiver="OBS-R05", ports=[
        {"port": 1, "code": 0, "antenna": "EGGBEATER-2M"}, {"port": 2, "code": 1, "antenna": "DISCONE"}])
    tmp_config.raw["broker"] = {"receivers": ["OBS-R05"]}
    hw = FakeHw()
    hw.adsb = False
    broker = Broker(tmp_config, hw, Clock())
    lease = broker.lease("pirate-watch", ttl_s=120, receiver="OBS-R05", port=2)
    assert broker.check_switch_port(force=True) == {"checked": True, "port": 2, "corrected": False}
    hw.port = 1                                                    # the board rebooted
    assert broker.check_switch_port(force=True) == {"checked": True, "port": 2, "was": 1, "corrected": True}
    assert hw.port == 2 and hw.calls[-1] == "select 2"
    assert broker.renew(lease["token"], 120)["port_corrections"] == 1
    assert broker.check_switch_port() is None                      # rate-limited between watchdog ticks
    broker.release(lease["token"])
    hw.port = 1
    assert broker.check_switch_port(force=True) is None            # no lease asks for a port: leave the board alone


def test_the_watch_logs_a_port_correction(caplog):
    from aobs.piratewatch.lease import LeaseKeeper

    class Client:
        def __init__(self) -> None:
            self.fixed = 0

        def call(self, method: str, path: str, body: dict | None = None) -> dict:
            return {"token": "t", "expires_at": "2099-01-01T00:00:00Z", "port_corrections": self.fixed}

    c = Client()
    k = LeaseKeeper(c, "OBS-R05", "pirate-watch", 2, 120, 3600)    # type: ignore[arg-type]
    k.acquire()
    k.renew_once()
    assert "re-selected" not in caplog.text
    c.fixed = 1
    k.renew_once()
    assert "ANTSW-01 was not on port 2" in caplog.text
    k.release()


def test_the_test_form_starts_from_the_ports_default_gain(rf, env):
    from aobs.scheduling.runner import Runner
    from aobs.scheduling.service import SchedulerService
    from aobs.scheduling.testlab import TestLab
    from test_scheduler import FakeRunnerHw, planner

    web_receiver_behind_switch(env, "OBS-R55")
    rf.save(per_port_gains(rf.current["layout"]), rf.current["version"], "owner (web)")
    lab = TestLab(SchedulerService(env.s, planner(env, []), Runner(env.s, FakeRunnerHw())), lambda: {"leases": {}})
    o = lab.options("OBS-R08")
    assert o["defaults"]["antenna"] == "EGGBEATER-2M" and o["defaults"]["gain_db"] == 38.6   # port 1 first
    assert "Eggbeater 2m" in o["defaults"]["gain_source"]
    assert o["antenna_gains"]["DISCONE-TRAM"]["gain_db"] == 16.6                         # applied on a port change
