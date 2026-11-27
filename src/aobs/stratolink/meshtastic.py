"""Meshtastic over the air, receive only: the 16-byte radio header, channel-key AES-CTR, and the few protobuf messages a
balloon tracker sends (Data, Position, Telemetry, User), decoded with a small wire-format reader so the broker's venv
needs no meshtastic/protobuf packages. Field numbers are meshtastic/protobufs (mesh.proto, telemetry.proto, 2.7).

Radio header (little-endian): dest u32, sender u32, packet id u32, flags u8 (hop_limit bits 0-2, want_ack bit 3,
via_mqtt bit 4, hop_start bits 5-7), channel hash u8, next hop u8, relay node u8 (last byte of the relayer's node
number); then the encrypted Data protobuf. Nonce = packet id (u64 LE) | sender (u32 LE) | 0 (u32).
"""

from __future__ import annotations

import base64
import dataclasses
import struct
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HEADER_LEN = 16
BROADCAST = 0xFFFFFFFF
# The well-known key that a 1-byte PSK of 0x01 ("AQ==", the default LongFast channel) expands to
DEFAULT_KEY = base64.b64decode("1PG7OiApB1nwvP+rz05pAQ==")

PORTS = {0: "UNKNOWN_APP", 1: "TEXT_MESSAGE_APP", 2: "REMOTE_HARDWARE_APP", 3: "POSITION_APP", 4: "NODEINFO_APP",
         5: "ROUTING_APP", 6: "ADMIN_APP", 7: "TEXT_MESSAGE_COMPRESSED_APP", 8: "WAYPOINT_APP", 9: "AUDIO_APP",
         10: "DETECTION_SENSOR_APP", 11: "ALERT_APP", 12: "KEY_VERIFICATION_APP", 32: "REPLY_APP",
         33: "IP_TUNNEL_APP", 34: "PAXCOUNTER_APP", 64: "SERIAL_APP", 65: "STORE_FORWARD_APP",
         66: "RANGE_TEST_APP", 67: "TELEMETRY_APP", 68: "ZPS_APP", 69: "SIMULATOR_APP", 70: "TRACEROUTE_APP",
         71: "NEIGHBORINFO_APP", 72: "ATAK_PLUGIN", 73: "MAP_REPORT_APP", 74: "POWERSTRESS_APP",
         76: "RETICULUM_TUNNEL_APP", 77: "CAYENNE_APP", 256: "PRIVATE_APP", 257: "ATAK_FORWARDER"}
PORT = {v: k for k, v in PORTS.items()}
ROLES = {0: "CLIENT", 1: "CLIENT_MUTE", 2: "ROUTER", 3: "ROUTER_CLIENT", 4: "REPEATER", 5: "TRACKER", 6: "SENSOR",
         7: "TAK", 8: "CLIENT_HIDDEN", 9: "LOST_AND_FOUND", 10: "TAK_TRACKER", 11: "ROUTER_LATE", 12: "CLIENT_BASE"}
