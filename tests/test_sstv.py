"""SSTV decoding (sstvdec): FM audio from raw IQ, every SSTV image of a pass, ISS routing and the processing outcome.

The signal is synthetic: Robot36 images made with pysstv, FM-modulated into IQ with a Doppler ramp of
+10 kHz to -10 kHz (an ISS pass at 437 MHz), noise added (about 16 dB carrier-to-noise in the ±20 kHz channel),
and stored as the receivers write it (cu8 / cs16)."""

from __future__ import annotations

import datetime as dt
import pathlib
import wave
from typing import Any

import numpy as np
import pytest

pytest.importorskip("pysstv")
pytest.importorskip("sstv")

from aobs.satellites import predict  # noqa: E402
from aobs.scheduling import schedview, sstvdec  # noqa: E402
from aobs.scheduling.decoders import mode_key  # noqa: E402
from aobs.scheduling.store import SchedulerError  # noqa: E402
from test_scheduler import NOW, FakeHandle, FakeRunnerHw, drain, env, run_job  # noqa: E402,F401

ISS = 25544
TARGET = 437_550_000
RATE = 240_000                        # an RTL-SDR rate; the booking puts the center rate/8 below the target
AUDIO = 12_000                        # pysstv's sample rate (tones are 1.1-2.3 kHz)
COLORS = [(255, 0, 0), (0, 0, 255)]


