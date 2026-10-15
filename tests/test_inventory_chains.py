"""Brief 5: inventory records (antennas, filters, LNAs, splitters, switches, rotators...) and signal chains.

Paths are only ever what the owner declared; these tests check the model, the derived receiver fields,
the recording snapshots and the warnings. No hardware is involved.
"""
from __future__ import annotations

import copy
from typing import Any

import pytest

from aobs.scheduling import rflayout
from aobs.scheduling.store import SchedulerError
from test_rflayout import owner_layout, rf  # noqa: F401
from test_scheduler import FakeRunnerHw, env, rtl_job, run_job, submit  # noqa: F401


def part(lay: dict[str, Any], cid: str, name: str, kind: str, **kw: Any) -> None:
    lay["components"][cid] = {"id": cid, "name": name, "kind": kind, "status": "working", "power": "none", **kw}


def shared_discone(rf: rflayout.RfLayout) -> dict[str, Any]:
    """Discone > LaNA > splitter on OBS-R02, and OBS-R01 fed from the splitter's second output."""
    lay = owner_layout(rf)
    part(lay, "SPLIT-2", "2-way splitter", "splitter", outputs=2)
    lay["receivers"]["OBS-R02"]["chain"] = ["DISCONE", "LANA", "SPLIT-2"]
    lay["receivers"]["OBS-R01"].update(chain=["SAWBIRD-NOAA"], feed="SPLIT-2")
    lay["components"].pop("V-DIPOLE-COPPER")
    return lay


def warnings(rf: rflayout.RfLayout, lay: dict[str, Any]) -> list[str]:
    res = rf.save(lay, rf.current["version"], "owner (web)", dry_run=True)
    assert res["ok"], res["errors"]
    return res["warnings"]


def test_older_layouts_load_with_the_new_fields_filled_in(rf, env):
    rf.save(owner_layout(rf), 1, "owner (web)")
    c = rf.current["layout"]["components"]
    assert c["DISCONE"]["polarization"] == "unknown" and c["DISCONE"]["mount"] == "unknown" and c["DISCONE"]["rotator"] is None
    assert c["AM-BANDSTOP"]["filter_type"] == "other" and c["AM-BANDSTOP"]["dc_pass"] == "unknown"
    assert c["SAWBIRD-NOAA"]["supply"] == "unknown"                       # own-supply parts say whether it is connected
    assert c["ANTSW-01"]["driver"] == "antsw-http" and c["ANTSW-01"]["port_count"] == 8
    assert rf.current["layout"]["receivers"]["OBS-R02"]["feed"] is None
    assert env.s.receivers["OBS-R01"].signal_path == "Copper V-dipole > Sawbird NOAA > OBS-R01"   # derived fields unchanged


def test_antenna_and_part_properties_are_kept(rf, env):
    lay = owner_layout(rf)
    lay["components"]["DISCONE"].update(manufacturer="Diamond", model="D-130", antenna_type="Discone", polarization="vertical",
                                        location="roof mast", orientation="vertical", mount="fixed", description="wideband",
                                        min_mhz=25, max_mhz=1300)
    lay["components"]["AM-BANDSTOP"].update(filter_type="notch", min_mhz=0.5, max_mhz=1.7)
    part(lay, "COAX-3M", "LMR-400 3 m", "cable", length_m=3, gain_db=-0.4)
    part(lay, "SMA-N", "SMA to N adapter", "connector")
    lay["receivers"]["OBS-R02"]["chain"] += ["COAX-3M", "SMA-N"]
    rf.save(lay, 1, "owner (web)")
    c = rf.current["layout"]["components"]
    assert {k: c["DISCONE"][k] for k in ("manufacturer", "model", "antenna_type", "polarization", "location", "orientation",
                                         "mount", "description", "min_mhz", "max_mhz")} == {
        "manufacturer": "Diamond", "model": "D-130", "antenna_type": "Discone", "polarization": "vertical",
        "location": "roof mast", "orientation": "vertical", "mount": "fixed", "description": "wideband",
        "min_mhz": 25.0, "max_mhz": 1300.0}
    assert c["COAX-3M"]["length_m"] == 3.0 and c["SMA-N"]["kind"] == "connector"
    assert env.s.receivers["OBS-R02"].signal_path == "Discone > Nooelec LaNA > LMR-400 3 m > SMA to N adapter > OBS-R02"


