"""Stratolink: the verified RF packet as a regression test, Meshtastic decoding of synthetic tracker traffic (built
here with an independent protobuf/AES encoder), the Semtech UDP receiver on a real socket (ACKs, source filtering,
single bind), deduplication across rebroadcasts and gateways, log import, position checks, flights and exports,
preflight, the control API, profiles, tracker provisioning and ground-station provisioning against a fake gateway."""

from __future__ import annotations

import base64
import datetime as dt
import errno
import json
import os
import pathlib
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import pytest

from aobs.stratolink import config as cfgmod
from aobs.stratolink import firmware, geo, gwprov, profiles, semtech, track, views
from aobs.stratolink import meshtastic as mt
from aobs.stratolink.control import API, ControlServer
from aobs.stratolink.provision import Provisioning
from aobs.stratolink.receiver import Receiver, file_rxpk, import_log
from aobs.stratolink.store import Store, iso

UTC = dt.timezone.utc
KNOWN = bytes.fromhex("ffffffff94fb61f6a6228b89620800611499ffd8c3376e6814cb04fc7549381b5e8e1a1b73b5ef77b3")
SL01 = 0xF661FB94
EUI = "2CF7F12004B70000"
T0 = dt.datetime(2026, 11, 27, 17, 0, tzinfo=UTC)


# ------------------------------------------------------------------ an independent encoder for test traffic
def varint(n: int) -> bytes:
    n &= 0xFFFFFFFFFFFFFFFF
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def fv(num: int, v: int) -> bytes:
    return varint(num << 3) + varint(v)


def ff32(num: int, v: int, signed: bool = False) -> bytes:
    return varint(num << 3 | 5) + struct.pack("<i" if signed else "<I", v)


def ffloat(num: int, v: float) -> bytes:
    return varint(num << 3 | 5) + struct.pack("<f", v)


def fbytes(num: int, b: bytes) -> bytes:
    return varint(num << 3 | 2) + varint(len(b)) + b


def position(lat: float, lon: float, alt: int, t: int, sats: int = 12, speed: int = 0, track_: int = 0,
             prec: int = 32) -> bytes:
    return (ff32(1, round(lat * 1e7), True) + ff32(2, round(lon * 1e7), True) + fv(3, alt) + ff32(4, t) + fv(5, 2)
            + fv(11, 141) + fv(15, speed) + fv(16, track_) + fv(19, sats) + fv(23, prec))


def device_metrics(batt: int, volts: float, uptime: int = 100) -> bytes:
    return ff32(1, 0) + fbytes(2, fv(1, batt) + ffloat(2, volts) + ffloat(3, 1.5) + ffloat(4, 0.2) + fv(5, uptime))


def user(long: str, short: str, hw: int = 110, role: int = 5) -> bytes:
    return fbytes(1, b"!f661fb94") + fbytes(2, long.encode()) + fbytes(3, short.encode()) + fv(5, hw) + fv(7, role)


def frame(sender: int, pid: int, port: int, payload: bytes, hop_limit: int = 3, hop_start: int = 3,
          key: bytes = mt.DEFAULT_KEY, channel: str = "LongFast", relay: int = 0x94) -> bytes:
    data = fv(1, port) + fbytes(2, payload)
    nonce = struct.pack("<QII", pid, sender, 0)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    enc = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor().update(data)
    flags = (hop_start << 5) | hop_limit
    return struct.pack("<III", 0xFFFFFFFF, sender, pid) + bytes([flags, mt.channel_hash(channel, key), 0, relay]) + enc


def rxpk(raw: bytes, rssi: float = -60, snr: float = 8.0, tmst: int = 1000, crc: int = 1) -> dict:
    return {"tmst": tmst, "chan": 8, "rfch": 0, "freq": 906.875, "stat": crc, "modu": "LORA", "datr": "SF11BW250",
            "codr": "4/5", "lsnr": snr, "rssi": rssi, "size": len(raw), "data": base64.b64encode(raw).decode()}


def push(eui: str = EUI, token: bytes = b"\x12\x34", body: dict | None = None) -> bytes:
    return b"\x02" + token + b"\x00" + bytes.fromhex(eui) + json.dumps(body or {}).encode()


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def s(tmp_path: pathlib.Path) -> cfgmod.Settings:
    st = cfgmod.Settings(data_dir=str(tmp_path / "data"), udp_host="127.0.0.1", udp_port=0,
                         allowed_sources=["127.0.0.1"], keys_file=str(tmp_path / "keys.toml"), import_paths=[],
                         firmware_dir=str(tmp_path / "fw"), rebind_retry_s=0.2)
    st.root.mkdir(parents=True)
    return st


@pytest.fixture
def store(s: cfgmod.Settings) -> Store:
    st = Store(s.db_path)
    st.register_tracker("!f661fb94", "Stratolink-01", short_name="SL01", hardware="heltec_v4")
    st.register_gateway(EUI, name="Observatory", lat=35.48, lon=-80.83, alt_m=230, ip="192.168.4.34")
    yield st
    st.close()


def ingest(store: Store, s: cfgmod.Settings, raw: bytes, at: dt.datetime, eui: str = EUI, **kw) -> dict:
    return file_rxpk(store, s, eui, "127.0.0.1", at, rxpk(raw, **kw), "live", cfgmod.channels(s))


