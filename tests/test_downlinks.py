"""Several downlinks per satellite: the ISS catalog, the on/off switches in Settings, one Upcoming-passes row per
active downlink with the receiver whose antenna covers it, one pass on two receivers at once, APRS decoding with
Direwolf, and SSTV decoding that reads the mode instead of assuming Robot36."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import shutil
import subprocess
import types
import wave
from typing import Any

import numpy as np
import pytest

from aobs.db import init_db, make_engine
from aobs.satellites import predict
from aobs.satellites.passes import PassService
from aobs.scheduling import aprsdec, rflayout, schedview
from aobs.scheduling.store import Scheduler, SchedulerError
from aobs.settings.store import SettingsError, validate
from test_receiver_serial import add_r07
from test_rule_scheduler import Passes, save
from test_rxsuggest import layout
from test_scheduler import NOW, Clock, drain, run_job, sched_config

ISS = 25544
RATE = 240_000                       # an RTL-SDR rate; bookings tune rate/8 below the target
ON = {"25544:aprs-145": True, "25544:aprs-437": True, "25544:repeater-437": True}
UHF = "sstv-437+repeater-437+aprs-437"          # three active 437 MHz downlinks: one capture


# ------------------------------------------------------------------ the catalog
def test_the_iss_has_five_downlinks_and_keeps_its_primary_one():
    got = {d["id"]: (d["hz"], d["pipeline"]) for d in predict.downlinks(ISS)}
    assert got == {"sstv-437": (437_550_000, "sstv"), "aprs-437": (437_825_000, "aprs"),
                   "repeater-437": (437_800_000, None), "voice-145": (145_800_000, "sstv"),
                   "aprs-145": (145_825_000, "aprs")}
    iss = predict.CATALOG[ISS]          # rules and older bookings still read the primary downlink
    assert (iss["downlink_hz"], iss["pipeline"]) == (437_550_000, "sstv")
    assert [d["id"] for d in predict.downlinks(ISS) if d["active"]] == ["sstv-437"]   # nothing changes until switched
    assert predict.downlink_for(ISS, 437_826_500)["id"] == "aprs-437"
    assert predict.downlink_for(ISS, 437_801_000)["id"] == "repeater-437"
    assert predict.downlink_for(ISS, 146_500_000) is None
    assert predict.downlinks(57166)[0]["id"] == "lrpt"


def test_downlink_switches_are_validated():
    ok, errs = validate("satellites", {"selected": [ISS], "downlinks": {"25544:aprs-145": True, "25544:sstv-437": False}})
    assert not errs and ok["downlinks"] == {"25544:aprs-145": True, "25544:sstv-437": False}
    _, errs = validate("satellites", {"selected": [ISS], "downlinks": {"25544:aprs-2m": True}})
    assert any("unknown downlink(s) 25544:aprs-2m" in e for e in errs)
    _, errs = validate("satellites", {"selected": [ISS], "downlinks": {"25544:aprs-145": "yes"}})
    assert any("true (on) or false (off)" in e for e in errs)
    assert validate("satellites", {"selected": [ISS]})[0]["downlinks"] == {}       # older documents: defaults


# ------------------------------------------------------------------ the station: R07 on 70 cm, R01 on 2 m
@pytest.fixture
def env(tmp_config, tmp_path, monkeypatch):
    cfg = sched_config(tmp_config, tmp_path)
    r01 = cfg.raw["scheduler"]["receivers"]["OBS-R01"]
    for k in ("auto", "legacy_unit", "legacy_kind", "legacy_output"):
        r01.pop(k)
    r01["mode"] = "broker"
    cfg.raw["scheduler"]["receivers"]["OBS-R02"].pop("auto")
    engine = make_engine(cfg.database_url)
    init_db(engine)
    clock = Clock()
    present = {"OBS-R01", "OBS-R02", "OBS-R05", "240526D360"}
    s = Scheduler(cfg, engine, clock, disk_free=lambda _: 500 * 10**9, present=lambda rx: rx.serial in present,
                  satdump_pipelines=frozenset({"meteor_m2-x_lrpt", "sstv", "aprs"}))
    e = types.SimpleNamespace(s=s, clock=clock, present=present, cfg=cfg, tmp=tmp_path, engine=engine)
    add_r07(e, serial="OBS-R07")
    save(s, "observatory", latitude=45.0, longitude=10.0, elevation_m=100)
    save(s, "satellites", selected=[ISS], min_elevation_deg=10, downlinks=ON)
    s.rf = rflayout.RfLayout(s)
    s.rf.load()
    layout(s.rf, {"UHF-YAGI": (430, 440), "VHF-YAGI": (144, 148), "DISCONE": (25, 1300)},
           chains={"OBS-R07": ["UHF-YAGI"], "OBS-R01": ["VHF-YAGI"], "OBS-R02": ["DISCONE"], "RSP1B": []})
    Passes(monkeypatch, [(ISS, 120, 10, 60), (ISS, 220, 10, 30)])
    return e


def rows(env: Any) -> list[dict[str, Any]]:
    return PassService(env.s).predicted(48)["passes"]


def test_upcoming_passes_has_one_row_per_capture(env):
    got = PassService(env.s).predicted(48)
    first = [(p["downlink"], p["downlink_hz"]) for p in got["passes"] if p["aos"] == got["passes"][0]["aos"]]
    assert first == [(UHF, 437_550_000), ("aprs-145", 145_825_000)]
    assert [d["hz"] for d in got["passes"][0]["downlinks"]] == [437_550_000, 437_800_000, 437_825_000]
    assert len(got["passes"]) == 4                                          # two passes x two captures
    assert any("145.800 MHz Voice" in x for x in got["inactive_downlinks"])
    save(env.s, "satellites", downlinks={"25544:sstv-437": False, "25544:aprs-437": True})   # an ARISS change
    assert [p["downlink"] for p in rows(env)][:2] == ["aprs-437", "aprs-437"]
    save(env.s, "satellites", downlinks={**ON, "25544:voice-145": True})                    # 2 m pair: one capture
    assert [p["downlink"] for p in rows(env)][:2] == [UHF, "voice-145+aprs-145"]


@pytest.mark.parametrize("downlink,rx", [(UHF, "OBS-R07"), ("aprs-145", "OBS-R01")])
def test_the_receiver_whose_antenna_covers_the_downlink_is_ticked(env, downlink, rx):
    p = rows(env)[0]
    f = schedview.form_for_pass(env.s, ISS, p["aos"], None, downlink)
    assert f["receiver_ids"] == [rx], f["suggest"]
    assert f["frequency_hz"] == predict.downlink_group(ISS, downlink)[0]["hz"] and f["downlink"] == downlink
    assert "narrowest" in f["suggest"]["reasons"][rx]["reason"]            # the discone on R02 covers it too
    assert f["decode"] is True                                             # both captures carry APRS


def test_several_downlinks_record_as_one_capture(env):
    p = rows(env)[0]
    f = schedview.form_for_pass(env.s, ISS, p["aos"], None, UHF)
    assert f["sample_rate_hz"] == 2_048_000 and f["name"].startswith("iss-437mhz-x3-")
    res = schedview.book(env.s, {**f, "receiver_ids": ["OBS-R07"]}, "owner")
    spec = env.s.get(res["job_id"])["spec"]
    assert spec["radio"]["center_frequency_hz"] == 437_294_000          # 437.550 MHz - 2.048 MS/s / 8
    assert [d["id"] for d in spec["metadata"]["downlinks"]] == ["sstv-437", "repeater-437", "aprs-437"]
    assert (spec["satdump"]["pipeline"], spec["satdump"]["frequency_hz"]) == ("sstv", 437_550_000)
    assert [(r["downlink"], [b["receiver_id"] for b in r["booked"]]) for r in rows(env) if r["aos"] == p["aos"]] == \
        [(UHF, ["OBS-R07"]), ("aprs-145", [])]
    save(env.s, "satellites", downlinks={})          # only 437.550 on: booking it alone is a duplicate of the group
    one = schedview.form_for_pass(env.s, ISS, p["aos"], None, "sstv-437")
    with pytest.raises(SchedulerError, match="already being recorded"):
        schedview.book(env.s, {**one, "receiver_ids": ["OBS-R02"]}, "owner")


def test_a_capture_that_cannot_hold_every_downlink_is_refused(env):
    p = rows(env)[0]
    f = schedview.form_for_pass(env.s, ISS, p["aos"], "RSP1B", UHF)      # SDRplay: 600 kHz analog bandwidth
    with pytest.raises(SchedulerError, match="do not fit in one recording on RSP1B"):
        schedview.book(env.s, f, "owner")


def test_one_pass_books_on_both_receivers_at_once(env):
    p = rows(env)[0]
    uhf = schedview.form_for_pass(env.s, ISS, p["aos"], None, UHF)
    vhf = schedview.form_for_pass(env.s, ISS, p["aos"], None, "aprs-145")
    a = schedview.book(env.s, {**uhf, "receiver_ids": ["OBS-R07"]}, "owner")
    b = schedview.book(env.s, {**vhf, "receiver_ids": ["OBS-R01"]}, "owner")     # not a duplicate of the 437 one
    assert not b["duplicates"]
    by = {r["downlink"]: [x["receiver_id"] for x in r["booked"]] for r in rows(env) if r["aos"] == p["aos"]}
    assert by == {UHF: ["OBS-R07"], "aprs-145": ["OBS-R01"]}
    spec = env.s.get(b["job_id"])["spec"]
    assert spec["metadata"]["downlink"]["id"] == "aprs-145" and spec["radio"]["target_frequency_hz"] == 145_825_000
    assert spec["satdump"]["pipeline"] == "aprs" and spec["satdump"]["enabled"]            # APRS decodes by default
    assert "downlinks" not in spec["metadata"]                                             # a single downlink
    assert len(env.s.get(a["job_id"])["spec"]["metadata"]["downlinks"]) == 3
    with pytest.raises(SchedulerError, match="already being recorded"):            # the same capture again is
        schedview.book(env.s, {**uhf, "receiver_ids": ["OBS-R02"]}, "owner")


def test_a_downlink_turned_off_after_booking_keeps_its_row(env):
    p = rows(env)[0]
    schedview.book(env.s, {**schedview.form_for_pass(env.s, ISS, p["aos"], None, "aprs-145"),
                           "receiver_ids": ["OBS-R01"]}, "owner")
    save(env.s, "satellites", downlinks={})
    first = [r for r in rows(env) if r["aos"] == p["aos"]]
    assert [(r["downlink"], r["downlink_active"]) for r in first] == [("sstv-437", True), ("aprs-145", False)]
    with pytest.raises(SchedulerError, match="downlink is off"):
        schedview.form_for_pass(env.s, ISS, rows(env)[-1]["aos"], None, "aprs-145")


def test_the_simple_book_endpoint_takes_a_downlink(env):
    p = rows(env)[0]
    res = PassService(env.s).schedule(ISS, p["aos"], "OBS-R01", "owner", downlink="aprs-145")
    spec = env.s.get(res["job_id"])["spec"]
    assert spec["radio"]["target_frequency_hz"] == 145_825_000 and spec["satdump"]["pipeline"] == "aprs"
    assert spec["job_name"].startswith("iss-aprs-145-")


# ------------------------------------------------------------------ APRS (Direwolf)
direwolf = pytest.mark.skipif(not (shutil.which("atest") and shutil.which("gen_packets")),
                              reason="Direwolf (atest, gen_packets) is not installed")

ATEST = """Fix Bits level = 0
Channel 0: 1200 baud, AFSK 1200 & 2200 Hz, A, 48000 sample rate, Tx AX.25.

