"""The pirate watch's read-only views for Starlab's web page, kept non-speech audio, and the local-time stamp fix."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import time
import urllib.error
import urllib.request
from typing import Any

import numpy as np
import pytest

from aobs.piratewatch import heatmap
from aobs.piratewatch.audio import write_wav
from aobs.piratewatch.config import Settings
from aobs.piratewatch.control import ControlServer
from aobs.piratewatch.spectrum import Baseline, Sweep
from aobs.piratewatch.store import History, Reader, Store
from aobs.piratewatch.transcribe import Transcriber, WhisperProcess

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)
TOKEN = "t0ken"


@pytest.fixture
def new_york(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _clip(s: Settings, name: str, seconds: float = 1.0) -> pathlib.Path:
    day = s.path("clips", "2026-11-12")
    day.mkdir(parents=True, exist_ok=True)
    path = day / name
    write_wav(str(path), (np.sin(np.arange(int(25_000 * seconds)) / 4) * 8000).astype(np.int16), 25_000)
    return path


def _populate(s: Settings) -> dict[str, int]:
    st = Store(s.path("piratewatch.sqlite3"))
    ids = {
        "speech": st.add_hit(detected_at="2026-11-12T12:00:00.000Z", start_utc="2026-11-12T12:00:01.000Z",
                             end_utc="2026-11-12T12:00:05.000Z", freq_hz=251_950_000, channel_hz=251_950_000,
                             duration_s=4.0, peak_db=-12.0, snr_db=18.0, status="speech", language="pt",
                             transcript="alô", translation="hello", audio_path=str(_clip(s, "a.wav"))),
        "no_speech": st.add_hit(detected_at="2026-11-12T13:10:00.000Z", start_utc="2026-11-12T13:10:01.000Z",
                                end_utc="2026-11-12T13:10:02.000Z", freq_hz=260_254_500, channel_hz=260_255_000,
                                duration_s=0.4, peak_db=-9.0, snr_db=22.0, status="no_speech",
                                audio_path=str(_clip(s, "b.wav", 0.5))),
        "missed": st.add_hit(detected_at="2026-11-13T02:00:00.000Z", freq_hz=260_254_000, channel_hz=260_255_000,
                             peak_db=-10.0, snr_db=20.0, status="missed", note="no carrier after tuning"),
    }
    st.add_activity(251_950_000, T0, 4.0)
    st.add_speech(251_950_000, T0)
    st.add_activity(260_255_000, T0 + dt.timedelta(hours=1), 0.4)
    st.add_activity(260_255_000, T0 + dt.timedelta(hours=14), 0.0)
    st.close()
    return ids


# ------------------------------------------------------------ local-time stamps (rtl_power) fixed once
def test_old_hit_log_detection_times_move_from_local_time_to_utc(tmp_path: pathlib.Path, new_york: None) -> None:
    import sqlite3
    path = tmp_path / "pw.sqlite3"
    st = Store(path)
    hid = st.add_hit(detected_at="2026-11-12T07:34:17.000Z", start_utc="2026-11-12T12:34:18.000Z", freq_hz=1,
                     channel_hz=1, status="missed")
    st.close()
    db = sqlite3.connect(path)          # an hit log written before the fix (user_version 0)
    db.execute("PRAGMA user_version=0")
    db.commit()
    db.close()
    st = Store(path)
    assert st.migrated_local_stamps
    row = st.hit(hid)
    assert row and row["detected_at"] == "2026-11-12T12:34:17.000Z"
    assert row["start_utc"] == "2026-11-12T12:34:18.000Z"           # start/end came from the real clock already
    st.close()
    again = Store(path)                                             # only once
    assert not again.migrated_local_stamps and (again.hit(hid) or {})["detected_at"] == "2026-11-12T12:34:17.000Z"


def test_old_history_stamps_move_to_utc_once(tmp_path: pathlib.Path, new_york: None) -> None:
    root = tmp_path / "history"
    root.mkdir()
    wrong = dt.datetime(2026, 11, 12, 7, 34, 17, tzinfo=UTC).timestamp()     # 07:34 local written as UTC
    np.array([wrong], dtype=np.float64).tofile(root / "2026-11-12-abcd1234.ts")
    h = History(root)
    assert h.fixed_files == 1
    assert np.fromfile(root / "2026-11-12-abcd1234.ts")[0] == dt.datetime(2026, 11, 12, 12, 34, 17, tzinfo=UTC).timestamp()
    assert History(root).fixed_files == 0
    fresh = History(tmp_path / "new")                               # a new history is marked, never shifted
    assert fresh.fixed_files == 0 and (tmp_path / "new" / History.MARKER).exists()


# ------------------------------------------------------------ non-speech audio kept 24 h
class _Worker(WhisperProcess):
    def __init__(self) -> None:
        self.proc = None

    def close(self) -> None:
        pass


def test_non_speech_audio_is_kept_then_pruned(tmp_path: pathlib.Path) -> None:
    s = Settings(data_dir=str(tmp_path))
    st = Store(s.path("piratewatch.sqlite3"))
    clip = _clip(s, "n.wav")
    hid = st.add_hit(detected_at="2026-11-12T12:00:00.000Z", start_utc="2026-11-12T12:00:00.000Z",
                     end_utc="2026-11-12T12:00:02.000Z", freq_hz=260_254_500, channel_hz=260_255_000,
                     audio_path=str(clip), status="pending")
    now = [T0 + dt.timedelta(minutes=5)]
    tr = Transcriber(s, st, worker=_Worker(), clock=lambda: now[0])
    tr.apply(st.hit(hid) or {}, {"ok": True, "speech": False, "speech_prob": 0.1, "language": "nn",
                                  "language_prob": 0.4, "transcript": "Thank you."})
    row = st.hit(hid) or {}
    assert row["status"] == "no_speech" and row["audio_path"] == str(clip) and clip.exists()
    assert row["transcript"] == "Thank you." and "kept 24 h" in row["note"]
    assert tr.prune() == 0
    now[0] = T0 + dt.timedelta(hours=24, minutes=1)
    assert tr.prune() == 1
    row = st.hit(hid) or {}
    assert row["audio_path"] is None and not clip.exists() and "deleted after 24 h" in row["note"]


# ------------------------------------------------------------ queries
def test_reader_filters_pages_and_hides_paths(tmp_path: pathlib.Path) -> None:
    s = Settings(data_dir=str(tmp_path))
    ids = _populate(s)
    r = Reader(s.path("piratewatch.sqlite3"))
    assert [h["id"] for h in r.hits()["hits"]] == [ids["speech"]]
    every = r.hits(speech_only=False)["hits"]
    assert [h["id"] for h in every] == [ids["missed"], ids["no_speech"], ids["speech"]]
    assert all("audio_path" not in h for h in every)
    assert [h["has_audio"] for h in every] == [False, True, True]
    assert [h["id"] for h in r.hits(False, min_hz=260_000_000, max_hz=261_000_000)["hits"]] == [ids["missed"], ids["no_speech"]]
    assert [h["id"] for h in r.hits(False, since="2026-11-12T13:00:00.000Z", until="2026-11-13T00:00:00.000Z")["hits"]] \
        == [ids["no_speech"]]
    assert [h["id"] for h in r.hits(False, channel_hz=260_255_000, hour=13)["hits"]] == [ids["no_speech"]]
    page = r.hits(False, limit=2)
    assert page["more"] and len(page["hits"]) == 2
    rest = r.hits(False, limit=2, before_id=page["hits"][-1]["id"])
    assert not rest["more"] and [h["id"] for h in rest["hits"]] == [ids["speech"]]
    assert Reader(tmp_path / "missing.sqlite3").hits() == {"hits": [], "more": False}


def test_hour_grid_lists_only_active_channels() -> None:
    rows = [{"channel_hz": 251_950_000, "hour_utc": "2026-11-12T12", "detections": 2, "seconds": 4.0, "speech": 1},
            {"channel_hz": 251_950_000, "hour_utc": "2026-11-13T12", "detections": 1, "seconds": 1.5, "speech": 0},
            {"channel_hz": 260_255_000, "hour_utc": "2026-11-12T13", "detections": 5, "seconds": 0.0, "speech": 0}]
    g = heatmap.hour_grid(rows)
    assert [c["channel_hz"] for c in g["channels"]] == [251_950_000, 260_255_000]
    a = g["channels"][0]
    assert a["detections"][12] == 3 and a["seconds"][12] == 5.5 and a["speech"][12] == 1
    assert a["totals"] == {"detections": 3, "seconds": 5.5, "speech": 1}
    assert g["peak"] == {"detections": 5, "seconds": 5.5, "speech": 1}


def test_ignore_list_separates_learned_and_auto_masked() -> None:
    freqs = 243_000_000 + (np.arange(2000) + 0.5) * 5_000
    rng = np.random.default_rng(3)
    b = Baseline(learn_sweeps=30, threshold_db=8)
    for i in range(30):
        db = -30 + rng.normal(0, 0.5, len(freqs))
        db[np.abs(freqs - 248_000_000) < 7_500] = -10
        b.add(Sweep(T0 + dt.timedelta(seconds=2 * i), freqs, db.astype(np.float32)))
    b.exclude(250_000_000, 250_020_000)
    ig = b.ignore_list()
    assert ig["complete"] and ig["learned_sweeps"] == 30
    [learned] = ig["learned"]
    assert learned["lo_hz"] <= 247_992_500 and learned["hi_hz"] >= 248_007_500 and learned["duty_pct"] == 100.0
    [auto] = ig["auto"]
    assert auto["lo_hz"] <= 250_000_000 <= 250_020_000 <= auto["hi_hz"] and "duty_pct" not in auto
    assert Baseline().ignore_list()["learned"] == []


# ------------------------------------------------------------ the control endpoint (what the broker proxies)
def _get(port: int, path: str, token: str = TOKEN, headers: dict[str, str] | None = None) -> tuple[int, Any, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers={"Authorization": f"Bearer {token}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
            return r.status, (json.loads(body) if r.headers["Content-Type"] == "application/json" else body), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), dict(e.headers)


@pytest.fixture
def served(tmp_path: pathlib.Path) -> Any:
    s = Settings(data_dir=str(tmp_path / "data"))
    ids = _populate(s)
    s.path("status.json").write_text(json.dumps({"state": "recording", "detail": "260.2545 MHz", "at": "x",
                                                 "started_at": "2026-11-12T12:00:00.000Z", "recording_hz": 260_254_500,
                                                 "lease_expires": "2026-11-12T12:40:00.000Z"}))
    server = ControlServer(s, TOKEN, host="127.0.0.1", port=0)
    server.start()
    assert server.bound.wait(5)
    yield s, server.port, ids
    server.stop()


def test_control_serves_status_hits_heatmap_and_ignore_list(served: Any) -> None:
    s, port, ids = served
    assert _get(port, "/v1/piratewatch", token="wrong")[0] == 401
    code, st, _ = _get(port, "/v1/piratewatch")
    assert code == 200 and st["state"] == "recording" and st["recording_hz"] == 260_254_500
    assert st["started_at"] and st["band_hz"] == [243_000_000, 270_000_000] and st["no_speech_keep_h"] == 24
    code, h, _ = _get(port, "/v1/piratewatch/hits?speech=0&min_hz=260000000&limit=10")
    assert code == 200 and [x["id"] for x in h["hits"]] == [ids["missed"], ids["no_speech"]]
    assert _get(port, "/v1/piratewatch/hits?hour=25")[0] == 400
    assert _get(port, "/v1/piratewatch/hits?since=yesterday")[0] == 400
    code, g, _ = _get(port, "/v1/piratewatch/heatmap?since=2026-11-12T00:00:00Z&until=2026-11-13T00:00:00Z")
    assert code == 200 and [c["channel_hz"] for c in g["channels"]] == [251_950_000, 260_255_000]
    assert g["channels"][1]["detections"][13] == 1 and g["channels"][1]["detections"][2] == 0   # 02Z on the 13th: out
    code, ig, _ = _get(port, "/v1/piratewatch/ignored")
    assert code == 200 and ig["saved"] is False and ig["learned"] == []
    assert _get(port, "/v1/piratewatch/../../etc/passwd")[0] == 404


def test_control_serves_clips_with_ranges_and_only_from_the_clips_folder(served: Any) -> None:
    s, port, ids = served
    code, body, hdr = _get(port, f"/v1/piratewatch/clips/{ids['speech']}")
    assert code == 200 and body[:4] == b"RIFF" and hdr["Content-Type"] == "audio/wav" and hdr["Accept-Ranges"] == "bytes"
    size = len(body)
    code, part, hdr = _get(port, f"/v1/piratewatch/clips/{ids['speech']}", headers={"Range": "bytes=4-11"})
    assert code == 206 and part == body[4:12] and hdr["Content-Range"] == f"bytes 4-11/{size}"
    code, tail, _ = _get(port, f"/v1/piratewatch/clips/{ids['speech']}", headers={"Range": "bytes=-4"})
    assert code == 206 and tail == body[-4:]
    assert _get(port, f"/v1/piratewatch/clips/{ids['speech']}", headers={"Range": f"bytes={size + 5}-"})[0] == 416
    assert _get(port, f"/v1/piratewatch/clips/{ids['no_speech']}")[0] == 200      # kept non-speech audio plays
    code, err, _ = _get(port, f"/v1/piratewatch/clips/{ids['missed']}")
    assert code == 404 and "missed bursts have none" in err["detail"]
    st = Store(s.path("piratewatch.sqlite3"))                      # a path outside clips/ is never served
    outside = s.root / "elsewhere.wav"
    outside.write_bytes(b"RIFFxxxx")
    st.update_hit(ids["speech"], audio_path=str(outside))
    st.close()
    assert _get(port, f"/v1/piratewatch/clips/{ids['speech']}")[0] == 404
