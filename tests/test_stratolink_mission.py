"""Stratolink missions: the lifecycle state machine, launch / phase / landing detection on simulated flights (latex,
mylar, pico, tethered) with irregular reporting, GPS altitude noise and spikes, duplicate packets, contact loss and
daemon restarts, plus the false-launch and false-landing cases, the timeline, statistics, archive and prediction
(against a fake predictor), and compatibility with databases made before missions existed."""

from __future__ import annotations

import datetime as dt
import json
import math
import random
import sqlite3

import pytest

from aobs.stratolink import config as cfgmod
from aobs.stratolink import mission, predict
from aobs.stratolink.control import API
from aobs.stratolink.receiver import file_rxpk
from aobs.stratolink.store import Store
from test_stratolink import EUI, SL01, device_metrics, frame, position, rxpk

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 12, 5, 15, 0, tzinfo=UTC)
SITE = (35.4835, -80.8272, 230.0)


@pytest.fixture
def s(tmp_path):
    st = cfgmod.Settings(data_dir=str(tmp_path / "d"), udp_host="127.0.0.1", allowed_sources=["127.0.0.1"],
                         keys_file=str(tmp_path / "k.toml"), import_paths=[], firmware_dir=str(tmp_path / "fw"),
                         prediction_url="http://predictor.invalid/tawhiri")
    st.root.mkdir(parents=True)
    return st


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Operator actions happen on the simulated clock: it follows the latest simulated reception."""
    c = Clock(T0 - dt.timedelta(minutes=5))
    import aobs.stratolink.store as st_mod
    monkeypatch.setattr(st_mod, "now", c)
    return c


@pytest.fixture
def store(s):
    st = Store(s.db_path)
    st.register_tracker("!f661fb94", "Stratolink-01", short_name="SL01", hardware="heltec_v4")
    st.register_gateway(EUI, name="Observatory", lat=35.48, lon=-80.83, alt_m=230)
    yield st
    st.close()


class Sim:
    """Feeds fixes through the real receiver path (frame -> rxpk -> ingest) and steps the engine like the daemon."""

    def __init__(self, store, s, fid, seed=1):
        self.store, self.s, self.fid = store, s, fid
        self.engine = mission.Engine(store, 180, 600)
        self.pid, self.tmst = 0x100, 1000
        self.rng = random.Random(seed)

    def fix(self, t: dt.datetime, lat, lon, alt, speed=None, dup=False, resend=False, step=True):
        self.pid += 1
        self.tmst += 1_000_000
        raw = frame(SL01, self.pid, 3, position(lat, lon, round(alt), int(t.timestamp()), sats=12,
                                                speed=round(speed) if speed is not None else 0))
        at = t + dt.timedelta(seconds=2)
        import aobs.stratolink.store as st_mod
        st_mod.now.t = max(st_mod.now.t, at + dt.timedelta(seconds=1))
        file_rxpk(self.store, self.s, EUI, "127.0.0.1", at, rxpk(raw, tmst=self.tmst), "live", cfgmod.channels(self.s))
        if dup:       # the same frame heard again (another station, or a rebroadcast)
            file_rxpk(self.store, self.s, "2CF7F12004C10000", "127.0.0.1", at + dt.timedelta(milliseconds=300),
                      rxpk(raw, tmst=self.tmst + 77, rssi=-110), "live", cfgmod.channels(self.s))
        if resend:    # the tracker sends the same GPS fix again in a new packet
            self.pid += 1
            raw2 = frame(SL01, self.pid, 3, position(lat, lon, round(alt), int(t.timestamp()), sats=12))
            file_rxpk(self.store, self.s, EUI, "127.0.0.1", at + dt.timedelta(seconds=20),
                      rxpk(raw2, tmst=self.tmst + 20_000_000), "live", cfgmod.channels(self.s))
        if step:
            return self.step(at + dt.timedelta(seconds=1))

    def battery(self, t, level, volts):
        self.pid += 1
        self.tmst += 1000
        file_rxpk(self.store, self.s, EUI, "127.0.0.1", t, rxpk(frame(SL01, self.pid, 67, device_metrics(level, volts)),
                                                               tmst=self.tmst), "live", cfgmod.channels(self.s))

    def step(self, now):
        import aobs.stratolink.store as st_mod
        st_mod.now.t = max(st_mod.now.t, now)
        return self.engine.step(self.fid, now)

    def state(self):
        return self.store.flight(self.fid)["state"]

    def kinds(self):
        return [e["kind"] for e in self.store.events(self.fid)]


def descent_speed(alt, sea_level=5.0):
    return sea_level * math.sqrt(predict.isa_density(0) / predict.isa_density(alt))


def latex_profile(rng, t0, ground_s=600, interval=60, jitter=25, ascent=5.0, burst=30000, noise=8.0,
                  wind=12.0, land_s=1800):
    """(t, lat, lon, alt, speed) of a latex flight: ground, ascent, burst, parachute descent, on the ground."""
    out, t = [], t0
    lat, lon, alt = SITE
    end_ground = t0 + dt.timedelta(seconds=ground_s)
    phase, ground_alt, landed_at = "ground", SITE[2], None
    while True:
        step = interval + rng.uniform(-jitter, jitter)
        t = t + dt.timedelta(seconds=step)
        dts = step
        if phase == "ground" and t >= end_ground:
            phase = "ascent"
        if phase == "ascent":
            alt += ascent * dts
            if alt >= burst:
                alt, phase = burst, "descent"
        elif phase == "descent":
            alt -= descent_speed(alt) * dts
            if alt <= ground_alt + 150:
                alt, phase, landed_at = ground_alt + 150, "landed", t
        if phase == "landed":
            alt = ground_alt + 150
            if (t - landed_at).total_seconds() > land_s:
                break
        moving = phase in ("ascent", "descent")
        if moving:
            lon += wind * dts / (111_320 * math.cos(math.radians(lat)))
        out.append((t, lat, lon, alt + rng.gauss(0, noise), wind if moving else 0.0))
    return out


def armed(store, kind="latex", name="Test mission", params=None):
    f = store.create_flight(name, SL01, "flight", balloon_type=kind, params=params)
    store.transition(f["id"], "PREFLIGHT", "checks", auto=False, by="t")
    return mission.arm(store, f["id"], "t")


# ------------------------------------------------------------------ state machine
def test_state_machine_rules(store):
    f = store.create_flight("SM", SL01, "flight")
    assert f["state"] == "PREPARATION" and f["status"] == "planned"
    with pytest.raises(ValueError, match="reason"):
        store.transition(f["id"], "PREFLIGHT", "", auto=False)
    with pytest.raises(ValueError, match="not allowed"):
        store.transition(f["id"], "ASCENT", "x", auto=False)              # launch needs arming first
    with pytest.raises(ValueError, match="not allowed"):
        store.transition(f["id"], "ASCENT", "x", auto=True)               # nothing launches automatically unarmed
    f = mission.arm(store, f["id"], "op")
    assert f["state"] == "ARMED" and f["status"] == "active" and f["armed_utc"] and f["started_utc"]
    assert f["launch_utc"] is None                                        # recording starts before launch
    with pytest.raises(ValueError, match="not allowed"):
        store.transition(f["id"], "COMPLETE", "x", auto=False)            # completing needs a landing
    with pytest.raises(ValueError, match="not allowed"):
        store.transition(f["id"], "LANDED", "x", auto=True)
    f = store.transition(f["id"], "PREFLIGHT", "disarm", auto=False)
    assert f["state"] == "PREFLIGHT" and f["armed_utc"] is None and f["status"] == "planned"
    ev = store.events(f["id"])
    assert [e["state_to"] for e in ev if e["kind"] == "state"] == ["ARMED", "PREFLIGHT"]
    assert all(e["auto"] == 0 and e["detail"] for e in ev if e["kind"] == "state")
    with pytest.raises(ValueError, match="already recording"):
        g = store.create_flight("other", SL01, "flight", start=True)
        mission.arm(store, f["id"], "op")
        store.transition(g["id"], "ARMED", "x", auto=False)


def test_unarmed_mission_never_launches(store, s):
    f = store.create_flight("not armed", SL01, "flight")
    sim = Sim(store, s, f["id"])
    for t, lat, lon, alt, sp in latex_profile(random.Random(2), T0)[:40]:
        assert sim.fix(t, lat, lon, alt, sp) is None
    assert sim.state() == "PREPARATION" and store.flight(f["id"])["launch_utc"] is None


# ------------------------------------------------------------------ a whole latex flight
def test_latex_flight_end_to_end_with_noise_gaps_duplicates_and_restart(store, s, tmp_path):
    f = armed(store)
    rng = random.Random(7)
    prof = latex_profile(rng, T0)
    sim = Sim(store, s, f["id"])
    spike_at, blackout = 30, range(70, 90)                 # a spike during ascent; 20 missing fixes (RF gap)
    sim.battery(T0 + dt.timedelta(seconds=30), 98, 4.1)
    launch_seen = None
    for i, (t, lat, lon, alt, sp) in enumerate(prof):
        if i in blackout:
            continue                                        # nothing received: contact lost, not landed
        if i == spike_at:
            sim.fix(t, lat, lon, alt + 4000, sp)            # isolated GPS altitude spike
            continue
        if i == 50:                                         # the daemon restarts mid-flight
            store2 = Store(s.db_path)
            sim.store, sim.engine = store2, mission.Engine(store2, 180, 600)
        if i in blackout:
            continue
        sim.fix(t, lat, lon, alt, sp, dup=(i % 7 == 0), resend=(i % 11 == 0))
        if i == blackout.start + 25 - 1:
            pass
        st = sim.state()
        if st == "ASCENT" and launch_seen is None:
            launch_seen = i
    # step through the blackout window as the daemon would: contact lost, phase uncertain, still DESCENT/ASCENT
    fl = sim.store.flight(f["id"])
    assert fl["state"] == "LANDED", fl["state"]
    ev = sim.store.events(f["id"])
    kinds = [e["kind"] for e in ev]
    for k in ("created", "armed", "launch", "milestone", "max_altitude", "burst", "descent", "landed"):
        assert k in kinds, (k, kinds)
    assert kinds.count("launch") == 1 and kinds.count("burst") == 1          # once each, across the restart
    assert len([e for e in ev if e["kind"] == "milestone"]) == len(set(e["title"] for e in ev if e["kind"] == "milestone"))
    # launch time: estimated within ~1 fix of the real one (ground ended at T0 + 600 s)
    lt = dt.datetime.fromisoformat(fl["launch_utc"].replace("Z", "+00:00"))
    assert abs((lt - (T0 + dt.timedelta(seconds=600))).total_seconds()) < 150
    assert fl["launch_method"] == "detected" and abs(fl["launch_alt_m"] - SITE[2]) < 15
    assert abs(fl["launch_lat"] - SITE[0]) < 1e-4
    a = sim.engine.analyze(fl, prof[-1][0] + dt.timedelta(seconds=5))
    m = a["stats"]
    assert 29_900 < m["max_alt_m"] < 30_100                                  # the +4000 m spike is not the maximum
    assert 4.5 < m["max_ascent_mps"] < 5.6
    assert m["max_descent_mps"] < -20                                        # fast just after burst in thin air
    assert m["downrange_m"] > 10_000 and m["distance_m"] >= m["downrange_m"]
    assert a["phase_confidence"] in ("medium", "high")
    # the operator confirms: COMPLETE with an archive
    mission.confirm_landing(sim.store, f["id"], "tommy", "recovered from a field")
    fl = sim.store.flight(f["id"])
    assert fl["state"] == "COMPLETE" and fl["summary"]["stats"]["max_alt_m"] == m["max_alt_m"]
    assert fl["summary"]["events"][-1]["kind"] == "completed"


def test_contact_loss_is_never_landing_and_the_clock_keeps_running(store, s):
    f = armed(store)
    sim = Sim(store, s, f["id"])
    prof = latex_profile(random.Random(3), T0, burst=12000)
    last_t = None
    for i, (t, lat, lon, alt, sp) in enumerate(prof):
        sim.fix(t, lat, lon, alt, sp)
        last_t = t
        if sim.state() == "DESCENT" and alt < 9000:
            break                                            # contact lost mid-descent
    assert sim.state() == "DESCENT"
    for minutes in (2, 5, 12, 40, 180):                      # hours of silence
        a = sim.step(last_t + dt.timedelta(minutes=minutes))
    fl = store.flight(f["id"])
    assert fl["state"] == "DESCENT" and fl["landed_utc"] is None              # never landed by silence
    assert a["contact"] == "lost" and a["phase_confidence"] == "uncertain" and a["freshness"]["state"] == "stale"
    assert a["met_s"] > 3 * 3600                                             # T+ keeps counting
    assert a["last_fix"]["rx_utc"] <= (last_t + dt.timedelta(seconds=3)).isoformat()   # last known position kept
    assert "contact_lost" in sim.kinds()
    # contact returns: tracking resumes, no positions are invented for the gap
    n_before = store.one("SELECT COUNT(*) AS n FROM positions")["n"]
    t2 = last_t + dt.timedelta(hours=3, minutes=1)
    sim.fix(t2, prof[-1][1], prof[-1][2], 400, 0)
    assert store.one("SELECT COUNT(*) AS n FROM positions")["n"] == n_before + 1
    assert "contact_restored" in sim.kinds()


def test_false_launches_rejected(store, s):
    f = armed(store)
    sim = Sim(store, s, f["id"])
    rng = random.Random(5)
    t = T0
    lat, lon = SITE[0], SITE[1]
    for i in range(40):                                      # carried around the site; GPS noise, two spikes, a hill
        t += dt.timedelta(seconds=60 + rng.uniform(-30, 30))
        lon += 0.0005                                        # walking/driving: horizontal only
        alt = SITE[2] + rng.gauss(0, 12) + (30 if 20 <= i < 26 else 0)     # up a 30 m hill and back
        if i in (10, 31):
            alt += 900                                       # isolated altitude spikes
        sim.fix(t, lat, lon, alt, 2)
    assert sim.state() == "ARMED" and "launch" not in sim.kinds()
    a = sim.step(t + dt.timedelta(seconds=5))
    assert a["launch_check"]["state"] == "waiting"


def test_two_fix_climb_is_not_enough_but_three_are(store, s):
    f = armed(store)
    sim = Sim(store, s, f["id"])
    t = T0
    for i in range(5):
        t += dt.timedelta(seconds=60)
        sim.fix(t, *SITE[:2], SITE[2] + (i % 2) * 5)
    for k, gain in enumerate((300, 600)):
        t += dt.timedelta(seconds=60)
        sim.fix(t, *SITE[:2], SITE[2] + gain)
        assert sim.state() == "ARMED"
    t += dt.timedelta(seconds=60)
    sim.fix(t, *SITE[:2], SITE[2] + 900)
    assert sim.state() == "ASCENT"


def test_manual_launch_now(store, s):
    f = armed(store)
    sim = Sim(store, s, f["id"])
    sim.fix(T0 + dt.timedelta(seconds=60), *SITE)
    f2 = mission.launch_now(store, f["id"], "tommy", "released by hand", now=T0 + dt.timedelta(seconds=90))
    assert f2["state"] == "ASCENT" and f2["launch_method"] == "manual" and f2["launch_lat"] == pytest.approx(SITE[0])
    with pytest.raises(ValueError):
        mission.launch_now(store, f["id"], "tommy")
    assert [e["kind"] for e in store.events(f["id"])].count("launch") == 1


def test_false_landing_reverts(store, s):
    """Descent stalls long enough to look landed (snagged / hovering), then continues: LANDED -> DESCENT."""
    f = armed(store, params={"landing_min_s": 300})
    sim = Sim(store, s, f["id"])
    t, alt = T0, SITE[2]
    for i in range(6):
        t += dt.timedelta(seconds=60)
        sim.fix(t, *SITE[:2], alt)
    for i in range(30):                                      # up to ~9 km
        t += dt.timedelta(seconds=60)
        alt += 300
        sim.fix(t, *SITE[:2], alt)
    for i in range(12):                                      # descent to ~2.4 km
        t += dt.timedelta(seconds=60)
        alt -= 550
        sim.fix(t, *SITE[:2], alt)
    assert sim.state() == "DESCENT"
    for i in range(8):                                       # level for 8 min
        t += dt.timedelta(seconds=60)
        sim.fix(t, *SITE[:2], alt + (i % 2) * 3)
    assert sim.state() == "LANDED"
    assert store.flight(f["id"])["completed_utc"] is None    # awaiting confirmation
    for i in range(4):                                       # it was not on the ground
        t += dt.timedelta(seconds=60)
        alt -= 300
        sim.fix(t, *SITE[:2], alt)
    fl = store.flight(f["id"])
    assert fl["state"] == "DESCENT" and fl["landed_utc"] is None
    assert "false_landing" in sim.kinds()


def test_irregular_sparse_reporting_still_detects(store, s):
    """3600-s positions (the firmware's clamp on the public channel): launch is detected late but still right."""
    f = armed(store, kind="pico")
    sim = Sim(store, s, f["id"])
    t, alt = T0, SITE[2]
    for i in range(3):
        t += dt.timedelta(seconds=3600 + 200 * i)
        sim.fix(t, *SITE[:2], alt + 3 * i)
    for i in range(4):
        t += dt.timedelta(seconds=3600 + 300 * (i % 2))
        alt += 0.8 * 3600
        sim.fix(t, *SITE[:2], alt)
    assert sim.state() == "ASCENT"
    fl = store.flight(f["id"])
    assert fl["launch_utc"] is not None


def test_pico_day_night_cycle_is_float_descent_ascent_not_burst(store, s):
    f = armed(store, kind="pico")
    sim = Sim(store, s, f["id"])
    t, alt = T0, SITE[2]
    for i in range(4):
        t += dt.timedelta(minutes=15)
        sim.fix(t, *SITE[:2], alt)
    while alt < 12000:                                       # 0.8 m/s
        t += dt.timedelta(minutes=15)
        alt += 0.8 * 900
        sim.fix(t, *SITE[:2], min(alt, 12000))
    for i in range(8):                                       # 2 h level
        t += dt.timedelta(minutes=15)
        sim.fix(t, *SITE[:2], 12000 + (i % 2) * 20)
    assert sim.state() == "FLOAT"
    for i in range(8):                                       # sinks at night, 1 m/s
        t += dt.timedelta(minutes=15)
        sim.fix(t, *SITE[:2], 12000 - (i + 1) * 900)
    assert sim.state() == "DESCENT"
    assert "burst" not in sim.kinds()
    for i in range(8):                                       # sunrise: up again
        t += dt.timedelta(minutes=15)
        sim.fix(t, *SITE[:2], 4800 + (i + 1) * 800)
    assert sim.state() == "ASCENT"


def test_tethered_test(store, s):
    f = armed(store, kind="tethered")
    sim = Sim(store, s, f["id"])
    t = T0
    for i in range(5):
        t += dt.timedelta(seconds=20)
        sim.fix(t, *SITE[:2], SITE[2] + (i % 2))
    latex_would = mission.detect_launch(mission.fixes_from(store.positions(SL01)), mission.PROFILES["latex"])
    for h in (8, 16, 24, 30, 30, 31, 30, 30, 31, 30, 30, 30):
        t += dt.timedelta(seconds=20)
        sim.fix(t, *SITE[:2], SITE[2] + h)
    assert latex_would["state"] == "waiting"
    assert sim.state() in ("ASCENT", "FLOAT")
    for h in (25, 18, 10, 4, 1, 0, 1, 0, 1, 0, 0, 1, 0):
        t += dt.timedelta(seconds=20)
        sim.fix(t, *SITE[:2], SITE[2] + h)
    assert sim.state() == "LANDED"


def test_gps_lost_while_packets_arrive_and_low_battery(store, s):
    f = armed(store)
    sim = Sim(store, s, f["id"])
    t = T0
    for i in range(5):
        t += dt.timedelta(seconds=60)
        sim.fix(t, *SITE)
    sim.step(t + dt.timedelta(seconds=10))
    for i in range(20):                                     # telemetry keeps coming, no positions
        t += dt.timedelta(seconds=60)
        sim.battery(t, 30 - i, 3.9 - 0.03 * i)
        sim.step(t + dt.timedelta(seconds=1))
    kinds = sim.kinds()
    assert "gps_lost" in kinds and "contact_lost" not in kinds
    assert kinds.count("low_battery") == 1
    t += dt.timedelta(seconds=60)
    sim.fix(t, *SITE)
    assert "gps_restored" in sim.kinds()


def test_notes_and_timeline_order(store, s):
    f = armed(store)
    mission.note(store, f["id"], "tommy", "Helium fill 2.1 m3, neck lift 1.6 kg")
    with pytest.raises(ValueError):
        mission.note(store, f["id"], "tommy", " ")
    ev = store.events(f["id"])
    assert ev[-1]["kind"] == "note" and ev[-1]["by_who"] == "tommy" and ev[-1]["auto"] == 0
    assert [e["at_utc"] for e in ev] == sorted(e["at_utc"] for e in ev)


# ------------------------------------------------------------------ prediction
class FakeTawhiri:
    def __init__(self):
        self.queries = []

    def __call__(self, req, timeout):
        import io
        import urllib.parse
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))
        self.queries.append(q)
        lat, lon = float(q["launch_latitude"]), float(q["launch_longitude"])
        burst = float(q.get("burst_altitude", q.get("float_altitude", 0)))
        drift = burst / 1e5 + float(q.get("descent_rate", 5)) / 100
        body = {"prediction": [
            {"stage": "ascent", "trajectory": [{"latitude": lat, "longitude": lon, "altitude": float(q["launch_altitude"]),
                                                "datetime": q["launch_datetime"]},
                                               {"latitude": lat, "longitude": lon + drift, "altitude": burst,
                                                "datetime": q["launch_datetime"]}]},
            {"stage": "descent", "trajectory": [{"latitude": lat, "longitude": lon + drift, "altitude": burst,
                                                 "datetime": q["launch_datetime"]},
                                                {"latitude": lat - 0.1, "longitude": lon + 2 * drift, "altitude": 200,
                                                 "datetime": q["launch_datetime"]}]}],
            "request": {"dataset": "2026-12-05T12:00:00Z"}, "metadata": {}, "warnings": {}}

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R(json.dumps(body).encode())


