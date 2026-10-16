import datetime as dt
import json
import pathlib

import pytest

from aobs.legacy import latest_recorder_intent
from aobs.satellites.forecast import plan_segments, quality_label

UTC = dt.timezone.utc


def test_plan_segments_matches_recorder_chunking():
    start = dt.datetime(2026, 10, 12, 2, 6, 23, tzinfo=UTC)
    segs = plan_segments(start, start + dt.timedelta(seconds=693), 600)
    assert [s.seconds for s in segs] == [600, 91]
    assert [s.seconds for s in plan_segments(start, start + dt.timedelta(seconds=482), 600)] == [482]


@pytest.mark.parametrize("el,label", [(80, "excellent"), (45, "good"), (30, "fair"), (20.0, "poor")])
def test_quality_label(el, label):
    assert quality_label(el) == label


JOURNAL = """\
2026-10-11 16:20:56,516 INFO OBS-R01: Waiting for METEOR M2-3; recording starts 2026-10-12T02:06:23.785386+00:00
2026-10-11 16:25:56,538 INFO OBS-R01: Waiting for METEOR M2-3; recording starts 2026-10-12T02:06:23.827832+00:00
"""


def test_journal_intent_latest_line():
    intent = latest_recorder_intent("x", JOURNAL)
    assert intent.satellites == "METEOR M2-3"
    assert intent.record_start == dt.datetime(2026, 10, 12, 2, 6, 23, 827832, tzinfo=UTC)


def test_journal_intent_consumed_by_capture():
    text = JOURNAL + "2026-10-11 22:06:24,000 INFO Recording OBS-R01 at 137.500 MHz: /data/x.cu8.part\n"
    assert latest_recorder_intent("x", text) is None


# ------------------------------------------------- integration with real host
LIVE = pathlib.Path("/etc/observatory-sdr/config.json").is_file() and pathlib.Path("/opt/observatory-sdr/observatory_scheduler.py").is_file()


@pytest.mark.skipif(not LIVE, reason="requires the observatory host")
@pytest.mark.skipif(not LIVE, reason="requires the observatory host (its config and live orbital-element cache)")
def test_72h_forecast_contains_recorders_12h_forecast_exactly(tmp_path):
    """Extending look_ahead_hours must not change any pass the recorder itself would schedule."""
    from aobs.config import load_config
    from aobs.legacy import load_legacy, predict, recorder_jobs, refresh_tle

    config = load_config(pathlib.Path(__file__).parents[1] / "config" / "observatory.toml")
    legacy = load_legacy(config)
    jobs = recorder_jobs(legacy, "OBS-R01")
    seed = pathlib.Path(legacy.recorder["storage_root"]) / "pass-cache" / "OBS-R01" / "observatory.tle"
    tle = refresh_tle(legacy, tmp_path, jobs, [seed])
    short = predict(legacy, tle, jobs, 12)
    long = predict(legacy, tle, jobs, 72)
    assert len(long) > len(short)
    # the two predictions run moments apart; times can straddle a second boundary, so allow 2 s
    near = lambda a, b: abs((a - b).total_seconds()) <= 2  # noqa: E731
    for w in short:
        assert any(x.job.norad_id == w.job.norad_id and near(x.record_start, w.record_start)
                   and near(x.record_end, w.record_end) for x in long), (w.job.norad_id, w.record_start)
    assert all(w.max_elevation_degrees >= w.job.min_elevation_degrees for w in long)
    last = max(w.record_start for w in long)
    assert last - min(w.record_start for w in long) > dt.timedelta(hours=60)


@pytest.mark.skipif(not LIVE, reason="requires the observatory host")
def test_reconcile_real_recording_from_today():
    """The 2026-10-11 19:30Z METEOR M2-4 pass was really recorded by the legacy recorder."""
    from aobs.satellites.outcomes import reconcile

    d = pathlib.Path("/data/observatory-recordings/OBS-R01")
    meta = d / "OBS-R01_20261011T193054.099742Z.json"
    if not meta.exists():
        pytest.skip("segment already pruned by recorder retention")
    p = json.loads(meta.read_text())["pass"]
    sat = p["satellites"][0]
    o = reconcile(int(sat["norad_id"]), dt.datetime.fromisoformat(sat["rise"]),
                  dt.datetime.fromisoformat(p["record_start"]), dt.datetime.fromisoformat(p["record_end"]),
                  d, dt.datetime.now(UTC), dt.timedelta(minutes=5), 0.9, None, [], dt.timedelta(hours=10_000))
    assert o.status == "RECORDED", o.reason
    assert len(o.segments) == 2