def test_one_antenna_feeds_two_receivers_through_a_splitter(rf, env):
    res = rf.save(shared_discone(rf), 1, "owner (web)")
    lay = rf.current["layout"]
    assert rflayout.full_chain(lay, "OBS-R01") == ["DISCONE", "LANA", "SPLIT-2", "SAWBIRD-NOAA"]
    r01 = env.s.receivers["OBS-R01"]
    assert r01.antenna == "DISCONE" and r01.signal_path == "Discone > Nooelec LaNA > 2-way splitter > Sawbird NOAA > OBS-R01"
    assert r01.amplifier == "Nooelec LaNA; Sawbird NOAA"
    assert any("OBS-R01: fed from 2-way splitter" in c for c in res["changes"])
    snap = rf.snapshot("OBS-R01")
    assert [(c["id"], c.get("shared", False)) for c in snap["chain"]] == [
        ("DISCONE", True), ("LANA", True), ("SPLIT-2", True), ("SAWBIRD-NOAA", False)]


@pytest.mark.parametrize("mutate, message", [
    (lambda l: l["receivers"]["RSP1B"].update(chain=[], feed="SPLIT-2"), "has 2 outputs but feeds 3 receivers"),
    (lambda l: l["receivers"]["OBS-R01"].update(chain=["QFH-137"]) or l["receivers"]["RSP1B"].update(chain=[]),
     "its own path cannot start with"),
    (lambda l: l["receivers"]["OBS-R01"].update(feed="LANA"), "not a splitter"),
    (lambda l: l["receivers"]["OBS-R02"].update(chain=["DISCONE", "LANA"]), "not in any receiver's path"),
    (lambda l: l["receivers"]["OBS-R01"]["chain"].append("SPLIT-2"), "in two places"),
    (lambda l: l["receivers"]["OBS-R02"]["chain"].append("DISCONE"), "in two places"),
])
def test_impossible_shared_paths_are_refused(rf, mutate, message):
    lay = shared_discone(rf)
    mutate(lay)
    dry = rf.save(lay, 1, "owner (web)", dry_run=True)
    assert not dry["ok"] and any(message in e for e in dry["errors"]), dry["errors"]


def test_antsw_cannot_feed_another_receiver_through_a_splitter(rf):
    lay = owner_layout(rf)
    part(lay, "SPLIT-2", "2-way splitter", "splitter")
    lay["receivers"]["OBS-R05"]["chain"] = ["ANTSW-01", "FM-BANDSTOP-2", "SPLIT-2"]
    lay["receivers"]["RSP1B"].update(chain=[], feed="SPLIT-2")
    dry = rf.save(lay, 1, "owner (web)", dry_run=True)
    assert any("cannot feed RSP1B (not even through a splitter" in e for e in dry["errors"]), dry["errors"]


def test_feeds_that_loop_are_refused(rf):
    lay = owner_layout(rf)
    part(lay, "SPLIT-A", "splitter A", "splitter")
    part(lay, "SPLIT-B", "splitter B", "splitter")
    lay["receivers"]["RSP1B"].update(chain=["SPLIT-A"], feed="SPLIT-B")
    lay["receivers"]["OBS-R03"].update(chain=["SPLIT-B"], feed="SPLIT-A")
    dry = rf.save(lay, 1, "owner (web)", dry_run=True)
    assert any("form a loop" in e for e in dry["errors"]), dry["errors"]


