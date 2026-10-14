import datetime as dt

from aobs.hardware.rtlsdr import Holder
from aobs.legacy import RecorderIntent
from aobs.satellites.readiness import FAIL, NA, PASS, WARN, evaluate

from conftest import NOW, make_forecast, make_pass, make_probe


def _checks(result):
    return {c.name: c for c in result.checks}


def test_next_pass_ready_when_recorder_agrees(tmp_config, fake_legacy):
    p = make_pass(NOW + dt.timedelta(hours=1))
    probe = make_probe(recorder_intent=RecorderIntent("OBS-R01", "METEOR M2-3", p.capture_start, ""))
    r = evaluate(tmp_config, fake_legacy, make_forecast([p]), probe, NOW)[p.pass_id]
    assert r.verdict == "READY", r.reason
    assert all(c.status == PASS for c in r.checks)


def test_recorder_disagreement_is_warning(tmp_config, fake_legacy):
    p = make_pass(NOW + dt.timedelta(hours=1))
    probe = make_probe(recorder_intent=RecorderIntent("OBS-R01", "METEOR M2-3", p.capture_start + dt.timedelta(minutes=10), ""))
    r = evaluate(tmp_config, fake_legacy, make_forecast([p]), probe, NOW)[p.pass_id]
    assert r.verdict == "WARNING"
    assert _checks(r)["recorder_agreement"].status == FAIL


def test_missing_recorder_intent_never_ready(tmp_config, fake_legacy):
    p = make_pass(NOW + dt.timedelta(hours=1))
    r = evaluate(tmp_config, fake_legacy, make_forecast([p]), make_probe(), NOW)[p.pass_id]
    assert r.verdict == "WARNING"


def test_beyond_recorder_horizon_is_scheduled_not_ready(tmp_config, fake_legacy):
    first = make_pass(NOW + dt.timedelta(hours=1))
    later = make_pass(NOW + dt.timedelta(hours=30), rev=17130)
    probe = make_probe(recorder_intent=RecorderIntent("OBS-R01", "METEOR M2-3", first.capture_start, ""))
    res = evaluate(tmp_config, fake_legacy, make_forecast([first, later]), probe, NOW)
    assert res[later.pass_id].verdict == "SCHEDULED"
    assert _checks(res[later.pass_id])["recorder_agreement"].status == NA


def test_service_down_fails(tmp_config, fake_legacy):
    p = make_pass(NOW + dt.timedelta(hours=1))
    probe = make_probe(service={"unit": "u", "ActiveState": "failed", "SubState": "failed", "UnitFileState": "enabled"},
                       recorder_intent=RecorderIntent("OBS-R01", "METEOR M2-3", p.capture_start, ""))
    r = evaluate(tmp_config, fake_legacy, make_forecast([p]), probe, NOW)[p.pass_id]
    assert r.verdict == "WARNING"
    assert _checks(r)["recording_service_active"].status == FAIL


def test_receiver_missing_fails(tmp_config, fake_legacy):
    p = make_pass(NOW + dt.timedelta(hours=1))
    r = evaluate(tmp_config, fake_legacy, make_forecast([p]), make_probe(rtl_devices=[]), NOW)[p.pass_id]
    assert _checks(r)["receiver_present"].status == FAIL


def test_foreign_lease_blocks(tmp_config, fake_legacy):
    p = make_pass(NOW + dt.timedelta(hours=1))
    probe = make_probe(leases={"OBS-R01": {"owner": "aobs:discovery", "priority": 10}},
                       recorder_intent=RecorderIntent("OBS-R01", "METEOR M2-3", p.capture_start, ""))
    r = evaluate(tmp_config, fake_legacy, make_forecast([p]), probe, NOW)[p.pass_id]
    assert _checks(r)["no_scheduling_conflict"].status == FAIL


def test_sdrpp_holding_receiver_fail_when_imminent_warn_when_far(tmp_config, fake_legacy):
    soon = make_pass(NOW + dt.timedelta(minutes=10))
    far = make_pass(NOW + dt.timedelta(hours=5), rev=17118)
    probe = make_probe(holders=[Holder(1, "sdrpp", "sdrpp --server")],
                       recorder_intent=RecorderIntent("OBS-R01", "METEOR M2-3", soon.capture_start, ""))
    res = evaluate(tmp_config, fake_legacy, make_forecast([soon, far]), probe, NOW)
    assert _checks(res[soon.pass_id])["no_scheduling_conflict"].status == FAIL
    assert _checks(res[far.pass_id])["no_scheduling_conflict"].status == WARN


def test_recorders_own_rtl_sdr_is_not_a_conflict(tmp_config, fake_legacy):
    p = make_pass(NOW - dt.timedelta(minutes=1))
    probe = make_probe(holders=[Holder(2, "rtl_sdr", "/usr/bin/rtl_sdr -d OBS-R01 -f 137500000")], recorder_capturing=True)
    r = evaluate(tmp_config, fake_legacy, make_forecast([p]), probe, NOW)[p.pass_id]
    assert _checks(r)["no_scheduling_conflict"].status == PASS
    assert _checks(r)["recorder_agreement"].status == PASS


def test_adsb_index_drift_detected(tmp_config, fake_legacy):
    p = make_pass(NOW + dt.timedelta(hours=1))
    probe = make_probe(index_map=["OBS-R01", "OBS-R05"],
                       recorder_intent=RecorderIntent("OBS-R01", "METEOR M2-3", p.capture_start, ""))
    r = evaluate(tmp_config, fake_legacy, make_forecast([p]), probe, NOW)[p.pass_id]
    assert _checks(r)["no_scheduling_conflict"].status == FAIL  # dump1090 would seize OBS-R01


def test_low_storage_fails(tmp_config, fake_legacy):
    p = make_pass(NOW + dt.timedelta(hours=1))
    probe = make_probe(disk_free=65 * 1024**3, recorder_intent=RecorderIntent("OBS-R01", "METEOR M2-3", p.capture_start, ""))
    r = evaluate(tmp_config, fake_legacy, make_forecast([p]), probe, NOW)[p.pass_id]
    assert _checks(r)["storage"].status == FAIL


def test_stale_tle_warns(tmp_config, fake_legacy):
    import dataclasses
    p = dataclasses.replace(make_pass(NOW + dt.timedelta(hours=1)), tle_epoch=NOW - dt.timedelta(days=5))
    probe = make_probe(recorder_intent=RecorderIntent("OBS-R01", "METEOR M2-3", p.capture_start, ""))
    r = evaluate(tmp_config, fake_legacy, make_forecast([p]), probe, NOW)[p.pass_id]
    assert _checks(r)["valid_prediction"].status == WARN


def test_unassigned_satellite_fails(tmp_config, fake_legacy):
    fake_legacy.recorder["receivers"]["OBS-R01"]["pass_targets"] = [59051]
    p = make_pass(NOW + dt.timedelta(hours=1))
    probe = make_probe(recorder_intent=RecorderIntent("OBS-R01", "METEOR M2-3", p.capture_start, ""))
    r = evaluate(tmp_config, fake_legacy, make_forecast([p]), probe, NOW)[p.pass_id]
    assert _checks(r)["receiver_assignment"].status == FAIL