# HardwareModel (mesh.proto, meshtastic 2.7.11)
HW_MODELS = {0: "UNSET", 1: "TLORA_V2", 2: "TLORA_V1", 3: "TLORA_V2_1_1P6", 4: "TBEAM", 5: "HELTEC_V2_0", 6:
             "TBEAM_V0P7", 7: "T_ECHO", 8: "TLORA_V1_1P3", 9: "RAK4631", 10: "HELTEC_V2_1", 11: "HELTEC_V1", 12:
             "LILYGO_TBEAM_S3_CORE", 13: "RAK11200", 14: "NANO_G1", 15: "TLORA_V2_1_1P8", 16: "TLORA_T3_S3", 17:
             "NANO_G1_EXPLORER", 18: "NANO_G2_ULTRA", 19: "LORA_TYPE", 20: "WIPHONE", 21: "WIO_WM1110", 22: "RAK2560",
             23: "HELTEC_HRU_3601", 24: "HELTEC_WIRELESS_BRIDGE", 25: "STATION_G1", 26: "RAK11310", 27:
             "SENSELORA_RP2040", 28: "SENSELORA_S3", 29: "CANARYONE", 30: "RP2040_LORA", 31: "STATION_G2", 32:
             "LORA_RELAY_V1", 33: "T_ECHO_PLUS", 34: "PPR", 35: "GENIEBLOCKS", 36: "NRF52_UNKNOWN", 37: "PORTDUINO",
             38: "ANDROID_SIM", 39: "DIY_V1", 40: "NRF52840_PCA10059", 41: "DR_DEV", 42: "M5STACK", 43: "HELTEC_V3",
             44: "HELTEC_WSL_V3", 45: "BETAFPV_2400_TX", 46: "BETAFPV_900_NANO_TX", 47: "RPI_PICO", 48:
             "HELTEC_WIRELESS_TRACKER", 49: "HELTEC_WIRELESS_PAPER", 50: "T_DECK", 51: "T_WATCH_S3", 52:
             "PICOMPUTER_S3", 53: "HELTEC_HT62", 54: "EBYTE_ESP32_S3", 55: "ESP32_S3_PICO", 56: "CHATTER_2", 57:
             "HELTEC_WIRELESS_PAPER_V1_0", 58: "HELTEC_WIRELESS_TRACKER_V1_0", 59: "UNPHONE", 60: "TD_LORAC", 61:
             "CDEBYTE_EORA_S3", 62: "TWC_MESH_V4", 63: "NRF52_PROMICRO_DIY", 64: "RADIOMASTER_900_BANDIT_NANO", 65:
             "HELTEC_CAPSULE_SENSOR_V3", 66: "HELTEC_VISION_MASTER_T190", 67: "HELTEC_VISION_MASTER_E213", 68:
             "HELTEC_VISION_MASTER_E290", 69: "HELTEC_MESH_NODE_T114", 70: "SENSECAP_INDICATOR", 71:
             "TRACKER_T1000_E", 72: "RAK3172", 73: "WIO_E5", 74: "RADIOMASTER_900_BANDIT", 75: "ME25LS01_4Y10TD", 76:
             "RP2040_FEATHER_RFM95", 77: "M5STACK_COREBASIC", 78: "M5STACK_CORE2", 79: "RPI_PICO2", 80:
             "M5STACK_CORES3", 81: "SEEED_XIAO_S3", 82: "MS24SF1", 83: "TLORA_C6", 84: "WISMESH_TAP", 85: "ROUTASTIC",
             86: "MESH_TAB", 87: "MESHLINK", 88: "XIAO_NRF52_KIT", 89: "THINKNODE_M1", 90: "THINKNODE_M2", 91:
             "T_ETH_ELITE", 92: "HELTEC_SENSOR_HUB", 93: "MUZI_BASE", 94: "HELTEC_MESH_POCKET", 95:
             "SEEED_SOLAR_NODE", 96: "NOMADSTAR_METEOR_PRO", 97: "CROWPANEL", 98: "LINK_32", 99:
             "SEEED_WIO_TRACKER_L1", 100: "SEEED_WIO_TRACKER_L1_EINK", 101: "MUZI_R1_NEO", 102: "T_DECK_PRO", 103:
             "T_LORA_PAGER", 104: "M5STACK_RESERVED", 105: "WISMESH_TAG", 106: "RAK3312", 107: "THINKNODE_M5", 108:
             "HELTEC_MESH_SOLAR", 109: "T_ECHO_LITE", 110: "HELTEC_V4", 111: "M5STACK_C6L", 112:
             "M5STACK_CARDPUTER_ADV", 113: "HELTEC_WIRELESS_TRACKER_V2", 114: "T_WATCH_ULTRA", 115: "THINKNODE_M3",
             116: "WISMESH_TAP_V2", 117: "RAK3401", 118: "RAK6421", 119: "THINKNODE_M4", 120: "THINKNODE_M6", 121:
             "MESHSTICK_1262", 122: "TBEAM_1_WATT", 123: "T5_S3_EPAPER_PRO", 124: "TBEAM_BPF", 125: "MINI_EPAPER_S3",
             126: "TDISPLAY_S3_PRO", 127: "HELTEC_MESH_NODE_T096", 128: "TRACKER_T1000_E_PRO", 129: "THINKNODE_M7",
             130: "THINKNODE_M8", 131: "THINKNODE_M9", 132: "HELTEC_V4_R8", 133: "HELTEC_MESH_NODE_T1", 134:
             "STATION_G3", 135: "T_IMPULSE_PLUS", 136: "T_ECHO_CARD", 137: "SEEED_WIO_TRACKER_L2", 138:
             "CROWPANEL_P4", 139: "HELTEC_MESH_TOWER_V2", 140: "MESHNOLOGY_W10", 255: "PRIVATE_HW"}