def test_prediction_basis_and_variants(store, s):
    f = armed(store)
    eng = mission.Engine(store)
    a = eng.analyze(store.flight(f["id"]), T0)
    with pytest.raises(predict.PredictionUnavailable, match="launch site"):
        predict.basis_for(store.flight(f["id"]), a, a["params"], now=T0)    # nothing to start from: no prediction
    b = predict.basis_for(store.flight(f["id"]), a, a["params"], {"lat": 35.5, "lon": -80.8, "alt_m": 230}, now=T0)
    fake = FakeTawhiri()
    r = predict.predict(b, "http://x/tawhiri", opener=fake)
    assert r["landing"] and r["dataset"] == "2026-12-05T12:00:00Z" and len(r["variants"]) == 6
    assert r["spread_m"] > 0 and any("not included" in u for u in r["uncertainty"])
    assert float(fake.queries[0]["launch_longitude"]) == pytest.approx(279.2)            # 0-360 longitudes
    # descending: measured rate converted to sea level
    v = predict.sea_level_descent(-20.0, 20000)
    assert 4 < v < 6
    sim = Sim(store, s, f["id"])
    t = T0
    for i in range(5):
        t += dt.timedelta(seconds=60)
        sim.fix(t, *SITE)
    store.transition(f["id"], "ASCENT", "test", auto=False, launch_utc=t.isoformat(), launch_lat=SITE[0],
                     launch_lon=SITE[1], launch_alt_m=SITE[2])
    for i in range(6):
        t += dt.timedelta(seconds=60)
        sim.fix(t, SITE[0], SITE[1], SITE[2] + 300 * (i + 1), step=False)
    a = eng.analyze(store.flight(f["id"]), t + dt.timedelta(seconds=5))
    b = predict.basis_for(store.flight(f["id"]), a, a["params"], now=t + dt.timedelta(seconds=5))
    assert b["ascent_rate_mps"] == pytest.approx(5.0, abs=0.3) and any("measured" in x for x in b["assumptions"])
    with pytest.raises(predict.PredictionUnavailable, match="old"):
        predict.basis_for(store.flight(f["id"]), a, a["params"], now=t + dt.timedelta(hours=3))