# ------------------------------------------------------------------ decoding
def test_known_rf_packet_regression():
    d = mt.decode(KNOWN)
    assert d.status == "decoded"
    assert mt.node_id(d.header.sender) == "!f661fb94"
    assert f"{d.header.packet_id:08x}" == "898b22a6"
    assert d.port == "TEXT_MESSAGE_APP" and d.portnum == 1
    assert d.content == {"text": "STRATOLINK TEST 001"}
    assert d.data["bitfield"] == 0
    assert d.channel == "LongFast" and d.header.channel_hash == 0x08
    assert d.header.hop_start == 3 and d.header.hop_limit == 2 and d.header.hops_away == 1


def test_matches_reference_meshtastic_library():
    """Where the meshtastic package is installed (the old receiver's venv), its protobufs agree with ours."""
    pb = pytest.importorskip("meshtastic.protobuf.mesh_pb2")
    raw = frame(SL01, 0x1CF57CED, 3, position(35.4835111, -80.8272358, 227, 1795798872, 14, 3, 14443000))
    d = mt.decode(raw)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    plain = Cipher(algorithms.AES(mt.DEFAULT_KEY), modes.CTR(struct.pack("<QII", 0x1CF57CED, SL01, 0))) \
        .decryptor().update(raw[16:])
    ref = pb.Data()
    ref.ParseFromString(plain)
    pos = pb.Position()
    pos.ParseFromString(ref.payload)
    assert d.content["lat"] == pytest.approx(pos.latitude_i * 1e-7)
    assert d.content["sats_in_view"] == pos.sats_in_view
    assert d.content["heading_deg"] == pytest.approx(pos.ground_track * 1e-5)


def test_decode_position_telemetry_nodeinfo():
    p = mt.decode(frame(SL01, 1, 3, position(35.4835111, -80.8272358, 227, 1795798872, 14, 3, 14443000)))
    assert p.port == "POSITION_APP"
    c = p.content
    assert c["lat"] == pytest.approx(35.4835111) and c["lon"] == pytest.approx(-80.8272358)
    assert c["alt_m"] == 227 and c["sats_in_view"] == 14 and c["speed_mps"] == 3
    assert c["heading_deg"] == pytest.approx(144.43) and c["precision_bits"] == 32 and c["pdop"] == 1.41
    t = mt.decode(frame(SL01, 2, 67, device_metrics(87, 3.91)))
    assert t.content["kind"] == "device" and t.content["battery_level"] == 87
    assert t.content["voltage"] == pytest.approx(3.91, abs=1e-4)
    u = mt.decode(frame(SL01, 3, 4, user("Stratolink-01", "SL01")))
    assert u.content["hw_model"] == "HELTEC_V4" and u.content["role"] == "TRACKER"
    assert u.content["short_name"] == "SL01"


def test_negative_altitude_int32():
    p = mt.decode(frame(SL01, 9, 3, position(35.0, -80.0, -20, 1795798872)))
    assert p.content["alt_m"] == -20


def test_malformed_and_encrypted_are_graceful():
    assert mt.decode(b"\x01\x02").status == "malformed"
    assert mt.decode(KNOWN[:16]).status == "malformed"
    private = os.urandom(16)
    raw = frame(SL01, 5, 1, b"secret", key=private, channel="Balloons")
    d = mt.decode(raw)
    assert d.status == "encrypted" and d.content == {}
    ch = mt.Channel("Balloons", private, public=False)
    assert mt.decode(raw, [ch, *mt.DEFAULT_CHANNELS]).content == {"text": "secret"}
    corrupt = KNOWN[:16] + bytes(b ^ 0x5A for b in KNOWN[16:])
    assert mt.decode(corrupt).status in ("encrypted", "decoded")      # never raises


def test_default_key_custom_channel_name():
    raw = frame(0x8FC774B0, 0x7357EDF1, 1, "👍".encode(), channel="Charlotte")
    d = mt.decode(raw)
    assert d.status == "decoded" and d.content["text"] == "👍" and d.channel.startswith("unnamed 0x")


def test_psk_expansion_and_hash():
    assert mt.expand_psk(b"\x01") == mt.DEFAULT_KEY
    assert mt.expand_psk(b"") is None
    assert mt.expand_psk(b"\x02")[-1] == (mt.DEFAULT_KEY[-1] + 1) & 0xFF
    assert mt.channel_hash("LongFast", mt.DEFAULT_KEY) == 0x08
    with pytest.raises(ValueError):
        mt.expand_psk(b"123")


# ------------------------------------------------------------------ Semtech protocol
def test_acks():
    d = semtech.parse(b"\x02\xab\xcd\x02" + bytes.fromhex(EUI))
    assert d.kind_name == "PULL_DATA" and d.ack() == b"\x02\xab\xcd\x04"
    d = semtech.parse(push(token=b"\x01\x02", body={"stat": {"temp": 41.5}}))
    assert d.ack() == b"\x02\x01\x02\x01" and d.eui == EUI and d.body["stat"]["temp"] == 41.5
    d = semtech.parse(b"\x01\x00\x01\x00" + bytes.fromhex(EUI) + b"{not json")
    assert d.ack() == b"\x01\x00\x01\x01" and d.body_error
    for bad in (b"", b"\x02\x00", b"\x03\x00\x00\x00" + bytes(8), b"\x02\x00\x00\x03" + bytes(8), b"\x02\x00\x00\x00"):
        with pytest.raises(semtech.ProtocolError):
            semtech.parse(bad)