\x1b[38;2;0;0;0mDECODED[1] 0:00.734 WB2OSZ-15 audio level = 50(14/14)
[0] WB2OSZ-15>TEST:,The quick brown fox
DECODED[2] 1:02.5 RS0ISS audio level = 40(10/10)
[0.2] RS0ISS*>CQ,ARISS,WIDE2-1:>ARISS - International Space Station
"""


def test_atest_output_is_parsed_into_packets():
    got = aprsdec.parse_atest(ATEST)
    assert [k["at_s"] for k in got] == [0.734, 62.5]
    assert got[1] == {"at_s": 62.5, "tnc2": "RS0ISS*>CQ,ARISS,WIDE2-1:>ARISS - International Space Station",
                      "source": "RS0ISS", "destination": "CQ", "path": ["ARISS", "WIDE2-1"],
                      "info": ">ARISS - International Space Station"}


def fm_iq(path: pathlib.Path, audio: np.ndarray, audio_rate: int, deviation_hz: float, doppler_hz: float,
          lead_s: float = 2.0, seed: int = 0, rate: int = RATE, offset_hz: int | None = None) -> None:
    """FM-modulate audio (-1..1) into cu8 IQ offset_hz (default rate/8) above the tuned center, with a Doppler
    ramp and noise."""
    offset = rate // 8 if offset_hz is None else offset_hz
    audio = np.concatenate([np.zeros(int(lead_s * audio_rate)), audio, np.zeros(int(lead_s * audio_rate))])
    n = int(len(audio) / audio_rate * rate)
    rng = np.random.default_rng(seed)
    phase, chunk = 0.0, 1 << 20
    with path.open("wb") as fo:
        for a in range(0, n, chunk):
            t = np.arange(a, min(n, a + chunk)) / rate
            f = offset + doppler_hz * (1 - 2 * t / (n / rate)) + \
                deviation_hz * np.interp(t * audio_rate, np.arange(len(audio)), audio)
            ph = phase + 2 * np.pi * np.cumsum(f) / rate
            phase = float(ph[-1])
            iq = 0.3 * np.exp(1j * ph) + 0.05 * (rng.standard_normal(len(t)) + 1j * rng.standard_normal(len(t)))
            inter = np.empty(2 * len(t))
            inter[0::2], inter[1::2] = iq.real, iq.imag
            fo.write(np.clip(inter * 128 + 127.4, 0, 255).astype(np.uint8).tobytes())


def afsk(d: pathlib.Path) -> np.ndarray:
    """Direwolf's four built-in test packets as 48 kHz AFSK 1200 audio (-1..1)."""
    subprocess.run(["gen_packets", "-r", "48000", "-o", str(d / "afsk.wav")], check=True, capture_output=True)
    with wave.open(str(d / "afsk.wav")) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float64) / 32768
    return audio / np.abs(audio).max()