def robot36(color: tuple[int, int, int]) -> np.ndarray:
    from PIL import Image
    from pysstv.color import Robot36
    im = Image.new("RGB", (320, 240), color)
    for y in range(0, 240, 40):                         # a checkerboard, so the image has structure
        for x in range(0, 320, 40):
            if (x // 40 + y // 40) % 2:
                im.paste((255, 255, 255), (x, y, x + 40, y + 40))
    return np.fromiter(Robot36(im, AUDIO, 16).gen_samples(), dtype=np.float32) / 32767


def synth_pass(path: pathlib.Path, fmt: str, colors: list[tuple[int, int, int]], gap_s: float = 8.0,
               deviation_hz: float = 5_000, doppler_hz: float = 10_000, seed: int = 0) -> float:
    """Write an FM SSTV pass as raw IQ centered rate/8 below TARGET. Returns its length in seconds."""
    pieces = [np.zeros(int(3 * AUDIO), np.float32)]
    for c in colors:
        pieces += [robot36(c), np.zeros(int(gap_s * AUDIO), np.float32)]
    audio = np.concatenate(pieces)
    n = int(len(audio) / AUDIO * RATE)
    rng = np.random.default_rng(seed)
    phase, chunk = 0.0, 1 << 20
    with path.open("wb") as fo:
        for a in range(0, n, chunk):
            t = np.arange(a, min(n, a + chunk)) / RATE
            f = RATE // 8 + doppler_hz * (1 - 2 * t / (n / RATE)) + deviation_hz * np.interp(t * AUDIO, np.arange(len(audio)), audio)
            ph = phase + 2 * np.pi * np.cumsum(f) / RATE
            phase = float(ph[-1])
            iq = 0.3 * np.exp(1j * ph) + 0.08 * (rng.standard_normal(len(t)) + 1j * rng.standard_normal(len(t)))
            inter = np.empty(2 * len(t), np.float64)
            inter[0::2], inter[1::2] = iq.real, iq.imag
            if fmt == "cu8":
                fo.write(np.clip(inter * 128 + 127.4, 0, 255).astype(np.uint8).tobytes())
            else:
                fo.write(np.clip(inter * 32767, -32768, 32767).astype("<i2").tobytes())
    return n / RATE


@pytest.fixture(scope="module")
def recs(tmp_path_factory):
    d = tmp_path_factory.mktemp("sstv")
    out = {}
    for fmt in ("cu8", "cs16"):
        out[fmt] = d / f"pass.{fmt}"
        out[fmt + "_s"] = synth_pass(out[fmt], fmt, COLORS)
    out["noise"] = d / "noise.cu8"
    synth_pass(out["noise"], "cu8", [], gap_s=20, deviation_hz=0)            # a carrier and noise, no SSTV
    return out


def inp(path: pathlib.Path, fmt: str) -> dict[str, Any]:
    return {"path": str(path), "format": fmt, "sample_rate_hz": RATE, "center_frequency_hz": TARGET - RATE // 8}


def dominant(path: str) -> str:
    from PIL import Image
    px = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32).reshape(-1, 3)
    colored = px[px.min(axis=1) < 200]                  # leave out the white squares
    return "rgb"[int(np.argmax(colored.mean(axis=0)))]


# ------------------------------------------------------------------ the DSP and the decoder
@pytest.mark.parametrize("fmt", ["cu8", "cs16"])
def test_every_transmission_of_a_doppler_shifted_pass_is_decoded(recs, tmp_path, fmt):
    r, arts = sstvdec.decode(inp(recs[fmt], fmt), {"target_frequency_hz": TARGET}, tmp_path, "")
    assert r["decoded"] and r["transmissions"] == 2 and r["images"] == 2 and r["modes"] == ["Robot 36"]
    imgs = [a for a in arts if a["kind"] == "image"]
    assert [dominant(a["path"]) for a in imgs] == ["r", "b"]          # both images, in order, with their colors
    assert all(c["width"] == 320 and c["height"] == 240 and c["complete"] for c in r["image_checks"])
    wav = next(a for a in arts if a["kind"] == "audio")
    with wave.open(wav["path"]) as w:                                  # 48 kHz mono 16-bit, as long as the pass
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (48_000, 1, 2)
        assert abs(w.getnframes() / 48_000 - recs[fmt + "_s"]) < 0.1
    plan = r["channel_plan"]
    assert plan["frequency_offset_hz"] == RATE // 8 and plan["channel_half_bandwidth_hz"] == 20_000
    assert plan["de_emphasis"] is False and plan["audio_sample_rate_hz"] == 48_000


def test_no_sstv_in_the_pass_is_a_normal_empty_result(recs, tmp_path):
    r, arts = sstvdec.decode(inp(recs["noise"], "cu8"), {"target_frequency_hz": TARGET}, tmp_path, "")
    assert not r["decoded"] and r["images"] == 0 and r["note"] == sstvdec.NO_IMAGES and r["errors"] == []
    assert [a["kind"] for a in arts] == ["audio"]                      # the WAV is kept either way


def test_a_time_slice_decodes_only_the_transmission_inside_it(recs, tmp_path):
    r, _ = sstvdec.decode(inp(recs["cs16"], "cs16"), {"target_frequency_hz": TARGET, "start_s": 40.0}, tmp_path, "")
    assert r["images"] == 1


def test_the_streaming_decimator_matches_one_long_filter():
    from scipy.signal import firwin, lfilter
    x = (np.random.default_rng(1).standard_normal(50_001) * (1 + 1j)).astype(np.complex64)
    h = firwin(77, 0.05)
    for m in (1, 4, 13):
        d = sstvdec.Decimator(h, m)
        y = np.concatenate([d(x[a:b]) for a, b in ((0, 999), (999, 1000), (1000, 31_337), (31_337, 50_001))])
        assert np.allclose(y, lfilter(h, 1.0, x)[::m], atol=1e-5)


def test_the_channel_must_fit_in_the_recording():
    with pytest.raises(ValueError, match="not inside the recorded band"):
        sstvdec.channel_plan(240_000, TARGET - 110_000, TARGET)
    plan = sstvdec.channel_plan(2_048_000, TARGET - 256_000, TARGET)
    assert plan["decimation"] == 20 and plan["resample"] == [15, 32]          # 102.4 kS/s -> 48 kS/s


# ------------------------------------------------------------------ routing: the ISS uses this decoder
def test_the_iss_is_an_sstv_satellite():
    assert mode_key(predict.CATALOG[ISS]["mode"]) == "SSTV"
    assert predict.CATALOG[ISS]["pipeline"] == "sstv" and predict.CATALOG[ISS]["downlink_hz"] == TARGET


@pytest.fixture
def sstv_env(env):
    env.s.pipelines = env.s.pipelines | {"sstv"}          # as the broker does when the sstv package is installed
    return env


def test_the_sstv_profile_is_created_once_even_on_a_seeded_database(sstv_env):
    store = sstv_env.s.profiles
    store.seed()
    pid = store.seed_sstv()
    p = store.get(pid)
    assert p["pipeline"] == "sstv" and p["satellites"] == [ISS] and p["modes"] == ["SSTV"] and p["installed"]
    assert store.seed_sstv() is None
    store.delete(pid, p["revision"], "owner")
    assert store.seed_sstv() is None                                       # a deleted built-in stays deleted
    with pytest.raises(SchedulerError, match="takes no fine-search"):
        store.create({"name": "x", "pipeline": "sstv", "satellites": [ISS], "search_offsets_hz": [0]}, "owner")


def test_the_pass_form_decodes_the_iss_with_sstv_and_tunes_off_the_target(sstv_env, monkeypatch):
    from test_rule_scheduler import Passes, save
    save(sstv_env.s, "observatory", latitude=45.0, longitude=10.0, elevation_m=100)
    save(sstv_env.s, "satellites", selected=[ISS], min_elevation_deg=10)
    Passes(monkeypatch, [(ISS, 120, 10, 60)])
    p = next(x for x in __import__("aobs.satellites.passes", fromlist=["PassService"]).PassService(sstv_env.s)
             .predicted(48)["passes"] if x["norad"] == ISS)
    f = schedview.form_for_pass(sstv_env.s, ISS, p["aos"], "OBS-R02")
    assert f["pipeline"] == "sstv" and f["frequency_hz"] == TARGET
    res = schedview.book(sstv_env.s, {**f, "decode": True, "pipeline": None}, "owner")
    spec = sstv_env.s.get(res["job_id"])["spec"]
    assert spec["satdump"]["pipeline"] == "sstv" and spec["satdump"]["enabled"]
    assert spec["radio"]["center_frequency_hz"] == TARGET - spec["radio"]["sample_rate_hz"] // 8


@pytest.mark.parametrize("rx,rate", [("OBS-R02", None), ("OBS-R02", 2_048_000), ("RSP1B", None), ("RSP1B", 6_000_000)])
def test_manual_recordings_tune_rate_over_8_below_the_target(sstv_env, rx, rate):
    """The DC spike of the receiver sits at the tuned center; the target must not (as rules.py does)."""
    start = NOW + dt.timedelta(hours=3, minutes=len(rx) + (rate or 0) // 1_000_000)
    form = {"name": "freq", "receiver_id": rx, "start": start.isoformat(), "end": (start + dt.timedelta(minutes=1)).isoformat(),
            "timezone": "UTC", "frequency_hz": TARGET, "sample_rate_hz": rate}
    res = schedview.book(sstv_env.s, form, "owner")
    radio = sstv_env.s.get(res["job_id"])["spec"]["radio"]
    off = TARGET - radio["center_frequency_hz"]
    assert radio["target_frequency_hz"] == TARGET and (rate is None or radio["sample_rate_hz"] == rate)
    if rx == "RSP1B" and rate == 6_000_000:
        # rate/8 (750 kHz) would put the target outside the SDRplay's 600 kHz analog bandwidth: tuned as far off
        # as that bandwidth allows instead (this booking used to be refused)
        assert radio["bandwidth_hz"] == 600_000 and off == 270_000 - 15_000
    else:
        assert off == radio["sample_rate_hz"] // 8


# ------------------------------------------------------------------ processing: history, outcome, Slack
class SynthHw(FakeRunnerHw):
    """The receiver 'records' a prepared IQ file."""

    def __init__(self, src: pathlib.Path):
        super().__init__()
        self.src = src

    def spawn(self, argv, log):
        import json
        run = json.loads(pathlib.Path(argv[-1]).read_text())
        raw = pathlib.Path(run["raw"])
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(self.src.read_bytes())
        pathlib.Path(run["result"]).write_text(json.dumps({"status": "recorded", "bytes": raw.stat().st_size,
                                                           "error": None, "actual_start": run["record_start"]}))
        return FakeHandle(4242)


def iss_job(env: Any) -> dict[str, Any]:
    form = {"name": "ISS SSTV", "norad": ISS, "receiver_id": "OBS-R02", "start": (NOW + dt.timedelta(minutes=60)).isoformat(),
            "end": (NOW + dt.timedelta(minutes=62)).isoformat(), "timezone": "UTC", "frequency_hz": TARGET,
            "sample_rate_hz": RATE, "decode": True, "pre_roll_s": 60, "post_roll_s": 60}
    return schedview.job_from_form(env.s, form)


def run_iss(env: Any, src: pathlib.Path) -> tuple[str, Any]:
    jid, runner = run_job(env, SynthHw(src), iss_job(env))
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    return jid, runner


def test_a_recorded_iss_pass_gets_its_sstv_images_in_history(sstv_env, recs):
    from aobs.scheduling.archive import outcome
    from aobs.scheduling.calendar import decode_note
    jid, runner = run_iss(sstv_env, recs["cu8"])
    dec = next(a for a in runner.proc.attempts(jid) if a["kind"] == "decode")
    assert dec["status"] == "completed" and dec["resolved"]["pipeline"] == "sstv", dec["status_reason"]
    kinds = [a["kind"] for a in runner.proc.artifacts(jid, attempt_id=dec["attempt_id"])]
    assert kinds.count("image") == 2 and kinds.count("audio") == 1
    job = sstv_env.s.get(jid)
    assert job["status"] == "completed" and "SSTV decoded 2 image(s)" in job["status_reason"]
    o = outcome(job, runner.proc.attempts(jid))
    assert (o["images"], o["frames"], o["frames_label"], o["level"]) == (2, 2, "SSTV transmissions", "image")
    assert decode_note({"decode": dec}) == "2 SSTV image(s) ✓"
    from aobs.scheduling.calendar import _decode_line
    assert _decode_line({"decode": dec}) == "Decode: SSTV · 2 image(s)"


def test_no_sstv_event_is_a_completed_recording_not_a_failed_decode(sstv_env, recs):
    from aobs.scheduling.archive import outcome
    jid, runner = run_iss(sstv_env, recs["noise"])
    dec = next(a for a in runner.proc.attempts(jid) if a["kind"] == "decode")
    assert dec["status"] == "completed" and dec["status_reason"].startswith(sstvdec.NO_IMAGES)
    assert runner.proc.reclassify_decodes() == 0                          # restart housekeeping leaves it alone
    job = sstv_env.s.get(jid)
    assert job["status"] == "completed" and "no SSTV images (normal" in job["status_reason"]
    assert "problems" not in job["status_reason"]
    o = outcome(job, runner.proc.attempts(jid))
    assert o["images"] == 0 and o["decode"] == "no images" and o["signal"] != "not detected"


def test_sstv_decode_requests_are_checked(sstv_env, recs):
    jid, runner = run_iss(sstv_env, recs["noise"])
    with pytest.raises(SchedulerError, match="does not take"):
        runner.proc.submit(jid, "decode", {"pipeline": "sstv", "search_offsets_hz": [0]})
    with pytest.raises(SchedulerError, match="not inside the recorded band"):
        runner.proc.submit(jid, "decode", {"pipeline": "sstv", "target_frequency_hz": TARGET + 200_000})
    sstv_env.s.pipelines = sstv_env.s.pipelines - {"sstv"}
    with pytest.raises(SchedulerError, match="not installed"):
        runner.proc.submit(jid, "decode", {"pipeline": "sstv"})


def test_decoded_sstv_images_go_to_slack(sstv_env, recs):
    from test_slack import enable, service
    sv, fake = service(sstv_env)
    enable(sstv_env, notify_completed=False, notify_receiver_offline=False, notify_storage=False)
    sv._init_state()
    run_iss(sstv_env, recs["cu8"])
    posted = sv.tick()
    assert any("ISS" in p and "2 SSTV image(s)" in p for p in posted), posted
    assert [m for m, *_ in fake.calls].count("files.getUploadURLExternal") == 2