def test_rxpk_fields():
    m = semtech.rxpk_fields(rxpk(KNOWN, rssi=-55, snr=7.8))
    assert m["sf"] == 11 and m["bw_khz"] == 250 and m["rssi"] == -55 and m["snr"] == 7.8 and m["crc"] == 1


# ------------------------------------------------------------------ the receiver on a real socket
def test_receiver_end_to_end(s, store):
    rx = Receiver(s, store)
    rx.start()
    for _ in range(50):
        if rx.sock:
            break
        time.sleep(0.05)
    port = rx.sock.getsockname()[1]
    gw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    gw.settimeout(2)
    try:
        gw.sendto(b"\x02\x00\x07\x02" + bytes.fromhex(EUI), ("127.0.0.1", port))
        assert gw.recvfrom(64)[0] == b"\x02\x00\x07\x04"
        body = {"stat": {"time": "2026-11-27 17:00:00 GMT", "rxnb": 3, "rxok": 2, "rxfw": 2, "ackr": 100.0, "dwnb": 0,
                         "txnb": 0, "temp": 38.5},
                "rxpk": [rxpk(KNOWN, rssi=-55, snr=7.8)]}
        gw.sendto(push(token=b"\x00\x08", body=body), ("127.0.0.1", port))
        assert gw.recvfrom(64)[0] == b"\x02\x00\x08\x01"
        for _ in range(50):
            if store.one("SELECT COUNT(*) AS n FROM rx")["n"]:
                break
            time.sleep(0.05)
    finally:
        gw.close()
        rx.stop()
    r = store.console()[0]
    assert r["sender_id"] == "!f661fb94" and r["content"] == {"text": "STRATOLINK TEST 001"} and r["rssi"] == -55
    st = store.one("SELECT * FROM gateway_stats")
    assert st["temp"] == 38.5 and st["ackr"] == 100.0 and st["rxok"] == 2
    g = views.gateway_view(store.gateways()[0], store, s, dt.datetime.now(UTC))
    assert g["online"] and g["temp_c"] == 38.5 and g["received"]["crc_ok"] == 1
    assert rx.counters["acks"] == 2 and rx.counters["decoded"] == 1


def test_disallowed_source_gets_no_ack(s, store):
    s.allowed_sources = ["10.9.9.9"]
    rx = Receiver(s, store)
    sent = []
    rx.handle(push(body={"rxpk": [rxpk(KNOWN)]}), ("192.0.2.5", 1700), lambda b, a: sent.append(b))
    assert sent == [] and rx.counters["rejected_source"] == 1
    assert store.one("SELECT COUNT(*) AS n FROM rx")["n"] == 0
    # a registered gateway's address is accepted even outside allowed_sources
    rx.handle(push(body={}), ("192.168.4.34", 1700), lambda b, a: sent.append(b))
    assert sent == [b"\x02\x12\x34\x01"]


def test_only_one_process_binds_the_port(s, store):
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("127.0.0.1", 0))
    s.udp_port = holder.getsockname()[1]
    rx = Receiver(s, store)
    with pytest.raises(OSError) as e:
        rx.bind()
    assert e.value.errno == errno.EADDRINUSE
    rx.start()
    time.sleep(0.3)
    assert rx.state == "port_busy" and "manual" in rx.detail
    holder.close()                                    # the manual receiver stops: this one takes over
    for _ in range(40):
        if rx.state == "listening":
            break
        time.sleep(0.1)
    assert rx.state == "listening"
    rx.stop()


def test_malformed_rxpk_does_not_stop_anything(s, store):
    rx = Receiver(s, store)
    out = rx.handle(push(body={"rxpk": [{"data": "@@@notbase64", "rssi": -90}, rxpk(KNOWN)]}), ("127.0.0.1", 1),
                    lambda b, a: None)
    assert [o["status"] for o in out] == ["malformed", "decoded"]
    crc = rx.handle(push(body={"rxpk": [rxpk(b"\x00" * 30, crc=-1, tmst=5)]}), ("127.0.0.1", 1), lambda b, a: None)
    assert crc[0]["status"] == "crc_error"


