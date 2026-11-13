from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import textwrap
import threading
import time
from typing import Any

import numpy as np
import pytest

from aobs.piratewatch import heatmap
from aobs.piratewatch.audio import ClipState, NoiseSquelch
from aobs.piratewatch.config import Settings, load
from aobs.piratewatch.daemon import Watch, read_control, write_control
from aobs.piratewatch.lease import LeaseError, LeaseKeeper, read_token
from aobs.piratewatch.radio import RadioError, Stream, audio_command, sweep_command
from aobs.piratewatch.spectrum import Baseline, Sweep, SweepAssembler
from aobs.piratewatch.store import History, Store
from aobs.piratewatch.transcribe import Transcriber, WhisperProcess

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 11, 20, 0, 0, tzinfo=UTC)
START, STOP, STEP = 243_000_000, 270_000_000, 5_000
FREQS = START + (np.arange((STOP - START) // STEP) + 0.5) * STEP


def sweep(db: np.ndarray, i: int = 0) -> Sweep:
    return Sweep(T0 + dt.timedelta(seconds=2 * i), FREQS.copy(), db.astype(np.float32))


def noise(rng: np.random.Generator) -> np.ndarray:
    return -30 + rng.normal(0, 0.7, len(FREQS))


def put(db: np.ndarray, center_hz: float, width_hz: float, level: float) -> np.ndarray:
    sel = np.abs(FREQS - center_hz) < width_hz / 2
    db = db.copy()
    db[sel] = level
    return db


def rtl_power_rows(db: np.ndarray, at: dt.datetime, hop_bins: int = 540) -> list[str]:
    at = at.astimezone()                 # rtl_power prints local time
    rows = []
    for k in range(0, len(db), hop_bins):
        low = START + k * STEP
        vals = db[k:k + hop_bins]
        rows.append(f"{at:%Y-%m-%d}, {at:%H:%M:%S}, {low}, {low + len(vals) * STEP}, {STEP}, 1000, "
                    + ", ".join(f"{v:.2f}" for v in vals))
    return rows


# ------------------------------------------------------------ spectrum
@pytest.fixture
def new_york(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_assembler_joins_hops_into_one_sweep(new_york: None) -> None:
    db = np.linspace(-40, -20, len(FREQS))
    asm = SweepAssembler(STOP)
    out = [s for line in rtl_power_rows(db, T0) + rtl_power_rows(db, T0 + dt.timedelta(seconds=2))
           if (s := asm.feed(line))]
    assert len(out) == 2
    assert out[0].at == T0 and out[0].at.tzinfo == UTC      # local rtl_power stamps come back as UTC
    assert len(out[0].freqs) == len(FREQS)
    assert np.allclose(out[0].freqs, FREQS) and np.allclose(out[0].db, db, atol=0.01)
    assert asm.feed("garbage") is None


def test_baseline_masks_steady_carriers_and_detects_voice_bursts() -> None:
    rng = np.random.default_rng(1)
    b = Baseline(learn_sweeps=60, threshold_db=8)
    for i in range(60):
        db = put(noise(rng), 255_550_000, 25_000, -12)          # steady wide carrier
        db = put(db, 260_000_000, 5_000, -5)                    # narrow spur
        if i % 10 == 0:                                         # intermittent voice: not masked
            db = put(db, 251_950_000, 20_000, -15)
        b.add(sweep(db, i))
    assert b.complete
    ranges = b.excluded_ranges()
    assert any(lo <= 255_550_000 <= hi for lo, hi in ranges)
    assert not any(lo <= 251_950_000 <= hi for lo, hi in ranges)

    db = put(put(noise(rng), 255_550_000, 25_000, -12), 260_000_000, 5_000, -5)
    db = put(db, 251_950_000, 20_000, -15)                      # voice (20 kHz, +15 dB)
    db = put(db, 247_000_000, 45_000, -15)                      # too wide
    db = put(db, 265_000_000, 5_000, -15)                       # too narrow
    db = put(db, 268_000_000, 20_000, -25)                      # only +5 dB
    cands = b.detect(sweep(db))
    assert [round(c.center_hz, -3) for c in cands] == [251_950_000]
    c = cands[0]
    assert 15_000 <= c.width_hz <= 25_000 and c.snr_db > 8 and c.peak_db == pytest.approx(-15, abs=0.1)


def test_baseline_follows_gain_drift_and_persists(tmp_path: pathlib.Path) -> None:
    rng = np.random.default_rng(2)
    b = Baseline(learn_sweeps=30)
    for i in range(30):
        b.add(sweep(noise(rng), i))
    assert b.detect(sweep(noise(rng) + 6)) == []                # whole band 6 dB up: drift, not a signal
    b.exclude(250_000_000, 250_030_000)
    b.save(tmp_path / "b.npz")
    b2 = Baseline(learn_sweeps=30)
    assert b2.load(tmp_path / "b.npz") and b2.complete
    assert b2.detect(sweep(put(noise(rng), 250_015_000, 20_000, -15))) == []    # later exclusion kept
    assert len(b2.detect(sweep(put(noise(rng), 252_015_000, 20_000, -15)))) == 1


def test_provisional_baseline_detects_while_learning() -> None:
    rng = np.random.default_rng(3)
    b = Baseline(learn_sweeps=600, min_provisional=20)
    for i in range(20):
        b.add(sweep(noise(rng), i))
    assert not b.complete and b.ready
    assert len(b.detect(sweep(put(noise(rng), 249_000_000, 20_000, -15)))) == 1


# ------------------------------------------------------------ audio
RATE = 25_000


def discriminator(iq: np.ndarray) -> np.ndarray:
    """rtl_fm's FM output: phase step per sample scaled so +-pi = +-16384."""
    ang = np.angle(iq[1:] * np.conj(iq[:-1]))
    return np.clip(ang * 16384 / np.pi, -32768, 32767).astype(np.int16)


def fm_audio(seconds: float, snr_db: float | None, rng: np.random.Generator) -> np.ndarray:
    n = int(RATE * seconds) + 1
    nz = (rng.normal(size=n) + 1j * rng.normal(size=n)) / np.sqrt(2)
    if snr_db is None:
        return discriminator(nz)
    t = np.arange(n) / RATE
    phase = 3_000 / 1_000 * np.sin(2 * np.pi * 1_000 * t)     # 3 kHz deviation, 1 kHz tone (beta = 3)
    sig = 10 ** (snr_db / 20) * np.exp(1j * phase)
    return discriminator(sig + nz)


def test_noise_squelch_tells_carrier_from_open_channel() -> None:
    rng = np.random.default_rng(4)
    sq = NoiseSquelch(RATE, noise_ref=9_500)
    open_ch = fm_audio(1, None, rng)
    carrier = fm_audio(1, 15, rng)
    assert not any(sq.present(open_ch[i:i + sq.block])[0] for i in range(0, len(open_ch) - sq.block, sq.block))
    assert all(sq.present(carrier[i:i + sq.block])[0] for i in range(0, len(carrier) - sq.block, sq.block))


def test_noise_reference_adapts_down_to_the_receiver() -> None:
    rng = np.random.default_rng(7)
    sq = NoiseSquelch(RATE, noise_ref=14_000)          # too high: open-channel noise (~9500 here) looks "quiet"
    a = fm_audio(4, None, rng)
    flags = [sq.present(a[i:i + sq.block])[0] for i in range(0, len(a) - sq.block, sq.block)]
    assert sq.noise_ref == pytest.approx(9_500, rel=0.1)
    assert not any(flags[-10:])


def test_clip_ends_after_silence_and_flags_stuck_carriers() -> None:
    rng = np.random.default_rng(5)
    clip = ClipState(NoiseSquelch(RATE, noise_ref=9_500), silence_s=10, max_s=300)
    clip.feed(fm_audio(4, 15, rng))
    assert not clip.done and clip.first_present == 0
    clip.feed(fm_audio(9, None, rng))
    assert not clip.done
    clip.feed(fm_audio(2, None, rng))
    assert clip.done and not clip.hit_max
    assert clip.active_s == pytest.approx(4, abs=0.2)

    stuck = ClipState(NoiseSquelch(RATE, noise_ref=9_500), silence_s=10, max_s=5)
    stuck.feed(fm_audio(6, 15, rng))
    assert stuck.done and stuck.hit_max


# ------------------------------------------------------------ commands, store, heatmap, lease
def test_commands() -> None:
    s = Settings()
    assert sweep_command(s, 2) == ["rtl_power", "-f", "243000000:270000000:10000", "-i", "2", "-d", "2",
                                   "-g", "16.6", "-c", "20%", "-"]
    assert audio_command(s, 2, 251_950_000) == ["rtl_fm", "-f", "251950000", "-M", "fm", "-s", "25000", "-d", "2",
                                                "-E", "dc", "-g", "16.6", "-"]


def test_real_tools_are_refused_in_tests() -> None:
    with pytest.raises(RadioError, match="not running rtl_power"):
        Stream(["rtl_power", "-"], None, lines=True)


def test_store_history_and_heatmap(tmp_path: pathlib.Path) -> None:
    st = Store(tmp_path / "db.sqlite3")
    hid = st.add_hit(detected_at="2026-11-11T20:00:00Z", freq_hz=251_950_000, channel_hz=251_950_000, status="pending",
                     audio_path="/x.wav", peak_db=-15.0, snr_db=15.0)
    st.add_activity(251_950_000, T0, 12.5)
    st.add_activity(251_950_000, T0 + dt.timedelta(minutes=5), 3.0)
    st.add_speech(251_950_000, T0)
    assert [r["id"] for r in st.pending()] == [hid]
    act = st.activity()
    assert act == [{"channel_hz": 251_950_000, "hour_utc": "2026-11-11T20", "detections": 2, "seconds": 15.5, "speech": 1}]
    txt = heatmap.ascii(st, 7, now=T0)
    assert "251.9500" in txt
    page = heatmap.render_html(st, tmp_path / "h.html", now=T0).read_text()
    assert "251.9500" in page and "<title>Pirate Watch Activity</title>" in page

    h = History(tmp_path / "hist", days=14)
    db = noise(np.random.default_rng(6))
    h.append(sweep(db, 0))
    h.append(sweep(db, 1))
    base = next((tmp_path / "hist").glob("*.u8")).with_suffix("")
    ts, freqs, back = History.read(base)
    assert len(ts) == 2 and np.allclose(freqs, FREQS) and np.allclose(back[1], db, atol=0.26)


def test_settings_reject_unknown_keys(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "pw.toml"
    p.write_text("[piratewatch]\nantenna_port = 3\n")
    assert load(p).antenna_port == 3
    p.write_text("[piratewatch]\nantena_port = 3\n")
    with pytest.raises(ValueError, match="antena_port"):
        load(p)


def test_read_token(tmp_path: pathlib.Path) -> None:
    (tmp_path / "a").write_text("BROKER_URL=http://x\nBROKER_TOKEN='abc'\n")
    (tmp_path / "b").write_text("rawtoken\n")
    assert read_token(str(tmp_path / "a")) == "abc"
    assert read_token(str(tmp_path / "b")) == "rawtoken"


class FakeBroker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.ttl_room: float | None = None     # seconds until the next reservation
        self.revoked = False
        self.busy: str | None = None

    def _lease(self, ttl: int) -> dict[str, Any]:
        room = ttl if self.ttl_room is None else min(ttl, self.ttl_room)
        return {"token": "tok", "expires_at": (dt.datetime.now(UTC) + dt.timedelta(seconds=room)).isoformat()}

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, path))
        if method == "POST" and path == "/v1/lease":
            if self.busy:
                raise LeaseError(self.busy, 409)
            return self._lease(body["ttl_s"] if body else 120)
        if path.endswith("/renew"):
            if self.revoked:
                raise LeaseError("Unknown or expired lease token", 404)
            return self._lease(120)
        return {"released": True}


def test_lease_keeper_loses_lease_on_revoke_and_before_reservations() -> None:
    fb = FakeBroker()
    k = LeaseKeeper(fb, "OBS-R05", "pirate-watch", 3, 120, renew_every_s=3600)  # type: ignore[arg-type]
    k.acquire()
    assert k.held
    k.renew_once()
    assert k.held
    fb.revoked = True
    k.renew_once()
    assert k.lost.is_set() and "Unknown" in k.lost_reason
    k.release()
    assert ("DELETE", "/v1/lease/tok") in fb.calls

    fb2 = FakeBroker()
    fb2.ttl_room = 10
    k2 = LeaseKeeper(fb2, "OBS-R05", "pirate-watch", None, 120, renew_every_s=3600)  # type: ignore[arg-type]
    k2.acquire()
    assert k2.lost.is_set() and "scheduled job" in k2.lost_reason
    k2.release()


# ------------------------------------------------------------ end to end with mock tools
MOCK_POWER = '''#!{py}
import datetime as dt, pathlib, sys, time
import numpy as np
state = pathlib.Path({state!r})
runs = int(state.read_text()) if state.exists() else 0
state.write_text(str(runs + 1))
START, STOP, STEP = 243_000_000, 270_000_000, 5_000
freqs = START + (np.arange((STOP - START) // STEP) + 0.5) * STEP
rng = np.random.default_rng(runs)
t0 = dt.datetime(2026, 11, 11, 20, 0, 0) + dt.timedelta(minutes=10 * runs)
for i in range(400):
    db = -30 + rng.normal(0, 0.7, len(freqs))
    db[np.abs(freqs - 255_550_000) < 12_500] = -12        # steady carrier
    if runs == 0 and i == 30:
        db[np.abs(freqs - 251_950_000) < 10_000] = -15    # the pirate
    at = t0 + dt.timedelta(seconds=2 * i)
    for k in range(0, len(db), 540):
        low = START + k * STEP
        vals = db[k:k + 540]
        print(f"{{at:%Y-%m-%d}}, {{at:%H:%M:%S}}, {{low}}, {{low + len(vals) * STEP}}, {{STEP}}, 1000, "
              + ", ".join(f"{{v:.2f}}" for v in vals), flush=True)
    time.sleep(0.01)
time.sleep(60)
'''

MOCK_FM = '''#!{py}
import sys, time
import numpy as np
pathlib_args = sys.argv
open({argv_log!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")
rate = 25_000
rng = np.random.default_rng(0)
def disc(iq):
    return np.clip(np.angle(iq[1:] * np.conj(iq[:-1])) * 16384 / np.pi, -32768, 32767).astype("<i2")
def audio(seconds, snr):
    n = int(rate * seconds) + 1
    nz = (rng.normal(size=n) + 1j * rng.normal(size=n)) / np.sqrt(2)
    if snr is None:
        return disc(nz)
    t = np.arange(n) / rate
    return disc(10 ** (snr / 20) * np.exp(1j * 3 * np.sin(2 * np.pi * 1000 * t)) + nz)
out = sys.stdout.buffer
for chunk in [audio(3, 15), audio(1.5, None), audio(30, None)]:
    out.write(chunk.tobytes()); out.flush()
time.sleep(60)
'''

FAKE_WHISPER = '''import json, sys
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    print(json.dumps({"id": req["id"], "ok": True, "speech": True, "speech_prob": 0.9, "language": "pt",
                      "language_prob": 0.97, "transcript": "alô, alô, câmbio", "translation": "hello, hello, over"}),
          flush=True)
'''


def _mock(path: pathlib.Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def test_watch_end_to_end(tmp_path: pathlib.Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    _mock(tools / "rtl_power", MOCK_POWER.format(py=sys.executable, state=str(tmp_path / "power-runs")))
    _mock(tools / "rtl_fm", MOCK_FM.format(py=sys.executable, argv_log=str(tmp_path / "fm-argv")))
    (tmp_path / "fake_whisper.py").write_text(FAKE_WHISPER)
    s = Settings(data_dir=str(tmp_path / "data"), learn_sweeps=20, silence_s=1.0, tool_prefix=str(tools))
    fb = FakeBroker()
    keeper = LeaseKeeper(fb, s.receiver, s.lease_owner, None, 120, renew_every_s=3600)  # type: ignore[arg-type]
    bias_offs: list[str] = []
    watch = Watch(s, keeper=keeper, device_index=lambda _s: 0, holders=lambda _s: [],
                  bias_forced=lambda _i: False, bias_off=bias_offs.append)
    watch.transcriber = Transcriber(s, watch.store,
                                    worker=WhisperProcess(s, [sys.executable, str(tmp_path / "fake_whisper.py")]))
    th = threading.Thread(target=watch.run, daemon=True)
    th.start()
    deadline = time.monotonic() + 60
    hits: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        hits = watch.store.hits("speech")
        if hits:
            break
        time.sleep(0.2)
    # release for SDR++: the watch stops the radio and hands the lease back
    write_control(s, released=True, until=None)
    t_rel = time.monotonic()
    while time.monotonic() - t_rel < 10 and watch.state != "released":
        time.sleep(0.1)
    state_at_release = watch.state
    watch.stopping.set()
    th.join(timeout=20)

    assert hits, "no speech hit"
    h = hits[0]
    assert h["freq_hz"] == pytest.approx(251_950_000, abs=3_000)
    assert h["channel_hz"] % 5_000 == 0
    assert h["duration_s"] == pytest.approx(3.0, abs=0.3)
    assert h["language"] == "pt" and h["translation"] == "hello, hello, over"
    assert h["peak_db"] == pytest.approx(-15, abs=0.1)
    assert pathlib.Path(h["audio_path"]).exists()
    fm_args = (tmp_path / "fm-argv").read_text().split()
    assert fm_args[fm_args.index("-s") + 1] == "25000"
    assert int(fm_args[fm_args.index("-f") + 1]) == h["freq_hz"]
    # the steady carrier at 255.55 MHz was learned and never recorded
    every = Store(tmp_path / "data" / "piratewatch.sqlite3").hits(None, 100)
    assert all(abs(x["freq_hz"] - 255_550_000) > 30_000 for x in every)
    assert state_at_release == "released"
    assert ("DELETE", "/v1/lease/tok") in fb.calls
    assert int((tmp_path / "power-runs").read_text()) >= 2          # sweeping resumed after the clip
    assert list((tmp_path / "data" / "history").glob("*.u8"))
    assert "251.95" in (tmp_path / "data" / "heatmap.html").read_text()
    status = json.loads((tmp_path / "data" / "status.json").read_text())
    assert status["state"] == "stopped"
    assert bias_offs and set(bias_offs) == {"OBS-R05"}          # switched off under every lease before any radio work


def test_watch_waits_while_another_process_holds_the_dongle(tmp_path: pathlib.Path) -> None:
    s = Settings(data_dir=str(tmp_path / "d"), tool_prefix=str(tmp_path))
    fb = FakeBroker()
    keeper = LeaseKeeper(fb, s.receiver, s.lease_owner, None, 120, renew_every_s=3600)  # type: ignore[arg-type]
    watch = Watch(s, keeper=keeper, device_index=lambda _s: 0, holders=lambda _s: ["sdrpp[123]"],
                  transcriber=Transcriber(s, Store(tmp_path / "x.sqlite3"), worker=WhisperProcess(s, ["true"])))
    watch.stopping.set()          # make wait() return at once
    watch.cycle()
    assert watch.state == "waiting" and "sdrpp[123]" in watch.detail
    assert fb.calls == []          # never asked for a lease

    watch2 = Watch(s, keeper=keeper, device_index=lambda _s: 0, holders=lambda _s: [],
                   transcriber=Transcriber(s, Store(tmp_path / "y.sqlite3"), worker=WhisperProcess(s, ["true"])))
    fb.busy = "OBS-R05 is leased by obsagent until ..."
    watch2.stopping.set()
    watch2.cycle()
    assert watch2.state == "waiting" and "obsagent" in watch2.detail
    assert read_control(s) == {}


# ------------------------------------------------------------ bias tee, antenna, Slack control
def test_bias_tee_is_never_requested() -> None:
    s = Settings()
    assert "-T" not in sweep_command(s, 1) and "-T" not in audio_command(s, 1, 250_000_000)
    with pytest.raises(RadioError, match="bias tee"):
        Stream(["rtl_fm", "-f", "250000000", "-T", "-"], "/nonexistent", lines=False)


EEPROM_DUMP = """Found 3 device(s):
Using device 1: Generic RTL2832U OEM
Current configuration:
__________________________________________
Vendor ID:\t\t0x0bda
Manufacturer:\t\tNooelec
Product:\t\tNESDR SMArt v5
Serial number:\t\tOBS-R05
Bias Tee always on:\t{flag}
__________________________________________
"""


@pytest.mark.parametrize("flag,expected", [("yes", True), ("no", False)])
def test_eeprom_bias_flag_is_read(tmp_path: pathlib.Path, flag: str, expected: bool) -> None:
    from aobs.piratewatch.radio import eeprom_bias_forced
    _mock(tmp_path / "rtl_eeprom", f"#!/bin/sh\nprintf '%b' '{EEPROM_DUMP.format(flag=flag)}' >&2\n")
    assert eeprom_bias_forced(1, str(tmp_path)) is expected
    with pytest.raises(RadioError, match="not running rtl_eeprom"):
        eeprom_bias_forced(1, None)


def test_watch_refuses_to_run_while_the_eeprom_forces_the_bias_tee(tmp_path: pathlib.Path) -> None:
    s = Settings(data_dir=str(tmp_path / "d"), tool_prefix=str(tmp_path))      # no rtl_power mock: must not get that far
    fb = FakeBroker()
    keeper = LeaseKeeper(fb, s.receiver, s.lease_owner, None, 120, renew_every_s=3600)  # type: ignore[arg-type]
    offs: list[str] = []
    watch = Watch(s, keeper=keeper, device_index=lambda _s: 1, holders=lambda _s: [],
                  bias_forced=lambda _i: True, bias_off=offs.append,
                  transcriber=Transcriber(s, Store(tmp_path / "x.sqlite3"), worker=WhisperProcess(s, ["true"])))
    watch.stopping.set()
    watch.cycle()
    assert watch.state == "blocked" and "rtl_eeprom -d 1 -b 0" in watch.detail
    assert ("DELETE", "/v1/lease/tok") in fb.calls and offs == []


def test_antenna_port_direct_or_a_switch_port(tmp_path: pathlib.Path) -> None:
    assert Settings().lease_port is None                       # default: wired directly (R05 -> discone)
    assert Settings(antenna_port=None).lease_port is None
    assert Settings(antenna_port=3).lease_port == 3
    p = tmp_path / "pw.toml"
    p.write_text('[piratewatch]\nantenna_port = "direct"\n')
    assert load(p).lease_port is None
    p.write_text('[piratewatch]\nantenna_port = "discone"\n')
    with pytest.raises(ValueError, match="direct"):
        load(p)


def test_control_endpoint_releases_and_resumes(tmp_path: pathlib.Path) -> None:
    import urllib.error
    import urllib.request
    from aobs.piratewatch.control import ControlServer

    s = Settings(data_dir=str(tmp_path / "d"))
    s.root.mkdir(parents=True)
    (s.path("status.json")).write_text(json.dumps({"state": "released", "at": "2026-11-11T20:00:00Z"}))
    srv = ControlServer(s, "sekrit", host="127.0.0.1", port=0)
    srv.start()
    assert srv.bound.wait(5)
    base = f"http://127.0.0.1:{srv.port}/v1/piratewatch"

    def call(path: str, body: dict | None = None, token: str = "sekrit") -> dict:
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            call("", token="wrong")
        assert err.value.code == 401
        out = call("/release", {"minutes": 90})
        assert out["released"] and out["confirmed"] and out["released_until"]
        assert read_control(s)["released"] is True
        with pytest.raises(urllib.error.HTTPError) as err:
            call("/release", {"minutes": "lots"})
        assert err.value.code == 400
        out = call("/resume", {})
        assert out["resume_requested"] and not out["released"]
        assert call("")["state"] == "released"
    finally:
        srv.stop()


MOCK_FM_HANG = '''#!{py}
import sys, time
import numpy as np
rate = 25_000
rng = np.random.default_rng(0)
n = int(rate * 2) + 1
t = np.arange(n) / rate
nz = (rng.normal(size=n) + 1j * rng.normal(size=n)) / np.sqrt(2)
iq = 10 ** (15 / 20) * np.exp(1j * 3 * np.sin(2 * np.pi * 1000 * t)) + nz
audio = np.clip(np.angle(iq[1:] * np.conj(iq[:-1])) * 16384 / np.pi, -32768, 32767).astype("<i2")
sys.stdout.buffer.write(audio.tobytes()); sys.stdout.buffer.flush()
time.sleep(120)            # the dongle dropped off USB: rtl_fm hangs instead of exiting
'''


def test_a_hung_rtl_fm_ends_the_clip_and_frees_the_receiver(tmp_path: pathlib.Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    _mock(tools / "rtl_power", MOCK_POWER.format(py=sys.executable, state=str(tmp_path / "power-runs")))
    _mock(tools / "rtl_fm", MOCK_FM_HANG.format(py=sys.executable))
    s = Settings(data_dir=str(tmp_path / "data"), learn_sweeps=20, silence_s=30.0, stall_s=1.5, tool_prefix=str(tools))
    fb = FakeBroker()
    keeper = LeaseKeeper(fb, s.receiver, s.lease_owner, None, 120, renew_every_s=3600)  # type: ignore[arg-type]
    watch = Watch(s, keeper=keeper, device_index=lambda _s: 0, holders=lambda _s: [], bias_forced=lambda _i: False,
                  bias_off=lambda _s: None,
                  transcriber=Transcriber(s, Store(tmp_path / "x.sqlite3"), worker=WhisperProcess(s, ["true"])))
    watch.wait = lambda _s: watch.stopping.set()          # stop after the first error instead of retrying
    th = threading.Thread(target=watch.cycle, daemon=True)
    th.start()
    th.join(timeout=30)
    assert not th.is_alive(), "the watch hung with rtl_fm"
    assert watch.state == "error" and "rtl_fm gave no audio" in watch.detail
    assert ("DELETE", "/v1/lease/tok") in fb.calls
    [hit] = watch.store.hits(None, 10)
    assert hit["status"] == "pending" and pathlib.Path(hit["audio_path"]).exists()      # the audio so far is kept
    assert "cut short: rtl_fm gave no audio" in hit["note"]
    status = json.loads(s.path("status.json").read_text())
    assert status["state"] == "error" and status["recording_hz"] is None


def test_control_host_defaults_to_this_computers_tailscale_address(tmp_path, monkeypatch):
    """No address is written into the code: an empty control_host is the Tailscale IPv4, else 127.0.0.1 only."""
    from aobs.piratewatch import config as pwconfig
    cfg = tmp_path / "piratewatch.toml"
    cfg.write_text("[piratewatch]\n")
    monkeypatch.setattr(pwconfig, "tailscale_ipv4", lambda: "100.101.102.103")
    assert load(cfg).control_host == "100.101.102.103"
    monkeypatch.setattr(pwconfig, "tailscale_ipv4", lambda: None)            # Tailscale not installed or down
    assert load(cfg).control_host == "127.0.0.1"
    cfg.write_text('[piratewatch]\ncontrol_host = "10.9.8.7"\n')             # an explicit address is used as given
    assert load(cfg).control_host == "10.9.8.7"


def test_tailscale_ipv4_reads_the_first_address(monkeypatch):
    import subprocess
    from aobs.piratewatch.config import tailscale_ipv4
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "100.101.102.103\n", ""))
    assert tailscale_ipv4() == "100.101.102.103"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "not running"))
    assert tailscale_ipv4() is None
    def missing(*a, **k):
        raise FileNotFoundError("tailscale")
    monkeypatch.setattr(subprocess, "run", missing)
    assert tailscale_ipv4() is None