LOC_SOURCES = {0: "unset", 1: "manual", 2: "internal", 3: "external"}


class DecodeError(ValueError):
    pass


# ------------------------------------------------------------------ protobuf wire format
def _varint(buf: bytes, i: int) -> tuple[int, int]:
    out = shift = 0
    while True:
        if i >= len(buf):
            raise DecodeError("truncated varint")
        b = buf[i]
        i += 1
        out |= (b & 0x7F) << shift
        if not b & 0x80:
            return out, i
        shift += 7
        if shift > 63:
            raise DecodeError("varint too long")


def fields(buf: bytes) -> list[tuple[int, int, Any]]:
    """Every (field number, wire type, raw value) in a message; raises DecodeError on malformed input."""
    out, i = [], 0
    while i < len(buf):
        key, i = _varint(buf, i)
        num, wt = key >> 3, key & 7
        if num == 0:
            raise DecodeError("field number 0")
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 1:
            if i + 8 > len(buf):
                raise DecodeError("truncated fixed64")
            v, i = buf[i:i + 8], i + 8
        elif wt == 2:
            n, i = _varint(buf, i)
            if i + n > len(buf):
                raise DecodeError("truncated length-delimited field")
            v, i = buf[i:i + n], i + n
        elif wt == 5:
            if i + 4 > len(buf):
                raise DecodeError("truncated fixed32")
            v, i = buf[i:i + 4], i + 4
        else:
            raise DecodeError(f"unsupported wire type {wt}")
        out.append((num, wt, v))
    return out


def _conv(kind: str, wt: int, v: Any) -> Any:
    if kind in ("uint32", "enum", "uint64"):
        if wt != 0:
            raise DecodeError(f"{kind} with wire type {wt}")
        return v & 0xFFFFFFFF if kind != "uint64" else v
    if kind == "int32":
        if wt != 0:
            raise DecodeError(f"int32 with wire type {wt}")
        v &= 0xFFFFFFFFFFFFFFFF
        return v - (1 << 64) if v >= 1 << 63 else v
    if kind == "sint32":
        if wt != 0:
            raise DecodeError(f"sint32 with wire type {wt}")
        return (v >> 1) ^ -(v & 1)
    if kind == "bool":
        if wt != 0:
            raise DecodeError(f"bool with wire type {wt}")
        return bool(v)
    if kind == "fixed32":
        if wt != 5:
            raise DecodeError(f"fixed32 with wire type {wt}")
        return struct.unpack("<I", v)[0]
    if kind == "sfixed32":
        if wt != 5:
            raise DecodeError(f"sfixed32 with wire type {wt}")
        return struct.unpack("<i", v)[0]
    if kind == "float":
        if wt != 5:
            raise DecodeError(f"float with wire type {wt}")
        return round(struct.unpack("<f", v)[0], 6)
    if kind in ("bytes", "message"):
        if wt != 2:
            raise DecodeError(f"{kind} with wire type {wt}")
        return bytes(v)
    if kind == "string":
        if wt != 2:
            raise DecodeError(f"string with wire type {wt}")
        return bytes(v).decode("utf-8", "replace")
    raise AssertionError(kind)


def parse(buf: bytes, schema: dict[int, tuple[str, str]]) -> dict[str, Any]:
    """Known fields by name (last one wins, as protobuf does); unknown fields are skipped."""
    out: dict[str, Any] = {}
    for num, wt, v in fields(buf):
        if num in schema:
            name, kind = schema[num]
            out[name] = _conv(kind, wt, v)
    return out


DATA = {1: ("portnum", "enum"), 2: ("payload", "bytes"), 3: ("want_response", "bool"), 4: ("dest", "fixed32"),
        5: ("source", "fixed32"), 6: ("request_id", "fixed32"), 7: ("reply_id", "fixed32"), 8: ("emoji", "fixed32"),
        9: ("bitfield", "uint32")}