@pytest.fixture(scope="module")
def aprs_iq(tmp_path_factory) -> pathlib.Path:
    d = tmp_path_factory.mktemp("aprs")
    fm_iq(d / "aprs.cu8", afsk(d), 48_000, deviation_hz=3_000, doppler_hz=3_500)     # ±3.5 kHz at 145.8 MHz
    return d / "aprs.cu8"


@direwolf
def test_direwolf_decodes_the_packets_and_saves_them_with_the_recording(aprs_iq, tmp_path):
    inp = {"path": str(aprs_iq), "format": "cu8", "sample_rate_hz": RATE, "center_frequency_hz": 145_825_000 - RATE // 8}
    r, arts = aprsdec.decode(inp, {"target_frequency_hz": 145_825_000}, tmp_path, "")
    assert r["decoded"] and r["packets"] == 4 and r["stations"] == ["WB2OSZ-15"], r
    lines = (tmp_path / "aprs.txt").read_text().splitlines()
    assert len(lines) == 4 and all("WB2OSZ-15>TEST:" in ln for ln in lines)
    data = json.loads((tmp_path / "aprs.json").read_text())
    assert [k["info"].endswith(f"{i} of 4") for i, k in enumerate(data["packets"], 1)] == [True] * 4
    assert {a["kind"] for a in arts} == {"audio", "product", "log"}


@direwolf
def test_an_aprs_pass_is_decoded_after_recording(env, aprs_iq):
    from test_sstv import SynthHw
    from aobs.scheduling.archive import outcome
    form = {"name": "ISS APRS", "norad": ISS, "downlink": "aprs-145", "receiver_id": "OBS-R01",
            "start": (NOW + dt.timedelta(minutes=60)).isoformat(), "end": (NOW + dt.timedelta(minutes=62)).isoformat(),
            "timezone": "UTC", "frequency_hz": 145_825_000, "sample_rate_hz": RATE, "decode": True,
            "pre_roll_s": 60, "post_roll_s": 60}
    jid, runner = run_job(env, SynthHw(aprs_iq), schedview.job_from_form(env.s, form))
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    dec = next(a for a in runner.proc.attempts(jid) if a["kind"] == "decode")
    assert dec["status"] == "completed" and dec["resolved"]["pipeline"] == "aprs", dec["status_reason"]
    job = env.s.get(jid)
    assert job["status"] == "completed" and "APRS decoded 4 packet(s) from 1 station(s)" in job["status_reason"]
    files = [pathlib.Path(a["path"]).name for a in runner.proc.artifacts(jid, attempt_id=dec["attempt_id"])]
    assert {"aprs.txt", "aprs.json", "aprs.wav"} <= set(files)
    o = outcome(job, runner.proc.attempts(jid))
    assert (o["frames"], o["frames_label"], o["signal"]) == (4, "APRS packets", "detected")


@direwolf
def test_no_aprs_packet_is_a_completed_recording(env, tmp_path):
    from test_sstv import SynthHw
    quiet = tmp_path / "quiet.cu8"
    fm_iq(quiet, np.zeros(48_000 * 4), 48_000, deviation_hz=0, doppler_hz=3_500)
    form = {"name": "ISS APRS", "norad": ISS, "downlink": "aprs-145", "receiver_id": "OBS-R01",
            "start": (NOW + dt.timedelta(minutes=60)).isoformat(), "end": (NOW + dt.timedelta(minutes=62)).isoformat(),
            "timezone": "UTC", "frequency_hz": 145_825_000, "sample_rate_hz": RATE, "decode": True,
            "pre_roll_s": 60, "post_roll_s": 60}
    jid, runner = run_job(env, SynthHw(quiet), schedview.job_from_form(env.s, form))
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc)
    dec = next(a for a in runner.proc.attempts(jid) if a["kind"] == "decode")
    assert dec["status"] == "completed" and dec["status_reason"].startswith(aprsdec.NO_PACKETS)
    assert runner.proc.reclassify_decodes() == 0
    assert env.s.get(jid)["status"] == "completed"