def test_prediction_endpoint_records_unavailable(store, s):
    api = API(s, store, lambda: {})
    f = armed(store)
    code, out, _ = api.post(f"/v1/stratolink/flights/{f['id']}/predict", {}, "tommy")
    assert out["prediction"]["status"] == "unavailable" and "launch site" in out["prediction"]["error"]
    code, out, _ = api.post(f"/v1/stratolink/flights/{f['id']}/predict",
                            {"overrides": {"lat": 35.5, "lon": -80.8}}, "tommy")
    assert out["prediction"]["status"] == "unavailable" and "unreachable" in out["prediction"]["error"]
    s.prediction_url = ""
    with pytest.raises(Exception):
        api.post(f"/v1/stratolink/flights/{f['id']}/predict", {}, "tommy")


# ------------------------------------------------------------------ API and compatibility
def test_mission_api_flow(store, s):
    api = API(s, store, lambda: {})
    code, f, _ = api.post("/v1/stratolink/flights", {"name": "API mission", "tracker": "!f661fb94",
                                                     "balloon_type": "mylar", "params": {"launch_gain_m": 40}}, "op")
    fid = f["flight"]["id"]
    assert f["flight"]["balloon_type"] == "mylar" and f["flight"]["params"]["launch_gain_m"] == 40
    assert api.post(f"/v1/stratolink/flights/{fid}/state", {"to": "PREFLIGHT", "reason": "checks"}, "op")[0] == 200
    code, out, _ = api.post(f"/v1/stratolink/flights/{fid}/arm", {}, "op")
    assert out["flight"]["state"] == "ARMED" and out["mission"]["analysis"]["armed_s"] is not None
    code, out, _ = api.post(f"/v1/stratolink/flights/{fid}/launch", {"reason": "go"}, "op")
    assert out["flight"]["state"] == "ASCENT"
    api.post(f"/v1/stratolink/flights/{fid}/note", {"text": "looks good"}, "op")
    code, mv, _ = api.get(f"/v1/stratolink/flights/{fid}/mission", {})
    assert mv["analysis"]["met_s"] is not None and mv["events"][-1]["detail"] == "looks good"
    assert "ABORTED" in mv["manual_transitions"]
    with pytest.raises(Exception):
        api.post(f"/v1/stratolink/flights/{fid}/abort", {}, "op")             # needs a reason
    code, out, _ = api.post(f"/v1/stratolink/flights/{fid}/abort", {"reason": "cut down for airspace"}, "op")
    assert out["flight"]["state"] == "ABORTED" and out["flight"]["summary"]["mission"]["state"] == "ABORTED"
    code, sm, _ = api.get(f"/v1/stratolink/flights/{fid}/summary", {})
    assert sm["events"][-1]["kind"] == "aborted"
    # legacy start/end still work
    code, g, _ = api.post("/v1/stratolink/flights", {"name": "legacy", "tracker": "!f661fb94", "start": True}, "op")
    assert g["flight"]["state"] == "ARMED"
    code, g2, _ = api.post(f"/v1/stratolink/flights/{g['flight']['id']}/end", {}, "op")
    assert g2["flight"]["state"] == "COMPLETE" and g2["flight"]["status"] == "ended"


