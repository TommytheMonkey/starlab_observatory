# Troubleshooting

| Symptom | Check |
|---|---|
| Header says **UDP port busy** | Something else holds 1700: `ss -ulpn \| grep 1700`. Usually the old `receiver.py`; stop it. |
| No **Stratolink** button in Starlab | The page shows it only while the daemon answers: `systemctl status starlab-stratolink`, `curl -s 127.0.0.1:8754/v1/stratolink` (401 = running). |
| Page: "Stratolink is not answering" | Same; also `broker_token_file` must be readable by tommy (the daemon logs "broker token … unreadable"). |
| Gateway **offline** | `python -m aobs.stratolink status`; `ss -ulpn`; on the gateway `logread -e lora_pkt_fwd`, `/etc/init.d/stratolink status`; its `server_address` must be the observatory's address; its address must be in `allowed_sources` or registered (status shows `rejected_source` counts). |
| Gateway online, no packets | RF: the tracker must be on US / LONG_FAST at 906.875 MHz (US slot 20, LongFast default). `rxnb` in the status report counts everything the radio heard; `rxok` CRC-valid; `rxfw` forwarded. rxnb > 0 with rxok 0 suggests a wrong sync word or bandwidth. |
| Packets **encrypted** | The tracker is on a channel with no key here (Trackers → Key), or it is a direct message (PKI, not decodable by design). |
| Tracker heard but **no position** | Its packets decode but no POSITION_APP: GPS off, no fix (Meshtastic sends no position without one), or precision 0. The console shows every packet; NODEINFO/TELEMETRY appear even without GPS. |
| Fix shown as **REJECTED** | The console says why (0,0, jump, altitude, GPS time). A cold-start fix far away is rejected until two fixes agree. |
| Wrong distance/bearing | The station's position: Ground stations → Edit. |
| Battery shows **EXT** | battery_level 101 is Meshtastic's "external power". |
| Positions less often than configured | Shown as "observed" on Mission Control and Preflight. Meshtastic skips a position when nothing changed with smart position on (the profiles turn it off), and the channel may be busy. |
| Provisioning tool: wrong hardware | The bundle was made for another board; make one for the board `detect` reports. |
| Provisioning tool: settings mismatch | Some firmware versions ignore or clamp a setting; the report lists which. Update firmware, adjust the profile. |
| RF test never verifies | The tracker's test message must reach a ground station: check it is in range and on LONG_FAST, and that the station is online. |