# ------------------------------------------------------------------ dedup, multi-gateway, privacy
def test_rebroadcast_and_second_gateway_are_one_packet(s, store):
    raw = frame(SL01, 0xAA, 3, position(35.50, -80.80, 1200, 1795798872))
    a = ingest(store, s, raw, T0, rssi=-50, snr=9, tmst=1)
    b = ingest(store, s, frame(SL01, 0xAA, 3, position(35.50, -80.80, 1200, 1795798872), hop_limit=2, relay=0x61),
               T0 + dt.timedelta(seconds=1), rssi=-80, snr=2, tmst=2)
    store.register_gateway("2CF7F12004B80000", name="Ridge", lat=35.9, lon=-81.2)
    c = ingest(store, s, raw, T0 + dt.timedelta(milliseconds=300), eui="2CF7F12004B80000", rssi=-110, snr=-12,
               tmst=99)
    assert not a["duplicate"] and b["duplicate"] and c["duplicate"]
    assert a["packet_ref"] == b["packet_ref"] == c["packet_ref"]
    p = store.one("SELECT * FROM packets")
    assert p["rx_count"] == 3 and p["gateway_count"] == 2 and p["best_rssi"] == -50 and p["min_hops"] == 0
    assert store.one("SELECT COUNT(*) AS n FROM positions")["n"] == 1     # one fix, not three
    rec = store.packet_receptions(p["id"])
    assert {r["gateway"] for r in rec} == {"Observatory", "Ridge"}
    assert sorted(r["rssi"] for r in rec) == [-110, -80, -50]
    # the same reception filed twice (a log import of something heard live) is not counted again
    assert ingest(store, s, raw, T0, rssi=-50, snr=9, tmst=1) is None


def test_foreign_nodes_keep_only_a_coarse_position(s, store):
    stranger = 0x76A45330
    raw = frame(stranger, 0x652E5BB8, 3, position(34.6996736, -76.8032768, 37, 1795798845))
    r = ingest(store, s, raw, T0)
    assert not r["is_tracker"] and "position" not in r
    assert store.one("SELECT COUNT(*) AS n FROM positions")["n"] == 0
    p = store.one("SELECT content FROM packets")
    assert json.loads(p["content"]) == {"lat": 34.7, "lon": -76.8, "precision_bits": 32, "location_source": "internal",
                                        "coarse": True}
    n = store.nodes()[0]
    assert n["coarse_lat"] == 34.7 and not n["registered"]
    assert store.purge_foreign_raw(T0 + dt.timedelta(days=2)) == 1
    assert store.one("SELECT raw_hex FROM rx")["raw_hex"] is None


def test_tracker_registration_from_discovery(s, store):
    new = 0x12345678
    ingest(store, s, frame(new, 1, 4, user("Stratolink-02", "SL02")), T0)
    assert any(n["node_id"] == "!12345678" and n["long_name"] == "Stratolink-02" for n in store.nodes())
    store.register_tracker("!12345678", "Stratolink-02", short_name="SL02")
    r = ingest(store, s, frame(new, 2, 3, position(35.6, -80.9, 300, 1795798900)), T0 + dt.timedelta(seconds=60))
    assert r["is_tracker"] and r["position"]["valid"] == 1


# ------------------------------------------------------------------ positions
def test_position_checks():
    now = T0
    ok, why = track.validate({"lat": 0.0, "lon": 0.0}, now, None, None)
    assert not ok and "0,0" in why
    assert track.validate({"lat": 35.0, "lon": -80.0, "precision_bits": 0}, now, None, None)[0] is False
    assert track.validate({"lat": 35.0, "lon": -80.0, "alt_m": 90000}, now, None, None)[0] is False
    assert track.validate({"lat": 35.0, "lon": -80.0, "gps_time": 100}, now, None, None)[0] is False
    last = {"lat": 35.0, "lon": -80.0, "alt_m": 200, "rx_utc": iso(now - dt.timedelta(seconds=60)), "gps_utc": None}
    ok, why = track.validate({"lat": 36.0, "lon": -80.0, "alt_m": 200}, now, last, None)
    assert not ok and why.startswith("jump")
    assert track.validate({"lat": 35.001, "lon": -80.0, "alt_m": 400}, now, last, None)[0]
    # the bad one was the reference: two agreeing fixes far away restart the track
    rej = {"lat": 36.0, "lon": -80.0, "alt_m": 200, "rx_utc": iso(now - dt.timedelta(seconds=30)),
           "reject_reason": "jump of 111 km"}
    assert track.validate({"lat": 36.0001, "lon": -80.0, "alt_m": 210}, now, last, rej)[0]


