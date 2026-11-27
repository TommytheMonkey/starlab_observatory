# Packet decoding

## Semtech UDP (gateway → observatory, port 1700)

| Byte | |
|---|---|
| 0 | protocol version (1 or 2) |
| 1-2 | random token |
| 3 | identifier: 0x00 PUSH_DATA, 0x02 PULL_DATA, 0x05 TX_ACK |
| 4-11 | gateway EUI |
| 12- | PUSH_DATA: JSON `{"rxpk": [...], "stat": {...}}` |

Answers: PUSH_DATA → `version, token, 0x01` (PUSH_ACK); PULL_DATA → `version, token, 0x04` (PULL_ACK). The ACK is
sent before anything is written to disk. TX_ACK is counted only (it follows downlinks, which this server never sends).
A PUSH_DATA with broken JSON is still acknowledged and logged as a bad datagram.

`rxpk`: `tmst` (concentrator µs counter), `freq`, `chan`, `rfch`, `stat` (1 CRC OK, -1 CRC bad, 0 no CRC), `modu`,
`datr` (`SF11BW250`), `codr`, `rssi`, `rssis`, `lsnr`, `foff`, `size`, `data` (base64 frame).
`stat`: `time`, `rxnb` (received), `rxok` (CRC OK), `rxfw` (forwarded), `ackr` (% of upstream datagrams
acknowledged), `dwnb`, `txnb`, `temp` (°C, SX1302 HAL v2), `lati/long/alti` (GPS or fake GPS only). Each report
covers the gateway's `stat_interval` (30 s).

## Meshtastic frame

| Bytes | Field |
|---|---|
| 0-3 | destination (LE u32; 0xFFFFFFFF broadcast) |
| 4-7 | sender node number (`!f661fb94`) |
| 8-11 | packet ID |
| 12 | flags: bits 0-2 hop_limit, bit 3 want_ack, bit 4 via_mqtt, bits 5-7 hop_start |
| 13 | channel hash = XOR(channel name bytes) XOR XOR(expanded key bytes) |
| 14 | next hop (last byte of a node number, 0 = none) |
| 15 | relay node (last byte of the relayer's number) |
| 16- | encrypted `Data` protobuf |

Hops taken = hop_start − hop_limit (firmware ≥ 2.3; hop_start 0 means unknown).

**Decryption:** AES-CTR with the channel key; nonce = packet ID (u64 LE) ‖ sender (u32 LE) ‖ 0 (u32). A 1-byte PSK
`n` expands to the default key `1PG7OiApB1nwvP+rz05pAQ==` with its last byte + (n − 1); `AQ==` (n = 1) is the public
LongFast key. Candidate channels are those whose hash matches; if none does, the default key is tried and believed
only if the result is a well-formed `Data` on a known port (custom-named channels using the public key). Otherwise
the packet is `encrypted`: a private channel or a PKI direct message.

**Protobufs** (meshtastic/protobufs 2.7, decoded by a small wire-format reader in `aobs/stratolink/meshtastic.py`, so
Starlab's venv needs no protobuf packages; cross-checked against the meshtastic package): `Data` (portnum, payload,
bitfield…), `Position` (latitude_i/longitude_i ×1e-7, altitude, time, ground_speed m/s, ground_track ×1e-5 °,
sats_in_view, PDOP ×0.01, precision_bits…), `Telemetry` → `DeviceMetrics` (battery_level, 101 = external power;
voltage; channel_utilization; air_util_tx; uptime) or `EnvironmentMetrics`, `User` (long/short name, hw_model, role).

## The verified packet

```
ffffffff94fb61f6a6228b89620800611499ffd8c3376e6814cb04fc7549381b5e8e1a1b73b5ef77b3
→ !f661fb94, packet 898b22a6, hop_limit 2 of 3 (relayed once, by …61), channel hash 0x08 (LongFast),
  TEXT_MESSAGE_APP "STRATOLINK TEST 001", bitfield 0
```

`python -m aobs.stratolink decode <hex>`; `tests/test_stratolink.py::test_known_rf_packet_regression`.

## Deduplication

* A **reception** (`rx`) is one frame heard by one gateway: identified by gateway EUI + concentrator counter + frame,
  so a log imported twice or a reception both logged and heard live is filed once.
* A **packet** is one Meshtastic message: sender + packet ID + hash of the encrypted body. A rebroadcast by another
  node (same body, lower hop_limit) and the same frame heard by a second gateway are further receptions of it:
  `rx_count`, `gateway_count`, best RSSI/SNR and fewest hops are kept, and every reception keeps its own RSSI, SNR and
  gateway. Positions and telemetry are filed once per packet.

## Position checks

Rejected (kept with the reason, never drawn or exported): no coordinates, 0,0, precision 0 (withheld), out of range,
altitude outside −500…60 000 m, GPS time before 2020 or in the future, or a jump from the last good fix faster than
`max_speed_mps` (150). If two consecutive fixes agree with each other but not with the last good one, the old
reference was wrong and the track continues from the new position.
