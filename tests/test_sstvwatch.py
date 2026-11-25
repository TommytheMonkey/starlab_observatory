"""The SSTV watch: synthetic SSTV (pysstv, an encoder only) on SSB IQ with noise, through the watch's own demodulator,
VIS detection, decoder (sstv-py, with the mode the VIS names), quality threshold, archive, Slack batching, lease
handling and ISS import; and the broker's side (the sstv notification, its Slack upload, the queue priority)."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import threading
import types
from typing import Any

import numpy as np
import pytest

pysstv = pytest.importorskip("pysstv")
pytest.importorskip("sstv")

from PIL import Image  # noqa: E402

from aobs.sstvwatch import config as cfgmod  # noqa: E402
from aobs.sstvwatch.decodeworker import decode_job, run_decode  # noqa: E402
from aobs.sstvwatch.demod import ChannelDemod  # noqa: E402
from aobs.sstvwatch.detect import Detector  # noqa: E402
from aobs.sstvwatch.notify import Notifier  # noqa: E402
from aobs.sstvwatch.plan import Planner  # noqa: E402
from aobs.sstvwatch.quality import image_quality  # noqa: E402
from aobs.sstvwatch.watch import TUNE_BELOW_HZ, Station  # noqa: E402

AUDIO = 12_000
RATE = 96_000                     # receiver rate in tests (production: 768 kS/s); any multiple of AUDIO works
UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 24, 17, 0, tzinfo=UTC)          # 12:00 EST: daytime
NIGHT = dt.datetime(2026, 11, 25, 3, 0, tzinfo=UTC)        # 22:00 EST


def picture(w: int, h: int, noise: bool = False) -> Image.Image:
    """A test card (gradients and blocks) or, with noise, random pixels: an SSTV transmission of noise."""
    if noise:
        return Image.fromarray(np.random.default_rng(7).integers(0, 256, (h, w, 3), dtype=np.uint8))
    y, x = np.mgrid[0:h, 0:w]
    r = (x * 255 // max(1, w - 1)).astype(np.uint8)
    g = (y * 255 // max(1, h - 1)).astype(np.uint8)
    b = (((x // 40 + y // 40) % 2) * 200 + 30).astype(np.uint8)
    return Image.fromarray(np.stack([r, g, b], -1))


def sstv_audio(mode: str, noise_picture: bool = False) -> np.ndarray:
    from pysstv import color
    cls = {"Robot 36": color.Robot36, "Martin 1": color.MartinM1, "Scottie 1": color.ScottieS1, "PD 120": color.PD120}[mode]
    s = cls(picture(cls.WIDTH, cls.HEIGHT, noise_picture), AUDIO, 16)
    return np.fromiter(s.gen_samples(), dtype=np.float32) / 32767


def ssb_iq(audio: np.ndarray, sideband: str = "usb", snr_db: float = 20.0, lead_s: float = 3.0, tail_s: float = 8.0,
           seed: int = 0) -> np.ndarray:
    """`audio` transmitted on USB/LSB TUNE_BELOW_HZ above the receiver's centre, at RATE, in white noise."""
    from scipy.signal import hilbert, resample_poly
    a = np.concatenate([np.zeros(int(lead_s * AUDIO)), audio, np.zeros(int(tail_s * AUDIO))])
    up = resample_poly(a, RATE // AUDIO, 1)
    z = hilbert(up)                                    # analytic: the upper sideband
    if sideband == "lsb":
        z = np.conj(z)
    t = np.arange(len(z)) / RATE
    sig = z * np.exp(2j * np.pi * TUNE_BELOW_HZ * t)
    p = np.mean(np.abs(sig[int(lead_s * RATE): int(lead_s * RATE) + len(audio) * (RATE // AUDIO)]) ** 2)
    # noise power in the 2.7 kHz SSB bandwidth set by snr_db; spread over the RATE bandwidth
    n0 = p / 10 ** (snr_db / 10) * RATE / 2_700
    rng = np.random.default_rng(seed)
    noise = (rng.standard_normal(len(z)) + 1j * rng.standard_normal(len(z))) * np.sqrt(n0 / 2)
    return (0.1 * (sig + noise)).astype(np.complex64)


def transmissions(iq: np.ndarray, sideband: str = "usb") -> list[Any]:
    demod = ChannelDemod(RATE, TUNE_BELOW_HZ, sideband, AUDIO)
    det = Detector(AUDIO)
    out = []
    for i in range(0, len(iq), 8192):
        base, audio = demod(iq[i:i + 8192])
        out += det.feed(base, audio, T0 + dt.timedelta(seconds=i / RATE))
    tail = det.flush("end of test signal")
    return out + ([tail] if tail else [])


def settings(tmp: pathlib.Path, **over: Any) -> cfgmod.Settings:
    s = cfgmod.Settings(data_dir=str(tmp / "sstv"), broker_token_file=str(tmp / "token"),
                        watchers=[{"receiver": "OBS-R10", "sample_rate_hz": RATE}], **over)
    s.validate()
    return s


class Sent(list):
    def __call__(self, text: str, data: dict[str, Any]) -> None:
        self.append((text, data))


def inline(job: dict[str, Any], _d: pathlib.Path) -> dict[str, Any]:
    return decode_job(job)


# ------------------------------------------------------------------ detection and decode, by mode
@pytest.mark.parametrize("mode", ["Robot 36", "Martin 1", "Scottie 1", "PD 120"])
def test_each_mode_is_detected_on_usb_and_decoded_with_the_mode_its_vis_names(mode, tmp_path):
    txs = transmissions(ssb_iq(sstv_audio(mode)))
    assert [t.mode for t in txs] == [mode] and txs[0].truncated is None
    assert abs((txs[0].start - (T0 + dt.timedelta(seconds=3))).total_seconds()) < 0.1
    st = Station(settings(tmp_path), send=Sent(), decoder=inline, iss_roots=())
    iid = st.submit(txs[0], st.s.channels[0], "OBS-R10")
    st.finish(iid, st.decoder(st.jobs.get_nowait()[1], tmp_path))
    row = st.store.get(iid)
    assert row["status"] == "decoded", row["error"]
    assert row["decoded_mode"] == mode and row["extra"]["mode_agrees"]
    assert row["quality"] > 0.7 and row["snr_db"] > 10
    for k in ("image_path", "thumb_path", "audio_path", "iq_path"):
        assert pathlib.Path(row[k]).is_file(), k
    assert np.fromfile(row["iq_path"], np.complex64).size == pytest.approx(len(txs[0].audio), abs=1)


def test_80m_lsb_is_detected_and_decoded(tmp_path):
    txs = transmissions(ssb_iq(sstv_audio("Robot 36"), "lsb"), "lsb")
    assert [t.mode for t in txs] == ["Robot 36"]
    res = decode_job(_job(tmp_path, txs[0]))
    assert res["status"] == "decoded" and res["quality"] > 0.7


def test_the_wrong_sideband_hears_nothing():
    """LSB audio demodulated as USB is inverted tones: no valid VIS header."""
    assert transmissions(ssb_iq(sstv_audio("Robot 36"), "lsb"), "usb") == []


def test_noise_and_afsk_are_not_transmissions():
    rng = np.random.default_rng(3)
    noise = (rng.standard_normal(RATE * 20) + 1j * rng.standard_normal(RATE * 20)).astype(np.complex64) * 0.05
    assert transmissions(noise) == []
    bits = rng.integers(0, 2, 1200 * 15)                     # 15 s of AFSK 1200 (APRS-like): 1200/2200 Hz
    f = np.repeat(np.where(bits, 1200.0, 2200.0), AUDIO // 1200)
    afsk = 0.5 * np.sin(2 * np.pi * np.cumsum(f) / AUDIO).astype(np.float32)
    assert transmissions(ssb_iq(afsk)) == []


def _job(tmp: pathlib.Path, tx: Any, **over: Any) -> dict[str, Any]:
    import wave
    wav = tmp / "tx.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO)
        w.writeframes((np.clip(tx.audio, -1, 1) * 32767).astype("<i2").tobytes())
    return {"wav": str(wav), "out_dir": str(tmp), "stem": "tx", "vis_mode": tx.mode, "min_quality": 0.35, **over}


def test_an_image_of_noise_is_filed_as_noise_not_kept(tmp_path):
    """A valid header followed by a picture that is mostly noise: below min_quality, so not an image."""
    txs = transmissions(ssb_iq(sstv_audio("Robot 36", noise_picture=True)))
    assert len(txs) == 1
    res = decode_job(_job(tmp_path, txs[0]))
    assert res["status"] == "noise" and res["quality"] < 0.35 and "mostly noise" in res["error"]
    assert not (tmp_path / "tx.png").exists()


def test_quality_score_separates_pictures_from_noise_and_blank_frames():
    assert image_quality(picture(320, 240))["score"] > 0.9
    assert image_quality(picture(320, 240, noise=True))["score"] < 0.1
    assert image_quality(Image.new("RGB", (320, 240), (40, 40, 40)))["score"] == 0.0


# ------------------------------------------------------------------ the decode worker's protections
def _fake_python(tmp: pathlib.Path, body: str) -> str:
    p = tmp / "fakepython"
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(0o755)
    return str(p)


def test_a_decode_that_runs_too_long_is_killed_and_failed(tmp_path):
    job = {"wav": "x.wav", "out_dir": str(tmp_path), "stem": "slow"}
    # a worker that keeps its heartbeat but never finishes
    py = _fake_python(tmp_path, f'while true; do date > "{tmp_path}/slow.job.heartbeat"; sleep 0.2; done')
    res = run_decode(job, tmp_path, timeout_s=1.5, stale_s=30, python=py, poll_s=0.1, grace_s=1)
    assert res["status"] == "failed" and "timed out" in res["error"]


def test_a_silent_decode_worker_is_stopped_by_the_heartbeat_watchdog(tmp_path):
    job = {"wav": "x.wav", "out_dir": str(tmp_path), "stem": "stuck"}
    py = _fake_python(tmp_path, "sleep 30")
    res = run_decode(job, tmp_path, timeout_s=60, stale_s=1.0, python=py, poll_s=0.1, grace_s=1)
    assert res["status"] == "failed" and "no heartbeat" in res["error"]


def test_the_real_worker_process_decodes(tmp_path):
    txs = transmissions(ssb_iq(sstv_audio("Robot 36")))
    res = run_decode(_job(tmp_path, txs[0]), tmp_path, timeout_s=120, stale_s=30)
    assert res["status"] == "decoded" and res["decoded_mode"] == "Robot 36"


# ------------------------------------------------------------------ where it listens
def test_20m_is_heard_most_and_longest_by_day_and_80m_only_at_night(tmp_path):
    s = settings(tmp_path)
    p = Planner(s)
    day = [p.next(T0) for _ in range(40)]
    share = {c.name: sum(d.seconds for d in day if d.channel.name == c.name) for c in s.channels}
    assert max(share, key=share.get) == "20m" and share["80m"] == 0
    assert day[0].channel.name == "20m" and max(d.seconds for d in day) == day[0].seconds
    night = [p.next(NIGHT) for _ in range(40)]
    names = {d.channel.name for d in night}
    assert "80m" in names and "10m" not in names
    ns = {n: sum(d.seconds for d in night if d.channel.name == n) for n in names}
    assert ns["40m"] > ns["15m"] and ns["80m"] > ns["15m"]


def test_two_watchers_never_share_a_channel(tmp_path):
    s = settings(tmp_path)
    p = Planner(s)
    for _ in range(20):
        a = p.next(T0)
        b = p.next(T0, busy={a.channel.name})
        assert a.channel.name != b.channel.name


# ------------------------------------------------------------------ Slack
def img(i: int, q: float = 0.8) -> dict[str, Any]:
    return {"id": i, "start_utc": "2026-11-24T17:00:00Z", "freq_hz": 14_230_000, "sideband": "usb", "band": "20m",
            "mode": "Robot 36", "receiver": "OBS-R10", "quality": q, "snr_db": 18.0, "thumb_path": f"/x/{i}.jpg"}


def test_more_than_five_decodes_in_ten_minutes_are_batched(tmp_path):
    clock = types.SimpleNamespace(now=T0)
    sent = Sent()
    n = Notifier(sent, 600, 5, "America/New_York", clock=lambda: clock.now)
    results = []
    for i in range(8):
        results.append(n.add(img(i, 0.5 + i / 20)))
        clock.now += dt.timedelta(seconds=30)
    assert results == ["sent"] * 5 + ["batched"] * 3 and len(sent) == 5
    text, data = sent[0]
    assert "14.230 MHz USB (20m) · Robot 36 · 2026-11-24 12:00 EST" in text and data["images"][0]["path"] == "/x/0.jpg"
    assert not n.tick()                                       # the window is still open
    clock.now += dt.timedelta(minutes=10)
    assert n.tick() and len(sent) == 6
    text, data = sent[-1]
    assert data["batch"] and data["ids"] == [5, 6, 7] and "3 more images" in text
    assert [i["path"] for i in data["images"]][0] == "/x/7.jpg"          # best first
    assert n.add(img(9)) == "sent"                            # the window has moved on


# ------------------------------------------------------------------ the daemon loop
class FakeKeeper:
    def __init__(self, lose_after: float | None = None):
        self.lost = threading.Event()
        self.lost_reason = ""
        self.token = None
        self.expires_at = None
        self.acquired = self.released = 0

    @property
    def held(self) -> bool:
        return self.token is not None and not self.lost.is_set()

    def acquire(self) -> dict[str, Any]:
        self.acquired += 1
        self.token = "t"
        return {}

    def release(self) -> None:
        self.released += 1
        self.token = None


class FakeSource:
    """Streams prepared IQ, then (optionally) loses the lease or stops the station when it runs out."""

    def __init__(self, iq: np.ndarray, at_end: Any):
        self.rate, self.iq, self.at_end, self.pos, self.tuned = RATE, iq, at_end, 0, []

    def tune(self, hz: int) -> None:
        self.tuned.append(hz)

    def read(self) -> np.ndarray:
        if self.pos >= len(self.iq):
            self.at_end()
            return np.zeros(8192, np.complex64)
        b = self.iq[self.pos:self.pos + 8192]
        self.pos += len(b)
        return b

    def close(self) -> None:
        pass


def test_the_watch_hears_a_transmission_files_it_and_posts_it(tmp_path):
    sent = Sent()
    keeper = FakeKeeper()
    holder: dict[str, Any] = {}

    def stop() -> None:
        holder["st"].stopping.set()

    iq = ssb_iq(sstv_audio("Robot 36"))
    src = FakeSource(iq, stop)
    st = Station(settings(tmp_path, dwell_s=60, min_dwell_s=60), keeper=lambda w: keeper, source=lambda w: src,
                 send=sent, decoder=inline, clock=lambda: T0, iss_roots=())
    holder["st"] = st
    w = st.watchers[0]
    w.cycle()                                                 # one lease: listen until the station stops
    st.jobs.put(None)
    st.decode_loop()
    assert src.tuned[0] == 14_230_000 - TUNE_BELOW_HZ         # 20 m first, by day
    assert keeper.acquired == keeper.released == 1
    rows = st.store.images()
    assert [r["mode"] for r in rows] == ["Robot 36"] and rows[0]["receiver"] == "OBS-R10" and rows[0]["band"] == "20m"
    assert len(sent) == 1 and "14.230 MHz USB" in sent[0][0] and pathlib.Path(sent[0][1]["images"][0]["path"]).is_file()
    assert rows[0]["notified_at"]


def test_losing_the_lease_to_a_booking_stops_the_radio_and_keeps_what_was_heard(tmp_path):
    keeper = FakeKeeper()
    audio = sstv_audio("Martin 1")
    iq = ssb_iq(audio[: AUDIO * 40], tail_s=0)                # the booking starts 40 s into a 114 s image

    def lose() -> None:
        keeper.lost_reason = "lease ends in 10 s (scheduled job ahead)"
        keeper.lost.set()

    st = Station(settings(tmp_path, retry_lease_s=0), keeper=lambda w: keeper, source=lambda w: FakeSource(iq, lose),
                 send=Sent(), decoder=inline, clock=lambda: T0, iss_roots=())
    w = st.watchers[0]
    w.cycle()
    assert keeper.released == 1 and w.state == "waiting" and "back after the booking" in w.detail
    assert st.on == {}                                        # the channel is free for another watcher
    iid, job = st.jobs.get_nowait()
    assert "lease lost" in (st.store.get(iid)["extra"]["truncated"] or "")


def test_a_release_stops_the_watch(tmp_path):
    from aobs.sstvwatch.watch import write_control
    s = settings(tmp_path)
    st = Station(s, keeper=lambda w: FakeKeeper(), source=lambda w: None, send=Sent(), iss_roots=())
    write_control(s, released=True, until=None)
    st.watchers[0].cycle()
    assert st.watchers[0].state == "released"


# ------------------------------------------------------------------ ISS images from the broker
def _iss_result(root: pathlib.Path) -> pathlib.Path:
    d = root / "iss-437mhz-x3-20261115-0525" / "processing" / "m1" / "p1"
    d.mkdir(parents=True)
    png = d / "sstv_01_Robot36.png"
    picture(320, 240).save(png)
    (d / "attempt.json").write_text(json.dumps({"attempt_id": "p1", "kind": "decode",
        "params": {"pipeline": "sstv", "target_frequency_hz": 437_550_000},
        "inputs": [{"receiver_id": "OBS-R07", "record_start": "2026-11-15T05:25:05+00:00", "recording_id": "m1",
                    "target": "ISS", "path": str(root / "raw.cu8")}]}))
    (d / "result.json").write_text(json.dumps({"inputs": [{"decoder": "sstv", "image_checks": [
        {"file": png.name, "mode": "Robot 36"}]}], "artifacts": [{"kind": "image", "path": str(png),
                                                                  "label": "SSTV image 1 (Robot 36)"}]}))
    return png


def test_iss_pass_images_are_filed_once_with_a_link_to_the_recording_and_not_posted_again(tmp_path):
    root = tmp_path / "jobs"
    png = _iss_result(root)
    sent = Sent()
    st = Station(settings(tmp_path), send=sent, iss_roots=(str(root),))
    assert st.import_iss() == 1 and st.import_iss() == 0
    r = st.store.images(source="iss")[0]
    assert (r["freq_hz"], r["band"], r["mode"], r["receiver"], r["recording_id"]) == \
        (437_550_000, "70cm", "Robot 36", "OBS-R07", "m1")
    assert r["image_path"] == str(png) and pathlib.Path(r["thumb_path"]).is_file() and r["quality"] > 0.9
    assert sent == []                                         # the broker posts ISS images itself
    from aobs.sstvwatch.control import public
    assert public(r)["recording_url"] == "/rf#recording=m1"


def test_the_control_endpoint_serves_the_gallery_and_files_only_from_records(tmp_path):
    import urllib.request
    from aobs.sstvwatch.control import ControlServer
    s = settings(tmp_path)
    st = Station(s, send=Sent(), iss_roots=())
    picture(64, 64).save(s.path("a.png"))
    iid = st.store.add(source="watch", status="decoded", start_utc="2026-11-24T17:00:00Z", freq_hz=14_230_000,
                       band="20m", mode="Robot 36", quality=0.9, image_path=str(s.path("a.png")),
                       thumb_path="/etc/passwd")
    srv = ControlServer(s, "tok", host="127.0.0.1", port=0)
    srv.start()
    assert srv.bound.wait(5)
    base = f"http://127.0.0.1:{srv.port}/v1/sstvwatch"

    def get(path: str, token: str = "tok") -> tuple[int, bytes]:
        req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    try:
        assert get("/images", "bad")[0] == 401
        code, body = get("/images?band=20m")
        imgs = json.loads(body)["images"]
        assert code == 200 and [i["id"] for i in imgs] == [iid] and imgs[0]["image_url"].endswith(f"/{iid}/image")
        assert "image_path" not in imgs[0]
        assert get(f"/images/{iid}/image")[1][:4] == b"\x89PNG"
        assert get(f"/images/{iid}/thumb")[0] == 404           # a path outside the allowed roots is never served
        assert json.loads(get("/facets")[1])["bands"] == ["20m"]
    finally:
        srv.stop()


def test_settings_file_is_checked(tmp_path):
    p = tmp_path / "w.toml"
    p.write_text('[sstvwatch]\nnope = 1\n')
    with pytest.raises(ValueError, match="unknown"):
        cfgmod.load(p)
    p.write_text('[sstvwatch]\n[[sstvwatch.channels]]\nname = "x"\nhz = 14230000\nmode = "am"\n')
    with pytest.raises(ValueError, match="mode must be one of"):
        cfgmod.load(p)
    assert cfgmod.load(tmp_path / "missing.toml").watchers[0].receiver == "OBS-R10"