def test_a_manual_switch_records_the_antenna_on_its_selected_port(rf, env):
    lay = owner_layout(rf)
    part(lay, "COAX-SW", "Coax switch", "switch", port_count=4, ports={"1": "V-DIPOLE-COPPER", "2": "TELESCOPING-V"})
    lay["receivers"]["OBS-R01"]["chain"] = ["COAX-SW", "SAWBIRD-NOAA"]
    lay["receivers"]["OBS-R03"]["chain"] = []
    assert any("no port is selected on Coax switch" in w for w in warnings(rf, lay))
    lay["components"]["COAX-SW"]["selected_port"] = "2"
    rf.save(lay, 1, "owner (web)")
    sw = rf.current["layout"]["components"]["COAX-SW"]
    assert sw["driver"] == "manual" and list(sw["ports"]) == ["1", "2", "3", "4"]
    r01 = env.s.receivers["OBS-R01"]
    assert r01.antenna == "TELESCOPING-V" and not r01.switched
    assert r01.signal_path == "Telescoping V-dipole > Coax switch port 2 > Sawbird NOAA > OBS-R01"
    snap = rf.snapshot("OBS-R01")
    assert snap["chain"][0] == {"id": "TELESCOPING-V", "name": "Telescoping V-dipole", "kind": "antenna", "status": "working",
                                "via_port": "2", "polarization": "unknown", "min_mhz": None, "max_mhz": None}
    lay = copy.deepcopy(rf.current["layout"])
    lay["components"]["COAX-SW"]["selected_port"] = "3"
    w = warnings(rf, lay)
    assert any("Coax switch is set to port 3, which has no antenna" in x for x in w)
    res = rf.rename("COAX-SW", "SW-2", rf.current["version"], "owner (web)")          # only ANTSW-01's ID is fixed
    assert res["ok"] and rf.current["layout"]["receivers"]["OBS-R01"]["chain"][0] == "SW-2"


def test_switch_port_counts_are_bounded(rf):
    lay = owner_layout(rf)
    part(lay, "BIG-SW", "Big switch", "switch", port_count=20)
    dry = rf.save(lay, 1, "owner (web)", dry_run=True)
    assert any("a switch has 2-16 ports" in e for e in dry["errors"])


def test_rotators_turn_antennas_and_are_not_in_paths(rf):
    lay = owner_layout(rf)
    part(lay, "G-5500", "Yaesu G-5500", "rotator")
    lay["components"]["QFH-137"]["rotator"] = "G-5500"
    rf.save(lay, 1, "owner (web)")
    assert rf.current["layout"]["components"]["QFH-137"]["mount"] == "rotatable"
    rf.rename("G-5500", "ROTATOR-1", 2, "owner (web)")
    assert rf.current["layout"]["components"]["QFH-137"]["rotator"] == "ROTATOR-1"
    lay = copy.deepcopy(rf.current["layout"])
    lay["receivers"]["RSP1B"]["chain"].append("ROTATOR-1")
    assert any("is a rotator" in e for e in rf.save(lay, 3, "owner (web)", dry_run=True)["errors"])
    lay = copy.deepcopy(rf.current["layout"])
    lay["components"]["QFH-137"]["rotator"] = "LANA"
    assert any("is not a rotator" in e for e in rf.save(lay, 3, "owner (web)", dry_run=True)["errors"])


def test_automatic_recordings_outside_the_antenna_or_through_a_notch_are_warned(rf):
    lay = owner_layout(rf)
    lay["components"]["V-DIPOLE-COPPER"].update(min_mhz=400, max_mhz=470)       # OBS-R01 records 137.5 MHz automatically
    lay["components"]["SAWBIRD-NOAA"].update(min_mhz=135, max_mhz=140)
    lay["components"]["FM-BANDSTOP-1"].update(filter_type="notch", min_mhz=130, max_mhz=140)
    lay["receivers"]["RSP1B"]["chain"].remove("FM-BANDSTOP-1")
    lay["receivers"]["OBS-R02"]["chain"].append("FM-BANDSTOP-1")
    w = warnings(rf, lay)
    assert "OBS-R01: automatic recordings (2 satellites): 137.5 MHz is outside Copper V-dipole's range (400–470 MHz)" in w
    assert "OBS-R02: automatic recordings (1 satellite): 137.5 MHz is inside FM bandstop's stop band (130–140 MHz)" in w
    assert not any("Sawbird" in x for x in w)                                   # inside the amplifier's rated range
    lay["components"]["FM-BANDSTOP-1"].update(filter_type="bandpass", min_mhz=430, max_mhz=440)
    w = warnings(rf, lay)
    assert "OBS-R02: automatic recordings (1 satellite): 137.5 MHz is outside FM bandstop's passband (430–440 MHz)" in w


