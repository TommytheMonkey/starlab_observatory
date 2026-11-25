"""APRS packet fields (aprsparse) and listenable FM audio (fmaudio): Mic-E parsing of a real RS0ISS packet, Doppler
from orbital elements, the streaming DSP, activity segments, and the audio attempt after an ISS recording."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import wave
from typing import Any

import numpy as np
import pytest

from aobs.satellites import predict
from aobs.scheduling import aprsparse, fmaudio
from test_scheduler import NOW, FakeHandle, FakeRunnerHw, drain, env, run_job  # noqa: F401  (env is a fixture)

UTC = dt.timezone.utc
ISS = 25544
# Decoded by Direwolf (atest) from m2026112118302d55eb, the ISS on 2026-11-24 18:08 UTC, at 437.825 MHz, as
# aprsdec wrote it to aprs.json (control bytes in Direwolf's <0xNN> notation)
RS0ISS = {"at_s": 394.319,
          "tnc2": "RS0ISS>0P0PS4,APRSAT:'v&<0x1c>l <0x1c>SI]ARISS-International Space Station=<0x0d>",
          "source": "RS0ISS", "destination": "0P0PS4", "path": ["APRSAT"],
          "info": "'v&<0x1c>l <0x1c>SI]ARISS-International Space Station=<0x0d>"}
RECORDING = "/data/x/ISS_OBS-R07_20261124T180816Z_437294000_2048000.cu8"
# digipeated by RS0ISS in the same pass (heard in the Doppler-tracked audio)
N1RCN = {"at_s": 141.58, "tnc2": "N1RCN>APRS,RS0ISS*:=2758.45N/08142.30Wr  N1RCN  EL97", "source": "N1RCN",
         "destination": "APRS", "path": ["RS0ISS*"], "info": "=2758.45N/08142.30Wr  N1RCN  EL97"}

pytest.importorskip("aprslib")


# ------------------------------------------------------------------ APRS packets
def test_the_rs0iss_mic_e_packet_is_fully_parsed():
    a = aprsparse.parse(RS0ISS)
    assert a["parsed"] and a["error"] is None and a["format"] == "mic-e"
    pos = a["position"]
    assert pos["placeholder"] and pos["note"] == "placeholder position (no GPS fix)"
    assert abs(pos["latitude"]) < 1 and abs(pos["longitude"]) < 1
    assert a["mic_e"] == {"message_bits": "010", "code": "M5", "status": "Special"}
    assert a["device"]["vendor"] == "Kenwood" and a["device"]["model"] == "TM-D710"
    assert a["comment"] == "ARISS-International Space Station"           # the ]…= radio code is not part of it
    assert a["symbol"] == {"table": "I", "code": "S"}
    assert a["speed_kmh"] == 0 and a["course_deg"] == 0


def test_packet_times_come_from_the_file_name_and_the_offset():
    [p] = aprsparse.enrich([RS0ISS], RECORDING)
    assert p["time_utc"] == "2026-11-24T18:14:50.319000+00:00"
    assert p["tnc2"] == RS0ISS["tnc2"] and p["info"] == RS0ISS["info"]  # the raw line keeps Direwolf's <0xNN> form
    assert aprsparse.recording_start("no-time-here.cu8") is None


def test_a_real_position_is_not_a_placeholder():
    a = aprsparse.parse(N1RCN)
    assert a["format"] == "uncompressed" and not a["position"]["placeholder"] and a["position"]["note"] is None
    assert a["position"]["latitude"] == pytest.approx(27.974167, abs=1e-5)
    assert a["position"]["longitude"] == pytest.approx(-81.705, abs=1e-5)
    assert a["symbol"] == {"table": "/", "code": "r"} and a["comment"] == "N1RCN  EL97"


def test_a_packet_aprslib_cannot_parse_is_kept_raw():
    junk = {"at_s": 1.0, "tnc2": "N0CALL>APRS:", "source": "N0CALL", "destination": "APRS", "path": [], "info": ""}
    out = aprsparse.enrich([junk, RS0ISS], RECORDING)
    assert [p["tnc2"] for p in out] == [junk["tnc2"], RS0ISS["tnc2"]]   # never dropped, order kept
    assert out[0]["aprs"]["parsed"] is False and out[0]["aprs"]["error"]
    assert out[1]["aprs"]["parsed"] is True


@pytest.mark.parametrize("comment,dti,model,rest", [
    (">hello=", "`", "TH-D72", "hello"), (">hello", "`", "TH-D7A", "hello"), ("]x", "`", "TM-D700", "x"),
    ("hi_%", "`", "FTM-400DR", "hi"), ("hi|3", "'", "TinyTrack3", "hi"), ("plain", "`", None, "plain")])
def test_mic_e_radio_codes(comment, dti, model, rest):
    dev, left = aprsparse.mic_e_device(comment, dti)
    assert (dev["model"] if dev else None) == model and left == rest


def test_the_backfill_adds_fields_without_touching_the_packets(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("bf", pathlib.Path(__file__).parents[1] / "scripts/backfill_aprs_packets.py")
    bf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bf)
    f = tmp_path / "processing" / "m1" / "p1" / "aprs.json"
    f.parent.mkdir(parents=True)
    old = {"recording": RECORDING, "target_frequency_hz": 437825000, "decoder": "direwolf atest -B 1200", "packets": [RS0ISS]}
    f.write_text(json.dumps(old))
    assert bf.main([str(f)]) == 0
    new = json.loads(f.read_text())
    assert {k: new[k] for k in ("recording", "target_frequency_hz", "decoder")} == {k: old[k] for k in ("recording", "target_frequency_hz", "decoder")}
    assert {k: new["packets"][0][k] for k in RS0ISS} == RS0ISS
    assert new["packets"][0]["aprs"]["mic_e"]["status"] == "Special"
    assert bf.backfill(f, False, False, None) == "already parsed"


# ------------------------------------------------------------------ Doppler
def test_doppler_sign_and_size():
    # approaching at 7.66 km/s (the ISS's orbital speed, the bound at the horizon) raises 437.825 MHz by 11.19 kHz
    assert fmaudio.doppler_hz(-7660.0, 437_825_000) == pytest.approx(7660 / 299_792_458 * 437_825_000)
    assert float(fmaudio.doppler_hz(-7660.0, 437_825_000)) == pytest.approx(11_186.6, abs=0.5)
    assert float(fmaudio.doppler_hz(7660.0, 437_825_000)) < 0                  # receding: heard lower
    assert float(fmaudio.doppler_hz(0.0, 437_825_000)) == 0


@pytest.fixture(scope="module")
def iss_pass():
    """An overhead ISS pass predicted from the shipped sample elements, for a station near Charlotte."""
    els = predict.parse_tle(predict.SAMPLE_TLE.read_text(), "sample")
    e = els[ISS]
    passes = predict.predict_passes(els, [ISS], 35.48, -80.83, 230, e.epoch, 36, min_elevation_deg=45)
    assert passes, "no high ISS pass in the sample elements' first 36 hours"
    return e, passes[0]


def test_the_doppler_curve_of_a_pass(iss_pass):
    e, p = iss_pass
    rec = fmaudio.elements_record(e)
    aos = dt.datetime.fromisoformat(p["aos"])
    t, d = fmaudio.doppler_curve(rec, 35.48, -80.83, 230, aos, p["duration_s"], 437_825_000)
    assert len(t) == len(d) and t[0] == 0 and t[1] == 1 and t[-1] >= p["duration_s"]   # every second of the pass
    assert 9_000 < d[0] < 11_200 and -11_200 < d[-1] < -9_000           # about ±10 kHz at 437 MHz, as heard on air
    assert np.all(np.diff(d) < 0)                                     # always falling over a pass
    tca = (dt.datetime.fromisoformat(p["max_at"]) - aos).total_seconds()
    zero = float(np.interp(0.0, d[::-1], t[::-1]))
    assert abs(zero - tca) < 5                                        # no shift at closest approach (peak elevation)
    # the same as an independent calculation: minus the time derivative of the station-satellite distance
    from skyfield.api import load, wgs84
    ts = load.timescale(builtin=True)
    sat, obs = e.satellite(ts), wgs84.latlon(35.48, -80.83, elevation_m=230)
    for s in (30.0, tca, p["duration_s"] - 30.0):
        r = [(sat - obs).at(ts.from_datetime(aos + dt.timedelta(seconds=s + h))).distance().m for h in (-0.5, 0.5)]
        expect = -(r[1] - r[0]) / 299_792_458 * 437_825_000
        assert float(np.interp(s, t, d)) == pytest.approx(expect, abs=15)


def test_the_mixer_removes_a_doppler_ramp_across_chunks():
    """A tone following the Doppler curve comes out at 0 Hz with a steady phase, whatever the chunk sizes."""
    rate, n = 48_000, 48_000 * 6
    curve = (np.arange(0, 8.0), np.linspace(3_000, -3_000, 8))
    f = 1_000 + np.interp(np.arange(n) / rate, *curve)
    x = np.exp(1j * 2 * np.pi * np.cumsum(f) / rate).astype(np.complex64)
    m = fmaudio.Mixer(rate, 1_000, curve)
    rng = np.random.default_rng(1)
    out, i = [], 0
    while i < n:
        k = int(rng.integers(500, 30_000))
        out.append(m(x[i:i + k]))
        i += k
    y = np.concatenate(out)
    resid = np.angle(y[1:] * np.conj(y[:-1])) * rate / (2 * np.pi)
    assert np.max(np.abs(resid)) < 1.0                               # Hz: the ramp is gone, no jumps at chunk edges


def test_the_streaming_resampler_matches_one_long_filter():
    from scipy.signal import firwin, upfirdn
    rng = np.random.default_rng(2)
    x = (rng.standard_normal(20_000) + 1j * rng.standard_normal(20_000)).astype(np.complex64)
    h = firwin(301, 6_000, fs=204_800 * 15)
    ref = (upfirdn(h * 15, x, 15, 64)).astype(np.complex64)
    rs = fmaudio.Resampler(h, 15, 64)
    parts, i = [], 0
    for k in [1, 7, 999, 64, 3_000, 5, 10_000, 20_000]:
        parts.append(rs(x[i:i + k]))
        i += k
    y = np.concatenate(parts)
    want = ref[: len(y)]
    assert len(y) == (len(x) - 1) * 15 // 64 + 1
    assert np.max(np.abs(y - want)) < 1e-4


def test_activity_segments_merge_close_bursts_and_drop_blips():
    snr = np.full(200, -10.0)
    snr[20:40] = 15          # 2 s
    snr[45:50] = 12          # 0.5 s later: merged with the first
    snr[100:102] = 20        # 0.2 s: too short
    snr[150:160] = 7         # opens at 6 dB
    snr[160:165] = 4         # stays open above 3 dB
    segs = fmaudio.segments_from_snr(snr)
    assert segs == [{"start_s": 2.0, "end_s": 5.0, "peak_snr_db": 15.0}, {"start_s": 15.0, "end_s": 16.5, "peak_snr_db": 7.0}]
    assert fmaudio.padded_spans(segs, 18.0) == [(1.5, 5.5), (14.5, 17.0)]


# ------------------------------------------------------------------ the DSP on a synthetic pass
RATE = 1_024_000
CENTER = 437_300_000
CH = 437_550_000                           # +250 kHz


def synth(path: pathlib.Path, seconds: float, bursts: list[tuple[float, float]], doppler: tuple[float, float],
          other: float | None = None) -> None:
    """cu8 IQ: an FM channel at CH with a 1 kHz tone (±3 kHz deviation) during each burst, drifting linearly from
    doppler[0] to doppler[1]; optionally a second FM carrier always on (a spur) at `other`."""
    n = int(seconds * RATE)
    rng = np.random.default_rng(3)
    ph = ph2 = 0.0
    with path.open("wb") as fo:
        for a in range(0, n, 1 << 18):
            t = np.arange(a, min(n, a + (1 << 18))) / RATE
            on = np.zeros(len(t), bool)
            for b0, b1 in bursts:
                on |= (t >= b0) & (t < b1)
            f = CH - CENTER + doppler[0] + (doppler[1] - doppler[0]) * t / seconds + 3_000 * np.sin(2 * np.pi * 1_000 * t)
            p = ph + 2 * np.pi * np.cumsum(f) / RATE
            ph = float(p[-1])
            iq = 0.25 * on * np.exp(1j * p) + 0.02 * (rng.standard_normal(len(t)) + 1j * rng.standard_normal(len(t)))
            if other is not None:
                p2 = ph2 + 2 * np.pi * (other - CENTER) * np.arange(1, len(t) + 1) / RATE
                ph2 = float(p2[-1])
                iq = iq + 0.25 * np.exp(1j * p2)
            inter = np.empty(2 * len(t))
            inter[0::2], inter[1::2] = iq.real, iq.imag
            fo.write(np.clip(inter * 128 + 127.4, 0, 255).astype(np.uint8).tobytes())


def tone_level(wav: pathlib.Path, a: float, b: float, hz: float = 1_000) -> float:
    with wave.open(str(wav)) as w:
        fr = w.getframerate()
        w.setpos(int(a * fr))
        x = np.frombuffer(w.readframes(int((b - a) * fr)), "<i2").astype(float) / 32768
    s = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    f = np.fft.rfftfreq(len(x), 1 / fr)
    return float(s[np.argmin(np.abs(f - hz))] / (np.median(s) + 1e-12))


def test_a_doppler_drifting_channel_becomes_clean_audio_with_its_segments(tmp_path):
    """An ISS-like ±10 kHz ramp: with the Doppler curve every burst is found where it is and its tone is clean;
    without it the first burst (about +8 kHz off, half outside the 12.5 kHz channel) comes out distorted."""
    src = tmp_path / "ISS_OBS-R07_20261124T180816Z_437300000_1024000.cu8"
    synth(src, 12.0, [(0.5, 2.0), (5.0, 7.0)], (10_000, -10_000))
    curve = (np.arange(0, 14.0), 10_000 - 20_000 * np.arange(0, 14.0) / 12.0)
    job = {"key": "t", "path": str(src), "format": "cu8", "sample_rate_hz": RATE, "center_frequency_hz": CENTER,
           "frequency_hz": CH, "rates": fmaudio.plan_rates(RATE), "wav": str(tmp_path / "t.wav"),
           "doppler": {"t_s": curve[0].tolist(), "shift_hz": curve[1].tolist()}}
    r = fmaudio.extract_track(job)
    assert r["audio_seconds"] == pytest.approx(12.0, abs=0.01)
    [s1, s2] = r["segments"]
    assert s1["start_s"] == pytest.approx(0.5, abs=0.15) and s1["end_s"] == pytest.approx(2.0, abs=0.15)
    assert s2["start_s"] == pytest.approx(5.0, abs=0.15) and s2["end_s"] == pytest.approx(7.0, abs=0.15)
    assert s1["peak_snr_db"] > 15 and r["active_s"] == pytest.approx(3.5, abs=0.3)
    assert tone_level(tmp_path / "t.wav", 0.7, 1.8) > 100 and tone_level(tmp_path / "t.wav", 5.2, 6.8) > 100
    plain = fmaudio.extract_track({**job, "doppler": None, "key": "u", "wav": str(tmp_path / "u.wav")})
    assert tone_level(tmp_path / "t.wav", 0.7, 1.8) > 3 * tone_level(tmp_path / "u.wav", 0.7, 1.8)    # measured 6.6x
    assert plain["segments"][0]["peak_snr_db"] < s1["peak_snr_db"]


def test_the_band_scan_finds_a_downlink_but_not_a_spur(tmp_path):
    src = tmp_path / "x_20261124T180816Z_.cu8"
    bursts = [(s, s + 4) for s in range(2, 60, 10)]
    synth(src, 62.0, bursts, (10_000, -10_000), other=CENTER + 120_000)
    t = np.arange(0, 64.0)
    found = fmaudio.scan_band(src, "cu8", RATE, CENTER, (t, 10_000 - 20_000 * t / 62.0), [])
    hz = [c["frequency_hz"] for c in found["candidates"]]
    assert any(abs(h - CH) < 1_500 for h in hz), found
    assert not any(abs(h - (CENTER + 120_000)) < 5_000 for h in hz)     # always on: a spur, not traffic
    sat = next(c for c in found["candidates"] if abs(c["frequency_hz"] - CH) < 1_500)
    assert sat["frame"] == "satellite"
    # a known channel is not "found" again
    assert not fmaudio.scan_band(src, "cu8", RATE, CENTER, (t, 10_000 - 20_000 * t / 62.0), [CH])["candidates"]


def test_the_plan_uses_the_booked_downlinks_and_tracks_doppler(iss_pass):
    e, _ = iss_pass
    rec = {"spec": {"target": {"type": "satellite", "norad_id": ISS},
                    "radio": {"target_frequency_hz": 437_550_000},
                    "metadata": {"downlink": {"hz": 437_550_000, "id": "sstv-437", "mode": "SSTV (FM)"},
                                 "downlinks": [{"hz": 437_550_000, "id": "sstv-437", "mode": "SSTV (FM)"},
                                               {"hz": 437_800_000, "id": "repeater-437", "mode": "repeater (FM voice)"},
                                               {"hz": 437_825_000, "id": "aprs-437", "mode": "APRS (FM)"}]}},
           "record_start": "2026-11-24T18:08:16+00:00"}
    first = {"path": RECORDING, "format": "cu8", "sample_rate_hz": 2_048_000, "center_frequency_hz": 437_294_000}
    r = fmaudio.plan(rec, first, {}, ISS, e, (35.48, -80.83, 230.0), [436_000_000])
    assert [(t["frequency_hz"], t["source"]) for t in r["tracks"]] == [(437_550_000, "target"), (437_800_000, "known"),
                                                                       (437_825_000, "known")]  # 436.000 is out of band
    assert r["doppler"]["enabled"] and r["doppler"]["start"] == "2026-11-24T18:08:16+00:00"
    assert r["doppler"]["elements"]["norad"] == ISS
    assert not fmaudio.plan(rec, first, {"doppler": False}, ISS, e, (35.48, -80.83, 230.0), [])["doppler"]["enabled"]
    nowhere = fmaudio.plan(rec, first, {}, ISS, e, None, [])["doppler"]
    assert not nowhere["enabled"] and "station location" in nowhere["why"]
    with pytest.raises(ValueError, match="Doppler tracking is not possible"):
        fmaudio.plan(rec, first, {"doppler": True}, ISS, e, None, [])
    with pytest.raises(ValueError, match="does not take"):
        fmaudio.plan(rec, first, {"volume": 11}, ISS, e, None, [])


# ------------------------------------------------------------------ after a recording: the audio attempt
class SynthHw(FakeRunnerHw):
    """The receiver 'records' a prepared IQ file."""

    def __init__(self, src: pathlib.Path):
        super().__init__()
        self.src = src

    def spawn(self, argv, log):
        run = json.loads(pathlib.Path(argv[-1]).read_text())
        raw = pathlib.Path(run["raw"])
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(self.src.read_bytes())
        pathlib.Path(run["result"]).write_text(json.dumps({"status": "recorded", "bytes": raw.stat().st_size,
                                                           "error": None, "actual_start": run["record_start"]}))
        return FakeHandle(4242)


def test_an_fm_pass_gets_listenable_audio_after_recording(env, tmp_path, monkeypatch):
    from aobs.scheduling import schedview
    from aobs.scheduling.archive import outcome
    from aobs.scheduling.catalog import Catalog
    from test_rule_scheduler import save
    save(env.s, "observatory", latitude=35.48, longitude=-80.83, elevation_m=230)
    src = tmp_path / "src.cu8"
    synth(src, 12.0, [(2.0, 5.0)], (0, 0))
    form = {"name": "ISS audio", "norad": ISS, "receiver_id": "OBS-R02", "start": (NOW + dt.timedelta(minutes=60)).isoformat(),
            "end": (NOW + dt.timedelta(minutes=62)).isoformat(), "timezone": "UTC", "frequency_hz": CH,
            "sample_rate_hz": RATE, "decode": False, "pre_roll_s": 60, "post_roll_s": 60}
    job = schedview.job_from_form(env.s, form)
    job["radio"]["center_frequency_hz"] = CENTER          # as the synthetic file was made
    jid, runner = run_job(env, SynthHw(src), job)
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    atts = runner.proc.attempts(jid)
    au = next(a for a in atts if a["kind"] == "audio")
    assert au["status"] == "completed", au["status_reason"]
    assert au["origin"] == "post-recording" and au["resolved"]["doppler"]["enabled"]
    kinds = [x["kind"] for x in runner.proc.artifacts(jid, attempt_id=au["attempt_id"])]
    assert kinds.count("audio") >= 3                                 # WAV, trimmed WAV (+ Opus copies with ffmpeg)
    rec = env.s.get(jid)
    assert rec["status"] == "completed"
    o = outcome(rec, atts)
    assert o["audio"]["tracks"] >= 1 and o["audio"]["active_s"] > 0
    assert (rec["result"]["postprocess"]["audio"]["tracks"][0]["segments"] or [None])[0] is not None
    d = Catalog(env.s, runner.proc).detail(jid)
    t = next(t for t in d["audio"]["tracks"] if t["frequency_hz"] == CH)
    assert t["artifacts"]["wav"] and t["timeline"]["snr_db"] and t["segments"][0]["start_s"] == pytest.approx(2.0, abs=0.2)


def test_audio_is_not_extracted_when_the_booking_said_no(env):
    from aobs.scheduling.processing import Processor
    p = Processor(env.s)
    fm = {"spec": {"metadata": {"downlinks": [{"hz": 1, "mode": "repeater (FM voice)", "pipeline": None}]},
                   "postprocess": {"generate_audio": False}, "resolved": {"defaults_applied": ["postprocess.generate_audio=false"]}}}
    assert p.wants_audio(fm)                                         # defaulted: an FM downlink gets audio
    said_no = {"spec": {**fm["spec"], "resolved": {"defaults_applied": []}}}
    assert not p.wants_audio(said_no)
    meteor = {"spec": {"metadata": {"downlink": {"hz": 1, "mode": "LRPT", "pipeline": "meteor_m2-x_lrpt"}},
                       "postprocess": {}}, "target": "METEOR-M2 3"}
    assert not p.wants_audio(meteor)
    assert p.wants_audio({"spec": {"postprocess": {"generate_audio": True}}})


# ------------------------------------------------------------------ scripts/backfill_audio.py, queue mode
def _backfill_script() -> Any:
    import importlib.util
    spec = importlib.util.spec_from_file_location("backfill_audio", pathlib.Path(__file__).parents[1] / "scripts/backfill_audio.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_audio_backfill_queues_through_the_database(env, tmp_path, monkeypatch, capsys):
    """The queue path against a real SQLite database (scheduler rows read as mappings: plain rows broke row_dict on
    the installed broker), a recording whose audio extraction was cancelled, and the dry run / --apply / rerun cycle."""
    from aobs import cli, config
    from aobs.scheduling import schedview
    form = {"name": "ISS audio", "norad": ISS, "receiver_id": "OBS-R02", "start": (NOW + dt.timedelta(minutes=60)).isoformat(),
            "end": (NOW + dt.timedelta(minutes=62)).isoformat(), "timezone": "UTC", "frequency_hz": CH,
            "sample_rate_hz": RATE, "decode": False, "pre_roll_s": 60, "post_roll_s": 60}
    job = schedview.job_from_form(env.s, form)
    job["radio"]["center_frequency_hz"] = CENTER
    src = tmp_path / "src.cu8"
    synth(src, 2.0, [(0.5, 1.5)], (0, 0))
    jid, runner = run_job(env, SynthHw(src), job)
    first = next(a for a in runner.proc.attempts(jid) if a["kind"] == "audio")       # queued after the recording
    runner.proc.cancel(first["attempt_id"])
    assert str(env.cfg.database_url).startswith("sqlite:///")
    monkeypatch.setattr(config, "load_config", lambda path=None: env.cfg)
    monkeypatch.setattr(cli, "_engine", lambda cfg: env.s.engine)
    bf = _backfill_script()

    assert bf.main(["--config", "unused.toml"]) == 0                                  # dry run: lists, queues nothing
    out = capsys.readouterr().out
    assert f"would queue  {jid}" in out and "1 to queue (dry run" in out
    assert [a["status"] for a in runner.proc.attempts(jid) if a["kind"] == "audio"] == ["cancelled"]

    assert bf.main(["--config", "unused.toml", "--apply"]) == 0
    out = capsys.readouterr().out
    audio = [a for a in runner.proc.attempts(jid) if a["kind"] == "audio"]
    assert [a["status"] for a in audio] == ["cancelled", "queued"] and audio[1]["origin"] == "backfill"
    assert audio[1]["resolved"]["tracks"][0]["frequency_hz"] == CH and f"queued {audio[1]['attempt_id']}" in out

    assert bf.main(["--config", "unused.toml", "--apply", "--recording", jid]) == 0    # queued already: not twice
    assert "has audio already, or it is queued" in capsys.readouterr().out
    assert len([a for a in runner.proc.attempts(jid) if a["kind"] == "audio"]) == 2