def test_flight_track_stats_and_exports(s, store):
    f = store.create_flight("SL-01 test flight", SL01, "flight", start=True)
    assert f["status"] == "active"
    t = T0
    with pytest.raises(ValueError):
        store.create_flight("x", 0x01020304)
    for i in range(10):
        lat, alt = 35.48 + i * 0.002, 230 + i * 150
        t = dt.datetime.now(UTC) + dt.timedelta(seconds=30 * i)
        ingest(store, s, frame(SL01, 100 + i, 3, position(lat, -80.83, alt, int(t.timestamp()), speed=5)), t,
               tmst=100 + i)
    ingest(store, s, frame(SL01, 200, 67, device_metrics(76, 3.87)), t, tmst=300)
    ingest(store, s, frame(SL01, 201, 3, position(0.0, 0.0, 0, int(t.timestamp()))), t, tmst=301)
    tv = views.track_view(store, s, SL01, store.flight(f["id"]), None, None)
    st = tv["stats"]
    assert st["fixes"] == 10 and st["max_alt_m"] == 230 + 9 * 150
    assert st["distance_m"] == pytest.approx(geo.distance_m(35.48, -80.83, 35.498, -80.83), rel=1e-6)
    assert st["ascent_rate_mps"] == pytest.approx(5.0, rel=0.01)
    assert len(tv["rejected"]) == 1 and tv["rejected"][0]["reject_reason"].startswith("0,0")
    status = views.status(store, s, {"state": "listening"})
    sl = status["trackers"][0]
    assert sl["battery"]["voltage"] == pytest.approx(3.87, abs=1e-4) and sl["battery"]["level"] == 76
    assert sl["observed_position_interval_s"] == pytest.approx(30, abs=1)
    assert sl["look"]["compass"] == "N" and sl["look"]["distance_m"] > 1000
    api = API(s, store, lambda: {})
    _, csv_body, h = api.get(f"/v1/stratolink/flights/{f['id']}/export.csv", {})
    assert csv_body.decode().count("\n") == 11 and "csv" in h["Content-Type"]
    _, gj, _ = api.get(f"/v1/stratolink/flights/{f['id']}/export.geojson", {})
    g = json.loads(gj)
    assert g["features"][0]["geometry"]["type"] == "LineString" and len(g["features"][0]["geometry"]["coordinates"]) == 10
    _, kml, _ = api.get(f"/v1/stratolink/flights/{f['id']}/export.kml", {})
    root = ET.fromstring(kml)
    assert root.tag.endswith("kml")
    store.end_flight(f["id"])
    with pytest.raises(ValueError):
        store.end_flight(f["id"])


def test_contact_states():
    assert views.contact_state(None, 180, 600) == "never"
    assert views.contact_state(10, 180, 600) == "ok"
    assert views.contact_state(200, 180, 600) == "late"
    assert views.contact_state(700, 180, 600) == "lost"


def test_preflight_separates_verified_from_manual(s, store):
    f = store.create_flight("Preflight", SL01, "ground_test", start=True)
    store.gateway_seen(EUI, "192.168.4.34", "PULL_DATA", dt.datetime.now(UTC), True)
    now = dt.datetime.now(UTC)
    ingest(store, s, frame(SL01, 1, 3, position(35.48, -80.83, 230, int(now.timestamp()))), now, tmst=1)
    ingest(store, s, frame(SL01, 2, 67, device_metrics(100, 4.1)), now, tmst=2)
    pf = views.preflight(store, s, store.flight(f["id"]))
    by = {c["item"]: c for c in pf["checks"]}
    assert by["gps_fix"]["ok"] and by["battery"]["ok"] and by["ground_station"]["ok"] and by["recording"]["ok"]
    assert all(by[i]["kind"] == "manual" and not by[i]["ok"] for i in views.MANUAL_ITEMS)
    assert not pf["go"]
    for i in views.MANUAL_ITEMS:
        store.confirm(f["id"], i, True, "tommy")
    assert views.preflight(store, s, store.flight(f["id"]))["manual_ok"] == 3


# ------------------------------------------------------------------ import
def test_import_is_idempotent(s, store, tmp_path):
    log = tmp_path / "packets.jsonl"
    lines = []
    for i, raw in enumerate([KNOWN, frame(SL01, 0xBEEF, 3, position(35.4835, -80.8272, 227, 1795798872)),
                             frame(SL01, 0xBEEF, 3, position(35.4835, -80.8272, 227, 1795798872), hop_limit=2)]):
        lines.append(json.dumps({"timestamp": f"2026-11-27T11:59:{30 + i:02d}.000000", "gateway": EUI,
                                 "packet": {k: v for k, v in rxpk(raw, tmst=1000 + i).items() if k != "data"} |
                                 {"data": base64.b64encode(raw).decode()}, "hex": raw.hex()}))
    lines.append("{broken")
    log.write_text("\n".join(lines) + "\n")
    assert import_log(store, s, log) == {"lines": 4, "added": 3, "skipped": 0, "errors": 1}
    assert import_log(store, s, log) == {"lines": 4, "added": 0, "skipped": 3, "errors": 1}
    assert store.one("SELECT COUNT(*) AS n FROM packets")["n"] == 2
    r = store.one("SELECT rx_utc FROM rx ORDER BY id LIMIT 1")
    assert r["rx_utc"].startswith("2026-11-27T16:59:30")          # 11:59 EST is 16:59 UTC


def test_import_of_the_real_receiver_log(s, store, tmp_path):
    real = pathlib.Path("/home/tommy/stratolink/receiver/packets.jsonl")
    if not real.exists():
        pytest.skip("no manual receiver log on this computer")
    copy = tmp_path / "copy.jsonl"
    copy.write_text(real.read_text())
    first = import_log(store, s, copy)
    assert first["errors"] == 0 and first["added"] == first["lines"]
    assert import_log(store, s, copy)["added"] == 0
    p = store.one("SELECT * FROM packets WHERE sender = ? AND packet_id = ?", (SL01, 0x898B22A6))
    assert json.loads(p["content"]) == {"text": "STRATOLINK TEST 001"}