@direwolf
def test_one_capture_runs_each_downlinks_decoder_on_the_same_iq(env, tmp_path):
    """The 437 MHz group at 1.024 MS/s: SSTV is decoded at 437.550 MHz and APRS at 437.825 MHz from one file."""
    from test_sstv import SynthHw
    from aobs.scheduling.archive import outcome
    rate, center = 1_024_000, 437_550_000 - 1_024_000 // 8
    src = tmp_path / "uhf.cu8"
    fm_iq(src, afsk(tmp_path), 48_000, deviation_hz=3_000, doppler_hz=10_000, rate=rate,
          offset_hz=437_825_000 - center)          # packets on 437.825; 437.550 carries no SSTV this pass
    form = {"name": "ISS 437", "norad": ISS, "downlink": UHF, "receiver_id": "OBS-R07",
            "start": (NOW + dt.timedelta(minutes=60)).isoformat(), "end": (NOW + dt.timedelta(minutes=62)).isoformat(),
            "timezone": "UTC", "frequency_hz": 437_550_000, "sample_rate_hz": rate, "decode": True,
            "pre_roll_s": 60, "post_roll_s": 60}
    job = schedview.job_from_form(env.s, form)
    assert job["radio"]["center_frequency_hz"] == center
    jid, runner = run_job(env, SynthHw(src), job)
    env.clock.now = NOW + dt.timedelta(minutes=80)
    drain(runner.proc, 40)
    decs = sorted((a["resolved"]["pipeline"], a["resolved"]["target_frequency_hz"], a["status"])
                  for a in runner.proc.attempts(jid) if a["kind"] == "decode")
    assert decs == [("aprs", 437_825_000, "completed"), ("sstv", 437_550_000, "completed")]
    reason = env.s.get(jid)["status_reason"]
    assert "no SSTV images" in reason and "APRS decoded 4 packet(s) from 1 station(s)" in reason, reason
    o = outcome(env.s.get(jid), runner.proc.attempts(jid))
    assert (o["frames"], o["images"], o["signal"]) == (4, 0, "detected")
    assert o["frames_label"] == "SSTV transmissions + APRS packets"