def test_antenna_and_receiver_with_no_common_frequencies_are_warned(rf):
    lay = owner_layout(rf)
    lay["components"]["DISCONE"].update(min_mhz=2000, max_mhz=6000)            # RTL-SDR tunes 24-1766 MHz
    lay["receivers"]["OBS-R03"].update(min_mhz=1, max_mhz=10)
    lay["components"]["TELESCOPING-V"].update(min_mhz=50, max_mhz=900)
    lay["components"]["FM-BANDSTOP-1"].update(filter_type="bandpass", min_mhz=1000, max_mhz=1100)
    lay["components"]["QFH-137"].update(min_mhz=136, max_mhz=138)
    w = warnings(rf, lay)
    assert any(x.startswith("OBS-R02: Discone (2000–6000 MHz) and the receiver (24–1766 MHz) have no frequencies") for x in w)
    assert any(x.startswith("OBS-R03: Telescoping V-dipole (50–900 MHz) and the receiver (1–10 MHz)") for x in w)
    assert "RSP1B: FM bandstop's passband (1000–1100 MHz) is outside Diamond DP-KE137 QFH's range (136–138 MHz)" in w


def test_power_and_bias_tee_warnings(rf, env):
    lay = owner_layout(rf)
    lay["components"]["SAWBIRD-NOAA"]["supply"] = "missing"
    part(lay, "MAST-LNA", "Mast LNA", "amplifier", power="bias_tee")
    lay["components"]["AM-BANDSTOP"]["dc_pass"] = "no"
    lay["receivers"]["RSP1B"]["chain"] = ["QFH-137", "MAST-LNA", "AM-BANDSTOP", "FM-BANDSTOP-1"]
    w = warnings(rf, lay)
    assert "OBS-R01: Sawbird NOAA needs its own power supply, which is marked not connected" in w
    assert "RSP1B: Mast LNA needs bias-tee DC from the receiver, but AM bandstop does not pass DC" in w
    env.s.receivers["OBS-R05"] = env.s.receivers["OBS-R05"].__class__(**{**env.s.receivers["OBS-R05"].__dict__,
                                                                          "bias_tee_fixed": False})
    lay["receivers"]["RSP1B"]["chain"] = ["QFH-137", "AM-BANDSTOP", "FM-BANDSTOP-1"]
    lay["receivers"]["OBS-R05"]["chain"] = ["ANTSW-01", "MAST-LNA"]
    lay["receivers"]["OBS-R02"]["chain"] = ["DISCONE", "LANA", "FM-BANDSTOP-2"]
    assert "OBS-R05's bias tee is permanently off, but Mast LNA needs bias-tee power" in warnings(rf, lay)


def test_two_always_on_bias_tees_on_one_splitter_are_warned(rf):
    lay = shared_discone(rf)
    lay["receivers"]["OBS-R03"].update(chain=[], feed="SPLIT-2", bias_tee="always_on")
    lay["components"]["SPLIT-2"]["outputs"] = 3
    lay["receivers"]["OBS-R01"]["feed"] = None
    lay["receivers"]["OBS-R01"]["chain"] = ["QFH-137", "SAWBIRD-NOAA"]
    lay["receivers"]["RSP1B"]["chain"] = []
    lay["receivers"]["EXT-9"] = {"id": "EXT-9", "chain": [], "feed": "SPLIT-2", "label": "Other", "bias_tee": "always_on"}
    w = warnings(rf, lay)
    assert any(x.startswith("2-way splitter: OBS-R03, EXT-9 all have their bias tee permanently on") for x in w), w