# ------------------------------------------------------------------ control API
def test_control_api_over_http(s, store):
    server = ControlServer(API(s, store, lambda: {"state": "listening"}), "tok", "127.0.0.1", 0)
    server.start()
    assert server.bound.wait(5)
    base = f"http://127.0.0.1:{server.port}/v1/stratolink"

    def call(path, body=None, token="tok"):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                              "X-Starlab-User": "tester"}, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        assert call("", token="wrong")[0] == 401
        ingest(store, s, KNOWN, dt.datetime.now(UTC))
        code, st = call("")
        assert code == 200 and st["trackers"][0]["name"] == "Stratolink-01" and st["last_rx_id"] == 1
        code, live = call("/live?after=0")
        assert live["rx"][0]["content"]["text"] == "STRATOLINK TEST 001" and live["last_rx_id"] == 1
        assert call("/live?after=1")[1]["rx"] == []
        code, f = call("/flights", {"name": "API flight", "tracker": "!f661fb94", "mode": "ground_test"})
        assert code == 200 and f["flight"]["status"] == "planned"
        assert call(f"/flights/{f['flight']['id']}/start", {})[1]["flight"]["status"] == "active"
        assert call("/flights", {"name": "", "tracker": "!f661fb94"})[0] == 400
        assert call(f"/flights/{f['flight']['id']}/preflight", {"item": "gps_fix", "confirmed": True})[0] == 400
        code, pf = call(f"/flights/{f['flight']['id']}/preflight", {"item": "radio", "confirmed": True})
        assert code == 200 and next(c for c in pf["checks"] if c["item"] == "radio")["detail"].startswith(
            "confirmed by tester")
        code, prof = call("/profiles")
        assert set(prof["profiles"]) >= {"ground-test", "high-altitude", "pico"}
        bad = dict(prof["profiles"]["ground-test"])
        bad["settings"] = {**bad["settings"], "lora.modem_preset": "SHORT_FAST"}
        assert call("/profiles/ground-test", {"profile": bad})[0] == 400
        assert call("/trackers/!f661fb94/key", {"channel": "Balloons", "psk": "AQ=="})[0] == 200
        assert (os.stat(s.keys_file).st_mode & 0o777) == 0o600
        assert call("/gateways", {"eui": "2CF7F12004B90000", "name": "Ridge", "lat": 35.9, "lon": -81.2})[0] == 200
        assert call("/nope")[0] == 404
    finally:
        server.stop()


# ------------------------------------------------------------------ keys
def test_keys_file_must_be_private(s, tmp_path):
    p = pathlib.Path(s.keys_file)
    key = base64.b64encode(os.urandom(16)).decode()
    cfgmod.write_key(p, "Balloons", key)
    assert (p.stat().st_mode & 0o777) == 0o600
    assert cfgmod.read_keys(p) == {"Balloons": key}
    assert any(c.name == "Balloons" and not c.public for c in cfgmod.channels(s))
    p.chmod(0o644)
    with pytest.raises(cfgmod.KeyFileError):
        cfgmod.read_keys(p)
    with pytest.raises(ValueError):
        cfgmod.write_key(p.with_name("k2.toml"), "x", "bm90IGEga2V5")     # 9 bytes: not a PSK


def test_settings_refuse_unsafe_binds():
    with pytest.raises(ValueError):
        cfgmod.Settings(udp_host="0.0.0.0").validate()
    with pytest.raises(ValueError):
        cfgmod.Settings(allowed_sources=["0.0.0.0/0"]).validate()
    st = cfgmod.Settings(allowed_sources=["100.64.0.0/10", "192.168.4.34"])
    assert st.source_allowed("100.64.49.1") and st.source_allowed("192.168.4.34") and not st.source_allowed("8.8.8.8")


# ------------------------------------------------------------------ profiles
def test_profiles_validate_and_estimate(tmp_path):
    ps = profiles.Profiles(tmp_path / "profiles")
    gt = ps.get("ground-test")
    s_ = gt["settings"]
    assert (s_["position.position_broadcast_secs"], s_["position.gps_update_interval"],
            s_["telemetry.device_update_interval"], s_["channel.position_precision"]) == (60, 30, 300, 32)
    for slug in profiles.DEFAULT_PROFILES:
        r = profiles.resolve(ps.get(slug), "heltec_v4")
        assert r["errors"] == [], (slug, r["errors"])
        assert r["settings"]["device.role"] == "TRACKER" and r["settings"]["device.rebroadcast_mode"] == "NONE"
    pico = profiles.resolve(ps.get("pico"), "heltec_v4")["estimate"]
    hab = profiles.resolve(ps.get("high-altitude"), "heltec_v4")["estimate"]
    assert pico["mah_per_day"] < hab["mah_per_day"] and pico["airtime_s_per_day"] < hab["airtime_s_per_day"]
    assert profiles.airtime_s(55) == pytest.approx(0.641, abs=0.002)    # SF11/250 kHz, 16-symbol preamble
    errs, _ = profiles.validate({**s_, "lora.tx_power": 29}, "heltec_v4")
    assert any("28 dBm" in e for e in errs)
    errs, _ = profiles.validate({**s_, "lora.region": "EU_868"}, "heltec_v4")
    assert errs
    errs, warns = profiles.validate(dict(s_), "rak4631")
    assert not errs and any("planned" in w for w in warns)
    edited = {**gt, "settings": {**s_, "position.position_broadcast_secs": 120}}
    ps.save("ground-test", edited)
    profiles.Profiles(tmp_path / "profiles")                           # restart: the edit is kept
    assert ps.get("ground-test")["settings"]["position.position_broadcast_secs"] == 120
    assert ps.reset("ground-test")["settings"]["position.position_broadcast_secs"] == 60