POSITION = {1: ("latitude_i", "sfixed32"), 2: ("longitude_i", "sfixed32"), 3: ("altitude", "int32"),
            4: ("time", "fixed32"), 5: ("location_source", "enum"), 6: ("altitude_source", "enum"),
            7: ("timestamp", "fixed32"), 8: ("timestamp_millis_adjust", "int32"), 9: ("altitude_hae", "sint32"),
            10: ("altitude_geoidal_separation", "sint32"), 11: ("pdop", "uint32"), 12: ("hdop", "uint32"),
            13: ("vdop", "uint32"), 14: ("gps_accuracy", "uint32"), 15: ("ground_speed", "uint32"),
            16: ("ground_track", "uint32"), 17: ("fix_quality", "uint32"), 18: ("fix_type", "uint32"),
            19: ("sats_in_view", "uint32"), 20: ("sensor_id", "uint32"), 21: ("next_update", "uint32"),
            22: ("seq_number", "uint32"), 23: ("precision_bits", "uint32")}
USER = {1: ("id", "string"), 2: ("long_name", "string"), 3: ("short_name", "string"), 4: ("macaddr", "bytes"),
        5: ("hw_model", "enum"), 6: ("is_licensed", "bool"), 7: ("role", "enum"), 8: ("public_key", "bytes"),
        9: ("is_unmessagable", "bool")}
TELEMETRY = {1: ("time", "fixed32"), 2: ("device_metrics", "message"), 3: ("environment_metrics", "message"),
             4: ("air_quality_metrics", "message"), 5: ("power_metrics", "message"), 6: ("local_stats", "message"),
             7: ("health_metrics", "message"), 8: ("host_metrics", "message")}
DEVICE_METRICS = {1: ("battery_level", "uint32"), 2: ("voltage", "float"), 3: ("channel_utilization", "float"),
                  4: ("air_util_tx", "float"), 5: ("uptime_seconds", "uint32")}
ENVIRONMENT_METRICS = {1: ("temperature", "float"), 2: ("relative_humidity", "float"),
                       3: ("barometric_pressure", "float"), 4: ("gas_resistance", "float"), 5: ("voltage", "float"),
                       6: ("current", "float"), 7: ("iaq", "uint32"), 9: ("lux", "float"),
                       13: ("wind_direction", "uint32"), 14: ("wind_speed", "float")}
POWER_METRICS = {1: ("ch1_voltage", "float"), 2: ("ch1_current", "float"), 3: ("ch2_voltage", "float"),
                 4: ("ch2_current", "float"), 5: ("ch3_voltage", "float"), 6: ("ch3_current", "float")}


# ------------------------------------------------------------------ keys and channels
def expand_psk(psk: bytes) -> bytes | None:
    """A channel PSK as the firmware uses it: empty = no encryption (None); 1 byte n = the default key with its last
    byte + (n - 1) (n = 0 also means no encryption); 16 or 32 bytes = AES-128/256 as is."""
    if len(psk) == 0 or psk == b"\x00":
        return None
    if len(psk) == 1:
        return DEFAULT_KEY[:-1] + bytes([(DEFAULT_KEY[-1] + psk[0] - 1) & 0xFF])
    if len(psk) in (16, 32):
        return bytes(psk)
    raise ValueError(f"a channel PSK is 0, 1, 16 or 32 bytes, not {len(psk)}")


def channel_hash(name: str, key: bytes | None) -> int:
    """The 1-byte channel id the radio header carries: XOR of the name's bytes XOR the expanded key's bytes."""
    h = 0
    for b in name.encode():
        h ^= b
    for b in key or b"":
        h ^= b
    return h


@dataclass(frozen=True)
class Channel:
    name: str
    key: bytes | None                    # expanded; None = unencrypted channel
    public: bool = True                  # the key is a well-known default (shown as "public" in the UI)

    @property
    def hash(self) -> int:
        return channel_hash(self.name, self.key)

    @classmethod
    def from_psk(cls, name: str, psk_b64: str, public: bool | None = None) -> "Channel":
        raw = base64.b64decode(psk_b64, validate=True)
        return cls(name, expand_psk(raw), public=len(raw) <= 1 if public is None else public)


