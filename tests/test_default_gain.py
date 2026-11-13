"""A receiver's default gain profile (RF editor): bookings, rules, Signal Path Tests and API jobs start from it,
never from a computed midpoint (Airspy 8/8/8) or the last test's gain."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from aobs.scheduling import rflayout, schedview
from aobs.scheduling.hardware import default_radio
from aobs.scheduling.rules import build_radio
from aobs.scheduling.store import SchedulerError
from test_inventory import add_airspy, inv, sysfs  # noqa: F401
from test_testlab import lab  # noqa: F401
from test_scheduler import env  # noqa: F401

DEFAULT = {"LNA": 5, "MIX": 5, "VGA": 6}


def set_profiles(env, rid: str, profiles: list[dict[str, Any]]) -> None:
    lay = copy.deepcopy(env.s.rf.current["layout"])
    lay["receivers"].setdefault(rid, {"id": rid})["gain_profiles"] = profiles
    env.s.rf.save(lay, env.s.rf.current["version"], "owner (web)")


@pytest.fixture
def rx02(env, inv):
    add_airspy(inv, "RX-02")
    set_profiles(env, "RX-02", [{"name": "old", "gain_mode": "manual", "gain_elements": {"LNA": 8, "MIX": 8, "VGA": 8}},
                                {"name": "Sawbird", "gain_mode": "manual", "gain_elements": DEFAULT, "default": True}])
    return env.s.receivers["RX-02"]


def test_without_a_default_airspy_starts_at_the_midpoint(env, inv):
    add_airspy(inv, "RX-02")
    assert default_radio(env.s.receivers["RX-02"], 137_900_000)["gain_elements"] != DEFAULT


def test_the_default_profile_reaches_the_receiver_and_default_radio(rx02):
    assert rx02.default_gain["name"] == "Sawbird"
    assert default_radio(rx02, 137_900_000)["gain_elements"] == DEFAULT


def test_only_one_default_per_receiver(env, rx02):
    lay = copy.deepcopy(env.s.rf.current["layout"])
    for p in lay["receivers"]["RX-02"]["gain_profiles"]:
        p["default"] = True
    with pytest.raises(SchedulerError, match="only one gain profile can be the default"):
        env.s.rf.save(lay, env.s.rf.current["version"], "owner (web)")


def test_a_default_that_does_not_fit_the_driver_is_ignored_and_warned(env, rx02):
    set_profiles(env, "RX-02", [{"name": "rtl", "gain_mode": "manual", "gain_db": 29.7, "default": True}])
    _, _, warns = rflayout.validate(env.s.rf.current["layout"], env.s)
    assert any('"rtl" is the default but does not fit' in w for w in warns)
    assert default_radio(env.s.receivers["RX-02"], 137_900_000)["gain_elements"] != {}


def test_manual_booking_form_and_booking(env, rx02):
    form = {"name": "m", "receiver_id": "RX-02", "frequency_hz": 137_900_000, "norad": 57166,
            "start": "2026-11-09T12:00:00+00:00", "end": "2026-11-09T12:10:00+00:00", "timezone": "UTC"}
    job = schedview.job_from_form(env.s, form)
    assert job["radio"]["gain_elements"] == DEFAULT


def test_rules_without_a_gain_profile_use_the_default(env, rx02):
    problems: list[str] = []
    radio = build_radio(env.s, {"gain_profile": None, "sample_rate_hz": None}, rx02, 137_900_000, problems)
    assert radio["gain_elements"] == DEFAULT and not problems
    radio = build_radio(env.s, {"gain_profile": "old", "sample_rate_hz": None}, rx02, 137_900_000, problems)
    assert radio["gain_elements"] == {"LNA": 8, "MIX": 8, "VGA": 8}             # a named profile is still honoured


def test_api_jobs_without_a_gain_get_the_default(env, rx02):
    from aobs.scheduling import spec as specmod
    raw = {"job_name": "api-job", "target": {"type": "frequency", "name": "x"},
           "schedule": {"start": "2026-11-09T12:00:00+00:00", "duration_seconds": 60, "timezone": "UTC"},
           "receiver": {"receiver_id": "RX-02", "driver": "airspy", "serial": rx02.serial},
           "radio": {"center_frequency_hz": 137_587_500, "target_frequency_hz": 137_900_000,
                     "sample_rate_hz": 2_500_000},
           "recording": {"enabled": True, "engine": "soapysdr", "format": "cs16"}}
    checked = specmod.check_job(raw, env.s.context())
    assert checked.job["radio"]["gain_elements"] == DEFAULT
    assert any("default gain profile 'Sawbird'" in d for d in checked.job["resolved"]["defaults_applied"])


def test_signal_path_test_form_ignores_the_last_tests_gain(env, rx02, lab, monkeypatch):
    last = {"frequency_hz": 137_900_000, "center_frequency_hz": None, "sample_rate_hz": 2_500_000, "duration_s": 30,
            "gain_elements": {"LNA": 8, "MIX": 8, "VGA": 8}, "gain_mode": "manual", "signal_type": "general"}
    monkeypatch.setattr(lab, "_last_request", lambda rid: dict(last))
    lab_opts = lab.options("RX-02")
    assert lab_opts["defaults"]["source"] == "your last test on RX-02"           # other settings still come from it
    assert lab_opts["defaults"]["gain_elements"] == DEFAULT
    assert "default gain profile" in lab_opts["defaults"]["gain_source"]