# ------------------------------------------------------------------ tracker provisioning
def test_provisioning_bundle_report_and_rf_check(s, store):
    pv = Provisioning(s.path("provision"), store, profiles.Profiles(s.path("profiles")))
    b = pv.bundle("Stratolink-02", "SL02", "heltec_v4", "ground-test")
    assert b["settings"]["lora.modem_preset"] == "LONG_FAST" and b["test_text"].startswith("STRATOLINK TEST")
    with pytest.raises(ValueError):
        pv.bundle("Stratolink-01", "SL01", "heltec_v4", "ground-test")          # name taken
    with pytest.raises(ValueError):
        pv.bundle("Stratolink-03", "TOOLONG", "heltec_v4", "ground-test")
    report = {"kind": "stratolink-provision-report", "bundle_id": b["bundle_id"], "status": "ok",
              "identity": {"node_id": "!0a0b0c0d", "hw_model": "HELTEC_V4", "firmware_version": "2.7.15"},
              "identity_after": {"node_id": "!0a0b0c0d"}, "applied": b["settings"], "mismatches": {},
              "test": {"text": b["test_text"], "packet_id": "00000777", "sent_utc": iso(dt.datetime.now(UTC))}}
    with pytest.raises(ValueError):
        pv.accept({**report, "identity": {**report["identity"], "hw_model": "RAK4631"}})
    r = pv.accept(report)
    assert r["tracker"]["name"] == "Stratolink-02" and r["rf_test"]["state"] == "waiting"
    ingest(store, s, frame(0x0A0B0C0D, 0x777, 1, b["test_text"].encode()), dt.datetime.now(UTC))
    v = pv.rf_test(0x0A0B0C0D)
    assert v["state"] == "verified" and v["receptions"][0]["gateway"] == "Observatory"
    assert "private" not in json.dumps(store.tracker(0x0A0B0C0D)).lower()


def test_provision_tool_is_shipped():
    from aobs.stratolink.provision import TOOL
    src = TOOL.read_text()
    compile(src, str(TOOL), "exec")
    assert "private_key" not in src.replace("never reads, copies or writes the device's private key", "")


# ------------------------------------------------------------------ ground-station provisioning
class FakeGateway:
    """A SenseCAP M2 as seen over SSH: answers the inspect script and records every command and file."""

    def __init__(self, board="sensecap,wm7628n", modem="US915"):
        self.board, self.modem = board, modem
        self.files: dict[str, bytes] = {}
        self.commands: list[str] = []

    def __call__(self, cmd, stdin, timeout):
        script = cmd[-1]
        self.commands.append(script)
        if "board_name=" in script:
            out = (f"board_name={self.board}\nmodel=SenseCAP WM7628N\nopenwrt=OpenWrt 21.02.0\nopenwrt_revision=r16279\n"
                   f"eth0_mac=2c:f7:f1:20:04:c1\ndeviceinfo_eui=\ndeviceinfo_loraModem={self.modem}\n"
                   "pkt_fwd_sha256=3bcd\nservice_lora_pkt_fwd=enabled\nservice_station=disabled\n"
                   "service_stratolink=absent\nrunning=\nspidev=/dev/spidev0.1 /dev/spidev0.2\n")
            return 0, out.encode(), ""
        if script.startswith("cat > "):
            path = script.split()[2].strip("'").removesuffix(".new")
            self.files[path] = stdin
            return 0, b"", ""
        if script.startswith("cat /root/stratolink-backup"):
            return 0, b"\x1f\x8b fake tgz", ""
        if script.startswith("sha256sum /tmp/stratolink-fw.bin"):
            import hashlib
            return 0, (hashlib.sha256(self.files["/tmp/stratolink-fw.bin"]).hexdigest() + "\n").encode(), ""
        return 0, b"", ""


