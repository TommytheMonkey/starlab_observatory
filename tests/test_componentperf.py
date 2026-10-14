from __future__ import annotations

import json

from aobs.scheduling.componentperf import compare_antennas, dashboard, performance
from test_scan import SCAN, run as run_scan, scanlab  # noqa: F401
from test_scheduler import env  # noqa: F401
from test_testlab import BASE, lab as _lab_fixture, run_test  # noqa: F401


def _set_r02(env, chain: list[str], note: str) -> None:
    lay = json.loads(json.dumps(env.s.rf.current["layout"]))
    if "FM-BANDSTOP-2" in chain:
        lay["receivers"]["OBS-R05"]["chain"] = ["ANTSW-01"]
    lay["receivers"]["OBS-R02"]["chain"] = chain
    env.s.rf.save(lay, env.s.rf.current["version"], "owner (web)", note)


def test_filter_history_from_scans_and_filter_tests(env, scanlab):
    a = run_scan(env, scanlab, SCAN)
    _set_r02(env, ["DISCONE", "FM-BANDSTOP-2", "LANA"], "FM bandstop on R02")
    scanlab.hw.filt = True
    b = run_scan(env, scanlab, {**SCAN, "baseline_test_id": a})
    scanlab.filter_test({"component_id": "FM-BANDSTOP-2", "baseline_test_id": a, "modified_test_id": b}, "owner (web)")
    p = performance(scanlab, "FM-BANDSTOP-2")
    assert p["component"]["kind"] == "filter" and p["location"] == {"receiver": "OBS-R02", "position": 1}
    assert p["tests_by_type"] == {"scan": 1, "filter_test": 1}
    pair = p["pairs"][0]
    assert pair["change"] == "added" and pair["comparable"] and pair["regions"][0]["median_change_db"] < -15
    texts = " ".join(o["text"] for o in p["observations"])
    assert "filter test(s) measured a median stopband attenuation" in texts
    assert "In 1 of 1 comparable before/after scan pair" in texts
    assert "Nothing here supports installing or removing" in texts
    assert p["path_history"][-1]["where"] == "OBS-R02"
    assert any(h["where"] == "OBS-R05" for h in p["path_history"])          # where it used to be


def test_amplifier_pairs_comparability_and_conservative_claims(env, _lab_fixture):
    lab = _lab_fixture
    base = run_test(env, lab, BASE)["test_id"]                              # Discone > LaNA > R02
    _set_r02(env, ["DISCONE"], "LaNA removed")
    lab.hw.tone = 3.0
    without = run_test(env, lab, {**BASE, "baseline_test_id": base})["test_id"]
    other_gain = run_test(env, lab, {**BASE, "gain_db": 40.2, "baseline_test_id": base})["test_id"]
    p = performance(lab, "LANA")
    pairs = {x["without"]: x for x in p["pairs"]}
    assert pairs[without]["comparable"] and pairs[without]["change"] == "removed"
    assert pairs[without]["deltas"]["snr_db"] > 3                            # stronger tone while the LNA was in
    assert not pairs[other_gain]["comparable"] and "different gain" in pairs[other_gain]["not_comparable_because"]
    texts = " ".join(o["text"] for o in p["observations"])
    assert "No consistent change in reception has been established" in texts
    assert "does not measure the amplifier's noise figure" in texts
    assert "Only 1 comparable pair" in texts


def test_three_agreeing_pairs_are_needed_for_a_consistent_claim(env, _lab_fixture):
    lab = _lab_fixture
    for k in range(3):
        _set_r02(env, ["DISCONE", "LANA"], f"LaNA in {k}")
        lab.hw.tone = 9.0
        b = run_test(env, lab, BASE)["test_id"]
        _set_r02(env, ["DISCONE"], f"LaNA out {k}")
        lab.hw.tone = 3.0
        run_test(env, lab, {**BASE, "baseline_test_id": b})
    p = performance(lab, "LANA")
    o = next(x for x in p["observations"] if x["evidence"] == "before/after signal tests" and "SNR" in x["text"])
    assert o["text"].startswith("In all 3 comparable pairs the target's SNR was higher") and o["confidence"] == "medium"


def test_history_survives_deleting_the_part(env, _lab_fixture):
    lab = _lab_fixture
    run_test(env, lab, BASE)
    lay = json.loads(json.dumps(env.s.rf.current["layout"]))
    lay["receivers"]["OBS-R02"]["chain"] = ["DISCONE"]
    del lay["components"]["LANA"]
    env.s.rf.save(lay, env.s.rf.current["version"], "owner (web)", "LaNA gone")
    p = performance(lab, "LANA")
    assert not p["in_layout"] and p["component"]["name"] == "Nooelec LaNA" and p["tests"] == 1


def test_antenna_comparison_lists_confounders_and_dashboard_sections(env, _lab_fixture):
    lab = _lab_fixture
    a = run_test(env, lab, BASE)["test_id"]
    lay = json.loads(json.dumps(env.s.rf.current["layout"]))
    lay["receivers"]["OBS-R01"]["chain"] = []                                   # free the dipole
    lay["receivers"]["OBS-R02"]["chain"] = ["V-DIPOLE-COPPER", "LANA"]
    env.s.rf.save(lay, env.s.rf.current["version"], "owner (web)", "dipole on R02")
    run_test(env, lab, {**BASE, "gain_db": 40.2, "baseline_test_id": a})
    c = compare_antennas(lab, "DISCONE", "V-DIPOLE-COPPER")
    band = c["bands"][0]
    assert band["frequency_mhz"] == 138.0 and "different gain settings" in band["confounders"]
    d = dashboard(lab)
    assert d["total_tests"] == 2 and d["comparisons"] and d["baselines"][0]["test_id"] == a
    assert {x["id"] for x in d["components"]} >= {"DISCONE", "LANA", "V-DIPOLE-COPPER"}
