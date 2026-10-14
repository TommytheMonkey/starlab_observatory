from __future__ import annotations

import datetime as dt
import pathlib
import types
from typing import Any

import pytest

from aobs.config import Config
from aobs.hardware.rtlsdr import UsbRtl
from aobs.satellites.forecast import Forecast, PlannedPass, plan_segments
from aobs.satellites.readiness import SystemProbe

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 12, 0, 0, 0, tzinfo=UTC)
TLE_LINE1 = "1 57166U 23091A   26284.31195887  .00000008  00000+0  22246-4 0  9991"
TLE_LINE1_M24 = "1 59051U 24039A   26284.31761372  .00000010  00000+0  25000-4 0  9990"


@pytest.fixture
def tmp_config(tmp_path: pathlib.Path) -> Config:
    raw: dict[str, Any] = {
        "site": {"name": "t", "host": "observatory", "display_timezone": "America/New_York"},
        "paths": {"data_root": str(tmp_path), "database_url": f"sqlite:///{tmp_path}/t.sqlite3",
                  "tle_cache": str(tmp_path / "tle"), "secrets_dir": str(tmp_path / "s")},
        "legacy": {"recorder_unit": "observatory-record@{receiver}.service", "adsb_device_index": 0, "adsb_serial": "OBS-R05"},
        "forecast": {"horizon_hours": 72, "receiver": "OBS-R01", "max_tle_age_hours_ok": 72, "max_tle_age_hours_warn": 336},
        "readiness": {"recorder_agreement_tolerance_s": 90, "min_extra_free_gib": 10, "conflict_imminent_minutes": 30},
        "outcome": {"grace_minutes": 5, "min_coverage": 0.9},
        "calendar": {"enabled": False, "calendar_id": "", "event_minutes": 10, "service_account_file": str(tmp_path / "s/sa.json"), "token_file": str(tmp_path / "s/token.json"),
                     "client_secret_file": str(tmp_path / "s/cs.json"), "calendar_name": "Satellite Observatory"},
        "antennas": {"QFH-137": {"description": "Diamond DP-KE137 QFH", "min_hz": 136_000_000, "max_hz": 139_000_000,
                                 "hardwired_receiver": "OBS-R01"}},
        "antenna_switch": {"base_url": "http://192.0.2.1"},
    }
    return Config(raw=raw, path=tmp_path / "observatory.toml")


@pytest.fixture
def fake_legacy(tmp_path: pathlib.Path) -> Any:
    root = tmp_path / "recordings"
    (root / "OBS-R01").mkdir(parents=True)
    (root / "pass-cache" / "OBS-R01").mkdir(parents=True)
    (root / "pass-cache" / "OBS-R01" / "observatory.tle").write_text(
        f"METEOR-M2 3\n{TLE_LINE1}\n2 57166 ...\nMETEOR-M2 4\n{TLE_LINE1_M24}\n2 59051 ...\n")
    recorder = {"storage_root": str(root), "segment_seconds": 600, "retention_hours": 6, "min_free_gib": 64,
                "receivers": {"OBS-R01": {"antenna": "137 MHz helix", "capability": "WEATHER_137MHZ",
                                          "pass_targets": [57166, 59051], "frequency_hz": 137500000,
                                          "sample_rate": 2400000, "gain_db": 0}}}
    station = {"passes": {"look_ahead_hours": 12},
               "receivers": [{"id": "OBS-R01", "serial": "OBS-R01", "capabilities": ["METEOR_LRPT", "WEATHER_137MHZ"]}],
               "jobs": [{"id": "meteor_m2_3", "name": "METEOR M2-3", "norad_id": 57166, "receiver": "OBS-R01", "enabled": True},
                        {"id": "meteor_m2_4", "name": "METEOR M2-4", "norad_id": 59051, "receiver": "OBS-R01", "enabled": True}]}
    return types.SimpleNamespace(recorder=recorder, station=station)


def make_pass(start: dt.datetime, norad: int = 57166, rev: int = 17115, max_el: float = 50.0,
              seconds: int = 690, name: str | None = None) -> PlannedPass:
    end = start + dt.timedelta(seconds=seconds)
    rise = start + dt.timedelta(seconds=120)
    st = end - dt.timedelta(seconds=120)
    return PlannedPass(
        pass_id=f"{norad}-r{rev}", norad_id=norad, satellite_name=name or ("METEOR M2-3" if norad == 57166 else "METEOR M2-4"),
        job_id="j", revolution=rev, host="observatory", receiver="OBS-R01", antenna="Diamond DP-KE137 QFH",
        center_frequency_hz=137_500_000, sample_rate=2_400_000, gain_db=0.0,
        aos=rise - dt.timedelta(minutes=4), los=st + dt.timedelta(minutes=4), threshold_rise=rise, threshold_set=st,
        min_elevation_deg=20.0, culmination=start + dt.timedelta(seconds=seconds / 2), max_elevation_deg=max_el,
        record_start=start, record_end=end, capture_start=start, capture_end=end,
        capture_segments=plan_segments(start, end, 600), merged_with=[], predicted_quality="good",
        tle_epoch=NOW - dt.timedelta(hours=12), predicted_at=NOW,
    )


def make_forecast(passes: list[PlannedPass], now: dt.datetime = NOW) -> Forecast:
    return Forecast(generated_at=now, horizon_hours=72, recorder_horizon_hours=12, receiver="OBS-R01",
                    tle_path=pathlib.Path("/dev/null"), tle_epochs={}, passes=passes)


def make_probe(**overrides: Any) -> SystemProbe:
    values: dict[str, Any] = dict(
        hostname="observatory",
        rtl_devices=[UsbRtl("OBS-R01", "RTL2838UHIDIR", "Realtek", 3, 10, "/sys/x"),
                     UsbRtl("OBS-R05", "NESDR SMArt v5", "Nooelec", 3, 12, "/sys/y")],
        index_map=["OBS-R05", "OBS-R01", "OBS-R02"], index_map_error=None,
        service={"unit": "observatory-record@OBS-R01.service", "ActiveState": "active", "SubState": "running",
                 "UnitFileState": "enabled", "NRestarts": "0"},
        recorder_intent=None, leases={}, holders=[], unreadable_procs=0, disk_free=800 * 1024**3,
        antsw_state={"receiver": "OBS-R05", "port": 7}, antsw_error=None, recorder_capturing=False,
    )
    values.update(overrides)
    return SystemProbe(**values)