def test_old_database_migrates(tmp_path):
    """A database made by the first Stratolink release (no mission columns) opens and keeps its flights."""
    p = tmp_path / "old.sqlite3"
    db = sqlite3.connect(p)
    db.executescript("""CREATE TABLE flights (id INTEGER PRIMARY KEY, name TEXT NOT NULL, tracker_node INTEGER NOT NULL,
      mode TEXT NOT NULL, status TEXT NOT NULL, created_utc TEXT NOT NULL, started_utc TEXT, ended_utc TEXT, notes TEXT,
      loss_warn_s REAL, loss_lost_s REAL);
      INSERT INTO flights VALUES (1,'old ended',4133616532,'flight','ended','2026-11-27T17:00:00Z','2026-11-27T17:01:00Z',
        '2026-11-27T18:00:00Z',NULL,NULL,NULL);
      INSERT INTO flights VALUES (2,'old active',4133616532,'ground_test','active','2026-11-27T17:00:00Z',
        '2026-11-27T17:01:00Z',NULL,NULL,NULL,NULL);
      INSERT INTO flights VALUES (3,'old planned',4133616532,'flight','planned','2026-11-27T17:00:00Z',NULL,NULL,NULL,
        NULL,NULL);""")
    db.commit()
    db.close()
    st = Store(p)
    by = {f["name"]: f for f in st.flights()}
    assert by["old ended"]["state"] == "COMPLETE" and by["old ended"]["completed_utc"] == "2026-11-27T18:00:00Z"
    assert by["old active"]["state"] == "ARMED" and by["old active"]["balloon_type"] == "tethered"
    assert by["old planned"]["state"] == "PREPARATION" and by["old planned"]["params"] == {}
    st.close()
    Store(p).close()                                          # opening again changes nothing


