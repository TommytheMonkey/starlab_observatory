import datetime as dt
import json
import pathlib

from aobs.satellites.outcomes import reconcile

UTC = dt.timezone.utc
START = dt.datetime(2026, 10, 11, 19, 30, 54, tzinfo=UTC)
END = START + dt.timedelta(seconds=690)
RISE = START + dt.timedelta(seconds=120)
GRACE = dt.timedelta(minutes=5)


def write_segment(d: pathlib.Path, start: dt.datetime, seconds: int, status="recorded", raw=True, short=False):
    base = "OBS-R01_" + start.strftime("%Y%m%dT%H%M%S.%fZ")
    expected = 2400000 * 2 * seconds
    size = expected // 2 if short else expected
    if raw:
        with (d / f"{base}.cu8").open("wb") as fh:
            fh.truncate(size)  # sparse file: real size without using disk
    (d / f"{base}.json").write_text(json.dumps({
        "receiver": "OBS-R01", "frequency_hz": 137500000, "sample_rate": 2400000, "baseband_format": "cu8",
        "started_at": start.isoformat(), "finished_at": (start + dt.timedelta(seconds=seconds + 1.7)).isoformat(),
        "status": status, "raw_file": f"{base}.cu8", "raw_bytes": size, "expected_bytes": expected,
        "rtl_sdr_return_code": 0, "error": None,
        "pass": {"record_start": START.isoformat(), "record_end": END.isoformat(),
                 "satellites": [{"name": "METEOR M2-3", "norad_id": 57166, "rise": RISE.isoformat(), "set": "x"}]},
    }))


def run(d, now, parts=(), lease=None):
    return reconcile(57166, RISE, START, END, d, now, GRACE, 0.9, lease, list(parts))


def test_before_window_none(tmp_path):
    assert run(tmp_path, START - dt.timedelta(minutes=1)) is None


def test_recorded_two_segments(tmp_path):
    write_segment(tmp_path, START, 600)
    write_segment(tmp_path, START + dt.timedelta(seconds=601.7), 88)
    o = run(tmp_path, END + GRACE + dt.timedelta(seconds=1))
    assert o.status == "RECORDED", o.reason
    assert o.coverage > 0.99
    assert o.recorded_bytes == 2400000 * 2 * 688
    assert o.actual_start == START


def test_missed_when_no_metadata(tmp_path):
    assert run(tmp_path, END + GRACE + dt.timedelta(seconds=1)).status == "MISSED"


def test_failed_segment(tmp_path):
    write_segment(tmp_path, START, 600, status="failed", short=True)
    o = run(tmp_path, END + GRACE + dt.timedelta(seconds=1))
    assert o.status == "FAILED"
    assert "failed" in o.reason


def test_partial_coverage_failed(tmp_path):
    write_segment(tmp_path, START, 300)
    o = run(tmp_path, END + GRACE + dt.timedelta(seconds=1))
    assert o.status == "FAILED"
    assert "%" in o.reason


def test_raw_missing_soon_after_is_failed(tmp_path):
    write_segment(tmp_path, START, 690, raw=False)
    o = run(tmp_path, END + GRACE + dt.timedelta(seconds=1))
    assert o.status == "FAILED"
    assert "missing" in o.reason


def test_recording_in_progress(tmp_path):
    part = tmp_path / "x.cu8.part"
    part.write_bytes(b"\0")
    o = run(tmp_path, START + dt.timedelta(minutes=2), parts=[part], lease="observatory:passes:OBS-R01")
    assert o.status == "RECORDING"


def test_window_open_without_capture_is_warning(tmp_path):
    o = run(tmp_path, START + dt.timedelta(minutes=2))
    assert o.status == "WARNING"


def test_unrelated_satellite_segment_ignored(tmp_path):
    write_segment(tmp_path, START, 690)
    o = reconcile(59051, RISE, START, END, tmp_path, END + GRACE * 2, GRACE, 0.9, None, [])
    assert o.status == "MISSED"