def test_gain_profiles_are_checked_against_the_driver(rf, env):
    lay = owner_layout(rf)
    lay["receivers"]["OBS-R02"]["gain_profiles"] = [{"name": "NOAA", "gain_mode": "manual", "gain_db": 29.7},
                                                    {"name": "bad", "gain_mode": "manual", "gain_db": 30}]
    lay["receivers"]["RSP1B"]["gain_profiles"] = [{"name": "weak", "gain_mode": "manual", "gain_elements": {"IFGR": 70, "RFGR": 4}},
                                                  {"name": "agc", "gain_mode": "auto", "gain_elements": {"RFGR": 4}}]
    w = warnings(rf, lay)
    assert any(x.startswith('OBS-R02: gain profile "bad": 30 dB is not an R820T gain step') for x in w)
    assert 'RSP1B: gain profile "weak": IFGR must be 20–59 dB' in w
    assert not any('"NOAA"' in x or '"agc"' in x for x in w)
    rf.save(lay, 1, "owner (web)")                                  # warnings never block saving
    assert rf.current["layout"]["receivers"]["OBS-R02"]["gain_profiles"][0] == {
        "name": "NOAA", "gain_mode": "manual", "gain_db": 29.7, "gain_elements": {}, "notes": ""}
    lay = copy.deepcopy(rf.current["layout"])
    lay["receivers"]["OBS-R02"]["gain_profiles"].append({"name": "NOAA", "gain_db": 20.7})
    with pytest.raises(SchedulerError, match="two gain profiles are called 'NOAA'"):
        rf.save(lay, 2, "owner (web)")


def test_documentation_receivers_carry_their_own_properties(rf):
    lay = owner_layout(rf)
    lay["receivers"]["OBS-R03"].update(min_mhz=24, max_mhz=1766, sample_rates="2.4 MS/s", formats="cu8", bias_tee="switchable")
    rf.save(lay, 1, "owner (web)")
    r = rf.current["layout"]["receivers"]["OBS-R03"]
    assert (r["min_mhz"], r["max_mhz"], r["sample_rates"], r["formats"], r["bias_tee"]) == (24.0, 1766.0, "2.4 MS/s", "cu8", "switchable")


def test_job_checks_warn_about_the_path_but_never_refuse(rf, env):
    lay = owner_layout(rf)
    lay["components"]["DISCONE"].update(min_mhz=400, max_mhz=470)
    rf.save(lay, 1, "owner (web)")
    r = submit(env, rtl_job("path-warning"), dry=True)["results"][0]
    assert not r["errors"], r["errors"]
    assert "signal path: 137.9 MHz is outside Discone's range (400–470 MHz)" in r["warnings"]
    # ANTSW-01: the antenna the job picks is the one checked
    lay = copy.deepcopy(rf.current["layout"])
    lay["components"]["MA-30-LOOP"].update(min_mhz=0.01, max_mhz=30)
    rf.save(lay, 2, "owner (web)")
    r = submit(env, rtl_job("loop", rx="OBS-R05", receiver__antenna="MA-30-LOOP"), dry=True)["results"][0]
    assert any("outside MA-30-LOOP's range" in w for w in r["warnings"]), r["warnings"]
    r = submit(env, rtl_job("dipole", rx="OBS-R05", receiver__antenna="V-DIPOLE"), dry=True)["results"][0]
    assert not any("signal path" in w for w in r["warnings"])


def test_the_editor_view_reports_warnings_for_the_saved_layout(rf):
    lay = owner_layout(rf)
    lay["components"]["LANA"]["status"] = "broken"
    rf.save(lay, 1, "owner (web)")
    v = rf.view()
    assert "OBS-R02: Nooelec LaNA is marked broken" in v["warnings"]
    assert v["options"]["filter_types"][1] == "bandpass" and "rotator" in v["kinds"]
    assert v["derived"]["OBS-R02"]["full_chain"] == ["DISCONE", "LANA"]


def test_a_recording_through_a_splitter_keeps_its_whole_path(rf, env):
    rf.save(shared_discone(rf), 1, "owner (web)")
    lay = copy.deepcopy(rf.current["layout"])
    # OBS-R02 records; its own path is unchanged by OBS-R01 sharing the splitter
    jid, _ = run_job(env, FakeRunnerHw(), rtl_job(satdump={"enabled": False}, postprocess={"decode": False}))
    snap = env.s.get(jid)["spec"]["resolved"]["rf_path"]
    assert snap["text"] == "Discone > Nooelec LaNA > 2-way splitter > OBS-R02"
    assert not any(c.get("shared") for c in snap["chain"])
    assert lay == rf.current["layout"]