# ------------------------------------------------------------------ randomized flights
@pytest.mark.parametrize("seed", range(10))
@pytest.mark.parametrize("noise,interval,jitter", [(8, 60, 25), (20, 60, 40), (15, 120, 60), (10, 30, 10)])
def test_randomized_latex_flights(store, s, seed, noise, interval, jitter):
    """Noise, jittered intervals, 15 % packet loss and one large altitude spike anywhere: always exactly one launch
    (on time), one burst, a suspected landing, and no false float, ascent or landing."""
    f = armed(store)
    rng = random.Random(seed)
    prof = latex_profile(rng, T0, noise=noise, interval=interval, jitter=jitter)
    sim = Sim(store, s, f["id"], seed)
    spike = rng.randrange(5, len(prof) - 5)
    seen = []
    for i, (t, lat, lon, alt, sp) in enumerate(prof):
        if rng.random() < 0.15:
            continue
        sim.fix(t, lat, lon, alt + (rng.choice([3000, -2500]) if i == spike else 0), sp)
        if sim.state() not in seen:
            seen.append(sim.state())
    fl = store.flight(f["id"])
    assert seen == ["ARMED", "ASCENT", "DESCENT", "LANDED"], seen
    lt = dt.datetime.fromisoformat(fl["launch_utc"].replace("Z", "+00:00"))
    assert abs((lt - (T0 + dt.timedelta(seconds=600))).total_seconds()) < 2.5 * interval
    k = sim.kinds()
    assert k.count("launch") == 1 and k.count("burst") == 1, k
    assert "false_landing" not in k and "float" not in k and "ascent" not in k, k