# The default-key channel names a stock radio can be on (modem presets): their hashes identify a packet's channel
PRESET_NAMES = ("LongFast", "LongSlow", "LongModerate", "LongTurbo", "MediumFast", "MediumSlow", "ShortFast",
                "ShortSlow", "ShortTurbo", "VLongSlow")
DEFAULT_CHANNELS = tuple(Channel(n, DEFAULT_KEY) for n in PRESET_NAMES)


def decrypt(key: bytes, packet_id: int, sender: int, data: bytes) -> bytes:
    nonce = struct.pack("<QII", packet_id, sender, 0)
    d = Cipher(algorithms.AES(key), modes.CTR(nonce)).decryptor()
    return d.update(data) + d.finalize()


# ------------------------------------------------------------------ packets
def node_id(num: int) -> str:
    return f"!{num & 0xFFFFFFFF:08x}"


def node_num(ident: str | int) -> int:
    if isinstance(ident, int):
        return ident & 0xFFFFFFFF
    s = ident.strip().lower()
    s = s[1:] if s.startswith("!") else s[2:] if s.startswith("0x") else s
    if len(s) != 8:
        raise ValueError(f"a node id is !xxxxxxxx (8 hex digits), not {ident!r}")
    return int(s, 16)


@dataclass
class Header:
    dest: int
    sender: int
    packet_id: int
    flags: int
    channel_hash: int
    next_hop: int
    relay_node: int

    @property
    def hop_limit(self) -> int:
        return self.flags & 0x07

    @property
    def want_ack(self) -> bool:
        return bool(self.flags & 0x08)

    @property
    def via_mqtt(self) -> bool:
        return bool(self.flags & 0x10)

    @property
    def hop_start(self) -> int:
        return (self.flags >> 5) & 0x07

    @property
    def hops_away(self) -> int | None:
        """Hops taken to reach the gateway; None for firmware older than 2.3 (hop_start 0 means it was not sent)."""
        return self.hop_start - self.hop_limit if self.hop_start and self.hop_start >= self.hop_limit else None

    def public(self) -> dict[str, Any]:
        return {"dest": node_id(self.dest), "broadcast": self.dest == BROADCAST, "sender": node_id(self.sender),
                "packet_id": f"{self.packet_id:08x}", "hop_limit": self.hop_limit, "hop_start": self.hop_start,
                "hops_away": self.hops_away, "want_ack": self.want_ack, "via_mqtt": self.via_mqtt,
                "channel_hash": self.channel_hash, "next_hop": self.next_hop, "relay_node": self.relay_node}


def parse_header(raw: bytes) -> Header:
    if len(raw) < HEADER_LEN:
        raise DecodeError(f"{len(raw)} bytes is shorter than the 16-byte Meshtastic header")
    dest, sender, pid = struct.unpack_from("<III", raw)
    flags, ch, nh, relay = raw[12:16]
    return Header(dest, sender, pid, flags, ch, nh, relay)