def test_gateway_deploy_against_fake(s, store, monkeypatch):
    fake = FakeGateway()
    gw = gwprov.Gateway("192.168.4.77", runner=fake)
    info = gw.inspect()
    assert info["compatible"] and info["eui"] == "2CF7F12004C10000"          # its own MAC, never a copy
    r = gwprov.deploy(gw, s, store, "Ridge", 35.9, -81.2, 400, log=lambda m: None)
    cfg = json.loads(fake.files["/root/stratolink.json"])
    gc = cfg["gateway_conf"]
    assert gc["gateway_ID"] == "2CF7F12004C10000" and gc["server_address"] == "127.0.0.1"
    assert cfg["SX130x_conf"]["radio_0"]["tx_enable"] is False and cfg["SX130x_conf"]["lorawan_public"] is False
    std = cfg["SX130x_conf"]["chan_Lora_std"]
    assert (std["bandwidth"], std["spread_factor"]) == (250000, 11)
    assert gwprov.radio_summary(cfg)["freq_mhz"] == pytest.approx(906.875)
    assert b"procd" in fake.files["/etc/init.d/stratolink"].lower() or b"USE_PROCD" in fake.files["/etc/init.d/stratolink"]
    joined = "\n".join(fake.commands)
    assert "for s in lora_pkt_fwd station" in joined and "/etc/init.d/$s stop" in joined and "/etc/init.d/$s disable" in joined and "stratolink enable" in joined
    assert joined.index("tar czf") < joined.index("/etc/init.d/stratolink enable")     # backup first
    g = store.gateway("2CF7F12004C10000")
    assert g["registered"] and g["name"] == "Ridge" and g["ip"] == "192.168.4.77"
    assert r["backup"].startswith("/root/stratolink-backup-")
    # the receiver now accepts it, verify sees its keepalive and a packet
    rx = Receiver(s, store)
    since = dt.datetime.now(UTC) - dt.timedelta(seconds=1)
    rx.handle(b"\x02\x00\x01\x02" + bytes.fromhex("2CF7F12004C10000"), ("192.168.4.77", 1700), lambda b, a: None)
    rx.handle(push(eui="2CF7F12004C10000", body={"rxpk": [rxpk(KNOWN, tmst=77)]}), ("192.168.4.77", 1700),
              lambda b, a: None)
    out = gwprov.verify(store, "2CF7F12004C10000", since, 5, 5, log=lambda m: None, sleep=lambda t: None)
    assert out["rf"]["status"] == "decoded"


def test_gateway_refuses_unsupported_hardware(s, store):
    for fake in (FakeGateway(board="seeed,other"), FakeGateway(modem="EU868")):
        gw = gwprov.Gateway("192.168.4.78", runner=fake)
        with pytest.raises(gwprov.GatewayError):
            gwprov.deploy(gw, s, store, "X", 1, 1, None, log=lambda m: None)
        with pytest.raises(gwprov.GatewayError):
            gwprov.flash(gw, s, "any", confirm=lambda q: "", log=lambda m: None)
        assert not any(c.startswith("cat > ") for c in fake.commands)            # nothing was written


def test_gateway_flash_checks_and_confirmation(s, tmp_path):
    rel = pathlib.Path(s.firmware_dir) / "r1"
    (rel / "images").mkdir(parents=True)
    img = rel / "images" / "fw-squashfs-sysupgrade.bin"
    img.write_bytes(b"IMAGE" * 100)
    (rel / "RELEASE.json").write_text(json.dumps({
        "title": "t", "hardware": {"board_name": "sensecap,wm7628n"},
        "files": [{"path": "images/fw-squashfs-sysupgrade.bin", "sha256": firmware.sha256(img), "kind": "image"}]}))
    fake = FakeGateway()
    gw = gwprov.Gateway("192.168.4.79", runner=fake)
    with pytest.raises(gwprov.GatewayError, match="not confirmed"):
        gwprov.flash(gw, s, "r1", confirm=lambda q: "yes", log=lambda m: None)
    assert not any("sysupgrade -n" in c for c in fake.commands)
    gwprov.flash(gw, s, "r1", confirm=lambda q: "FLASH 2CF7F12004C10000", log=lambda m: None)
    assert any(c.startswith("sysupgrade -n /tmp/stratolink-fw.bin") for c in fake.commands)
    img.chmod(0o644)
    img.write_bytes(b"tampered")
    assert firmware.file_for(pathlib.Path(s.firmware_dir), "r1", "images/fw-squashfs-sysupgrade.bin") is None
    assert firmware.file_for(pathlib.Path(s.firmware_dir), "r1", "../../etc/passwd") is None


def test_gateway_password_never_from_environment(monkeypatch):
    monkeypatch.setenv("SSHPASS", "hunter2")
    with pytest.raises(gwprov.GatewayError):
        gwprov.env_password_refused()


def test_template_strip_and_render():
    cfg = gwprov.json_loose("""{ "SX130x_conf": {"radio_0": {"freq": 906875000, "tx_enable": true},
        "chan_Lora_std": {"enable": true, "radio": 0, "if": 0, "bandwidth": 250000, "spread_factor": 11}},
        /* comment */ "gateway_conf": {"gateway_ID": "2CF7F12004B70000", "server_address": "192.168.4.66"} }""")
    t = gwprov.strip_device(cfg)
    assert "gateway_ID" not in t["gateway_conf"] and "server_address" not in t["gateway_conf"]
    out = gwprov.render(t, "AABBCCDDEEFF0000", "192.168.4.66", 1700)
    assert out["SX130x_conf"]["radio_0"]["tx_enable"] is False and out["gateway_conf"]["gateway_ID"] == "AABBCCDDEEFF0000"


def test_archived_release_if_present():
    root = pathlib.Path("/data/archive/stratolink/firmware")
    rel = next((r for r in firmware.releases(root) if r["id"] == "sensecap-m2-us915-meshtastic-2026.11.27"), None)
    if rel is None:
        pytest.skip("no firmware archive on this computer")
    img = next(f for f in rel["files"] if f["path"].endswith("sysupgrade.bin"))
    assert img["sha256"] == "2a220aa2cbaeda91cb6638da32456737ee4166084385a8a99134e91732131c69"
    assert firmware.file_for(root, rel["id"], img["path"]) is not None