# ------------------------------------------------------------------ reporting intervals and the private channel
def test_firmware_rules_predict_the_observed_reverts():
    """What we saw on SL01 (2.7.26): 60 s positions -> 3600, 60 s telemetry -> 1800, on LongFast."""
    from aobs.stratolink import profiles as prof
    want = {"position.position_broadcast_secs": 60, "telemetry.device_update_interval": 60,
            "position.broadcast_smart_minimum_interval_secs": 30, "device.role": "TRACKER"}
    e = prof.firmware_effect(want, "public")
    assert e["position.position_broadcast_secs"]["persisted"] == 3600
    assert e["telemetry.device_update_interval"]["persisted"] == 1800
    assert e["position.broadcast_smart_minimum_interval_secs"]["persisted"] == 300
    assert prof.firmware_effect(want, "private") == {}
    kept = prof.firmware_effect(want, "private", keep_public_secondary=True)
    assert set(kept) == {"telemetry.device_update_interval"}            # positions free, telemetry still clamped
    assert prof.firmware_effect({**want, "device.role": "ROUTER"}, "public")[
        "position.position_broadcast_secs"]["persisted"] == 43200


def test_private_channel_bundle_and_decoding(store, s):
    import os
    from aobs.stratolink import meshtastic as mt
    from aobs.stratolink import profiles as prof
    from aobs.stratolink.provision import Provisioning
    pv = Provisioning(s.path("provision"), store, prof.Profiles(s.path("profiles")), __import__("pathlib").Path(
        s.keys_file), "Stratolink")
    gt = pv.bundle("Stratolink-05", "SL05", "heltec_v4", "ground-test")
    assert gt["firmware_effect"]["position.position_broadcast_secs"]["persisted"] == 3600
    assert gt["channel_config"] == {"mode": "public", "name": "LongFast"}
    with pytest.raises(ValueError, match="key first"):
        pv.bundle("Stratolink-02", "SL02", "heltec_v4", "ground-test", channel_mode="private")
    info = pv.create_private_channel()
    assert info["exists"] and info["key_bits"] == 256 and (os.stat(s.keys_file).st_mode & 0o777) == 0o600
    with pytest.raises(ValueError, match="never replaced"):
        pv.create_private_channel()
    b = pv.bundle("Stratolink-02", "SL02", "heltec_v4", "ground-test", channel_mode="private")
    assert b["settings"]["lora.channel_num"] == 20 and b["firmware_effect"] == {}
    assert b["channel_config"]["psk"] and (os.stat(s.path("provision", f"{b['bundle_id']}.bundle.json")).st_mode
                                           & 0o777) == 0o600
    assert "in the bundle file only" in json.dumps(pv.bundles())          # never shown on the page
    # the observatory decodes both the private channel and the public one
    key = mt.expand_psk(__import__("base64").b64decode(cfgmod.read_keys(__import__("pathlib").Path(s.keys_file))
                                                       ["Stratolink"]))
    priv = frame(0x0A0B0C0D, 0x55, 1, b"private hello", key=key, channel="Stratolink")
    d = mt.decode(priv, cfgmod.channels(s))
    assert d.status == "decoded" and d.channel == "Stratolink" and d.channel_public is False
    assert mt.decode(priv).status == "encrypted"                          # without the key file: not readable
    assert mt.decode(frame(0x0A0B0C0D, 0x56, 1, b"public"), cfgmod.channels(s)).channel == "LongFast"
    # a report: requested vs persisted, the firmware's change explained, not a failure
    report = {"kind": "stratolink-provision-report", "bundle_id": gt["bundle_id"], "status": "ok",
              "identity": {"node_id": "!0a0b0c0e", "hw_model": "HELTEC_V4", "firmware_version": "2.7.26.54e0d8d"},
              "applied": {**gt["settings"], "position.position_broadcast_secs": 3600,
                          "telemetry.device_update_interval": 1800}, "mismatches": {}}
    r = pv.accept(report)
    p = r["tracker"]["provision"]
    assert p["persisted_differs"]["position.position_broadcast_secs"]["requested"] == 60
    assert "raises it to 3600" in p["persisted_differs"]["position.position_broadcast_secs"]["rule"]
    from aobs.stratolink import views
    iv = views.intervals(store, 0x0A0B0C0E, p, None)
    assert iv["position"] == {"requested": 60, "persisted": 3600, "observed": None,
                              "rule": p["persisted_differs"]["position.position_broadcast_secs"]["rule"]}