def test_an_unnamed_decode_of_an_aprs_recording_uses_the_aprs_decoder_not_the_iss_sstv_profile(env):
    from aobs.scheduling.processing import _downlink_pipeline
    env.s.profiles.seed_sstv()
    rec = {"spec": {"target": {"norad_id": ISS}, "radio": {"target_frequency_hz": 145_825_000}}}
    assert _downlink_pipeline(rec) == "aprs"
    assert env.s.profiles.match(rec, _downlink_pipeline(rec)) is None
    assert _downlink_pipeline({"spec": {"target": {"norad_id": ISS}, "radio": {"target_frequency_hz": 437_550_000}}}) \
        == "sstv"


def test_settings_reject_an_unknown_downlink_on_save(env):
    with pytest.raises(SettingsError):
        save(env.s, "satellites", downlinks={"25544:nope": True})


# ------------------------------------------------------------------ SSTV: the mode is read, not assumed
def test_a_pd120_image_on_145_8_is_decoded_as_pd120(tmp_path):
    pytest.importorskip("pysstv")
    pytest.importorskip("sstv")
    from PIL import Image
    from pysstv.color import PD120
    from aobs.scheduling import sstvdec
    im = Image.new("RGB", (640, 496), (0, 0, 255))
    for y in range(0, 496, 62):
        for x in range(0, 640, 80):
            if (x // 80 + y // 62) % 2:
                im.paste((255, 255, 255), (x, y, x + 80, y + 62))
    audio = np.fromiter(PD120(im, 12_000, 16).gen_samples(), dtype=np.float32) / 32767
    src = tmp_path / "pd120.cu8"
    fm_iq(src, audio, 12_000, deviation_hz=5_000, doppler_hz=3_500)
    inp = {"path": str(src), "format": "cu8", "sample_rate_hz": RATE, "center_frequency_hz": 145_800_000 - RATE // 8}
    r, _ = sstvdec.decode(inp, {"target_frequency_hz": 145_800_000}, tmp_path, "")
    assert r["images"] == 1 and r["modes"] == ["PD 120"], r
    assert r["image_checks"][0]["width"] == 640