@dataclass
class Decoded:
    header: Header
    status: str                              # decoded | encrypted | malformed
    channel: str | None = None
    channel_public: bool | None = None
    portnum: int | None = None
    port: str | None = None
    data: dict[str, Any] = field(default_factory=dict)     # Data message fields (payload removed)
    content: dict[str, Any] = field(default_factory=dict)  # the decoded inner message, by port
    error: str | None = None

    def public(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["header"] = self.header.public()
        return d


def decode_position(b: bytes) -> dict[str, Any]:
    p = parse(b, POSITION)
    out: dict[str, Any] = {}
    if "latitude_i" in p:
        out["lat"] = p["latitude_i"] * 1e-7
    if "longitude_i" in p:
        out["lon"] = p["longitude_i"] * 1e-7
    if "altitude" in p:
        out["alt_m"] = p["altitude"]
    elif "altitude_hae" in p:
        out["alt_m"] = p["altitude_hae"]
        out["alt_ref"] = "hae"
    if p.get("time"):
        out["gps_time"] = p["time"]
    elif p.get("timestamp"):
        out["gps_time"] = p["timestamp"]
    if "ground_speed" in p:
        out["speed_mps"] = p["ground_speed"]
    if "ground_track" in p:
        out["heading_deg"] = p["ground_track"] * 1e-5      # firmware sends 1e-5 degrees
    for k in ("sats_in_view", "precision_bits", "fix_quality", "fix_type", "seq_number", "gps_accuracy"):
        if k in p:
            out[k] = p[k]
    for k in ("pdop", "hdop", "vdop"):
        if p.get(k):
            out[k] = p[k] / 100
    if "location_source" in p:
        out["location_source"] = LOC_SOURCES.get(p["location_source"], str(p["location_source"]))
    return out


def decode_telemetry(b: bytes) -> dict[str, Any]:
    t = parse(b, TELEMETRY)
    out: dict[str, Any] = {"time": t.get("time")}
    if "device_metrics" in t:
        out["kind"] = "device"
        out.update(parse(t["device_metrics"], DEVICE_METRICS))
    elif "environment_metrics" in t:
        out["kind"] = "environment"
        out.update({f"env_{k}" if k == "voltage" else k: v
                    for k, v in parse(t["environment_metrics"], ENVIRONMENT_METRICS).items()})
    elif "power_metrics" in t:
        out["kind"] = "power"
        out.update(parse(t["power_metrics"], POWER_METRICS))
    else:
        out["kind"] = next((k.removesuffix("_metrics") for k in t if k.endswith(("_metrics", "_stats"))), "other")
    return out


def decode_user(b: bytes) -> dict[str, Any]:
    u = parse(b, USER)
    out = {k: u[k] for k in ("id", "long_name", "short_name", "is_licensed") if k in u}
    if "hw_model" in u:
        out["hw_model"] = HW_MODELS.get(u["hw_model"], str(u["hw_model"]))
    if "role" in u:
        out["role"] = ROLES.get(u["role"], str(u["role"]))
    out["has_public_key"] = bool(u.get("public_key"))
    return out


def decode_content(port: str, payload: bytes) -> dict[str, Any]:
    if port == "TEXT_MESSAGE_APP":
        return {"text": payload.decode("utf-8", "replace")}
    if port == "POSITION_APP":
        return decode_position(payload)
    if port == "TELEMETRY_APP":
        return decode_telemetry(payload)
    if port == "NODEINFO_APP":
        return decode_user(payload)
    return {"bytes": len(payload)}


def decode(raw: bytes, channels: tuple[Channel, ...] | list[Channel] = DEFAULT_CHANNELS) -> Decoded:
    """A received LoRa frame. Never raises for bad input: a short or corrupt frame is `malformed`, a frame no known
    channel key opens (a private channel, or a PKI direct message) is `encrypted`."""
    try:
        h = parse_header(raw)
    except DecodeError as exc:
        return Decoded(Header(0, 0, 0, 0, 0, 0, 0), "malformed", error=str(exc))
    body = raw[HEADER_LEN:]
    if not body:
        return Decoded(h, "malformed", error="no payload after the header")
    candidates = [c for c in channels if c.hash == h.channel_hash]
    guessed = not candidates
    if guessed:
        # A channel with a name of its own but the default PSK (common on public meshes): try the default key, and
        # believe it only if the result is a well-formed Data message on a known port
        candidates = [Channel(f"unnamed 0x{h.channel_hash:02x}", DEFAULT_KEY)]
    last = None
    for c in candidates:
        plain = decrypt(c.key, h.packet_id, h.sender, body) if c.key else body
        try:
            d = parse(plain, DATA)
            if "portnum" not in d:
                raise DecodeError("Data message without a portnum")
            if guessed and d["portnum"] not in PORTS:
                raise DecodeError("not a known port")
            port = PORTS.get(d["portnum"], f"PORT_{d['portnum']}")
            content = decode_content(port, d.get("payload", b""))
        except DecodeError as exc:            # wrong key (same hash), or a corrupt frame
            last = str(exc)
            continue
        data = {k: v for k, v in d.items() if k != "payload"}
        data["payload_len"] = len(d.get("payload", b""))
        return Decoded(h, "decoded", channel=c.name, channel_public=c.public, portnum=d["portnum"], port=port,
                       data=data, content=content)
    if guessed:
        return Decoded(h, "encrypted", error=f"no configured channel has hash 0x{h.channel_hash:02x} "
                                             "(a private channel, or a direct message)")
    return Decoded(h, "encrypted", error=f"channel hash 0x{h.channel_hash:02x} matches "
                                         f"{', '.join(c.name for c in candidates)} but the key does not open it ({last})")