def test_private_profile_validation():
    from aobs.stratolink import profiles as prof
    ps = prof.DEFAULT_PROFILES["ground-test"]
    r = prof.resolve(ps, "heltec_v4", channel_mode="private")
    assert r["errors"] == [] and r["persisted"]["position.position_broadcast_secs"] == 60
    r = prof.resolve(ps, "heltec_v4")
    assert r["persisted"]["position.position_broadcast_secs"] == 3600 and any("will persist" in w for w in r["warnings"])
    errs, _ = prof.validate({**ps["settings"], "lora.channel_num": 5}, "heltec_v4")
    assert any("906.875" in e for e in errs)


def test_observed_interval_with_few_fixes():
    fx = [mission.Fix(1000.0 + 60 * i, 0, 0, 100, None, {}) for i in range(5)]
    assert mission.observed_interval(fx) == 60
    assert mission.observed_interval(fx[:2]) is None                      # one gap is not a pattern
    many = [mission.Fix(1000.0 + 30 * i, 0, 0, 100, None, {}) for i in range(40)]
    assert mission.observed_interval(many) == 30


def test_burst_found_after_the_descent_turn(store, s):
    """Dense fixes: the descent is detected before the drop reaches burst_drop_m; the burst is logged once it does."""
    f = armed(store)
    sim = Sim(store, s, f["id"])
    t, alt = T0, SITE[2]
    for i in range(6):
        t += dt.timedelta(seconds=5)
        sim.fix(t, *SITE[:2], alt)
    while alt < 3000:
        t += dt.timedelta(seconds=5)
        alt += 50
        sim.fix(t, *SITE[:2], alt)
    kinds_at_turn = None
    for i in range(30):
        t += dt.timedelta(seconds=5)
        alt -= 45
        sim.fix(t, *SITE[:2], alt)
        if sim.state() == "DESCENT" and kinds_at_turn is None:
            kinds_at_turn = sim.kinds()
    assert "burst" not in kinds_at_turn                     # too early to tell at the turn
    assert sim.kinds().count("burst") == 1
