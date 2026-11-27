"""Semtech UDP packet-forwarder protocol (PROTOCOL.TXT v1/v2, sx1302_hal 2.1), the server side, receive only.

Upstream datagrams: version u8 | token u16 | identifier u8 | gateway EUI (8 bytes, PUSH_DATA/PULL_DATA/TX_ACK) | body.
  PUSH_DATA (0x00) body is JSON {"rxpk": [...], "stat": {...}}  -> answer PUSH_ACK (0x01): version | token | 0x01
  PULL_DATA (0x02) keepalive, no body                            -> answer PULL_ACK (0x04): version | token | 0x04
  TX_ACK    (0x05) only follows a PULL_RESP, which this server never sends (no transmit path), so it is just counted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PUSH_DATA, PUSH_ACK, PULL_DATA, PULL_RESP, PULL_ACK, TX_ACK = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
KINDS = {PUSH_DATA: "PUSH_DATA", PULL_DATA: "PULL_DATA", TX_ACK: "TX_ACK"}
VERSIONS = (1, 2)
MAX_BODY = 65507


class ProtocolError(ValueError):
    pass


@dataclass
class Datagram:
    version: int
    token: bytes
    kind: int
    eui: str
    body: dict[str, Any] = field(default_factory=dict)
    body_error: str | None = None        # PUSH_DATA whose JSON did not parse (still acknowledged)

    @property
    def kind_name(self) -> str:
        return KINDS.get(self.kind, f"0x{self.kind:02x}")

    def ack(self) -> bytes | None:
        """The reply the gateway waits for, or None (TX_ACK needs none)."""
        if self.kind == PUSH_DATA:
            return bytes([self.version]) + self.token + bytes([PUSH_ACK])
        if self.kind == PULL_DATA:
            return bytes([self.version]) + self.token + bytes([PULL_ACK])
        return None


def parse(data: bytes) -> Datagram:
    if len(data) < 4:
        raise ProtocolError(f"{len(data)}-byte datagram is too short")
    version, token, kind = data[0], data[1:3], data[3]
    if version not in VERSIONS:
        raise ProtocolError(f"protocol version {version} is not 1 or 2")
    if kind not in KINDS:
        raise ProtocolError(f"identifier 0x{kind:02x} is not an upstream message")
    if len(data) < 12:
        raise ProtocolError(f"{KINDS[kind]} without a gateway EUI")
    d = Datagram(version, bytes(token), kind, data[4:12].hex().upper())
    if kind in (PUSH_DATA, TX_ACK) and len(data) > 12:
        try:
            body = json.loads(data[12:].decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("JSON body is not an object")
            d.body = body
        except (UnicodeDecodeError, ValueError) as exc:
            d.body_error = str(exc)[:200]
    return d


def rxpk_fields(p: dict[str, Any]) -> dict[str, Any]:
    """The reception metadata of one rxpk, typed, unknown/absent values as None."""
    def num(k: str, cast: type = float) -> Any:
        v = p.get(k)
        try:
            return cast(v) if v is not None and not isinstance(v, bool) else None
        except (TypeError, ValueError):
            return None
    sf = bw = None
    datr = p.get("datr")
    if isinstance(datr, str) and datr.startswith("SF") and "BW" in datr:
        try:
            sf, bw = int(datr[2:datr.index("BW")]), int(datr[datr.index("BW") + 2:])
        except ValueError:
            pass
    return {"tmst": num("tmst", int), "gw_time": p.get("time") if isinstance(p.get("time"), str) else None,
            "freq_mhz": num("freq"), "chan": num("chan", int), "rfch": num("rfch", int), "modu": p.get("modu"),
            "datr": datr if isinstance(datr, str) else None, "sf": sf, "bw_khz": bw,
            "codr": p.get("codr") if isinstance(p.get("codr"), str) else None,
            "rssi": num("rssi"), "rssis": num("rssis"), "snr": num("lsnr"), "foff_hz": num("foff"),
            "crc": num("stat", int), "size": num("size", int)}


STAT_FIELDS = ("time", "lati", "long", "alti", "rxnb", "rxok", "rxfw", "ackr", "dwnb", "txnb", "temp")


def stat_fields(s: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in STAT_FIELDS:
        v = s.get(k)
        if k == "time":
            out["gw_time"] = v if isinstance(v, str) else None
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
        else:
            out[k] = None
    return out
