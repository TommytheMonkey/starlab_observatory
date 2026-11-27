# Tracker profiles

Profiles are starting points, not universally correct settings. They live in `/data/archive/stratolink/profiles/`
and are edited on Trackers → Profiles (JSON). Saving validates the profile against every hardware it names; a bundle
is only made from a profile that validates for that hardware.

| Setting | Ground Test | High-Altitude Flight | Long-Duration Pico |
|---|---|---|---|
| `position.position_broadcast_secs` | 60 | 60 | 900 |
| `position.gps_update_interval` | 30 | 30 | 600 |
| `telemetry.device_update_interval` | 300 | 300 | 1800 |
| `channel.position_precision` | 32 (full) | 32 | 32 |
| `lora.hop_limit` | 3 | 2 | 1 |
| `lora.tx_power` | 0 (firmware default) | 22 dBm | 17 dBm |
| `device.node_info_broadcast_secs` | 3600 | 10800 | 43200 |
| `bluetooth.enabled` | true | false | false |
| `power.is_power_saving` | false | false | true (ls_secs 300) |
| `display.screen_on_secs` | 60 | 10 | 1 |

All three: US region, `LONG_FAST` preset (required: the ground stations listen only there), role `TRACKER`,
`rebroadcast_mode NONE` (a tracker at altitude hears a whole region; relaying for it would flood the public mesh),
GPS enabled, smart position off (fixed intervals), position flags altitude/MSL/DOP/satellites/sequence/time/heading/
speed, MQTT ignored, Wi-Fi off.

`overrides` holds per-hardware changes, e.g. `{"rak4631": {"lora.tx_power": 22}}`.

## What the firmware keeps

On the public LongFast channel, Meshtastic 2.7.26 raises `position.position_broadcast_secs` to 3600 and every
telemetry interval to 1800 at boot (and the smart-broadcast minimum to 300). Every profile shows its **requested**
values next to what **will persist** for the chosen channel; power and airtime estimates use the persisted values.
On the private Stratolink channel the requested values stand. Details: [reporting-intervals.md](reporting-intervals.md).

## Validation

Errors (block a bundle): unknown settings, wrong types or ranges, region/preset other than US/LONG_FAST, TX power
above the board's maximum, `rebroadcast_mode NONE` with a role that does not accept it, precision 0.

Warnings (shown): GPS updates slower than position reports, positions more often than every 60 s on the shared
channel, hop limit above 3, reduced precision, a rebroadcasting airborne tracker, hardware that is not yet fully
supported, TX power 0 meaning "firmware default".

## Estimates

Each profile shows packets/day, airtime/day and duty cycle (LoRa time-on-air for SF11/250 kHz/4-5/16-symbol
preamble: a ~55-byte position frame is ~0.64 s), and an mAh/day figure from the hardware's power table. The Heltec V4
and RAK4631 figures are **nominal, not measured on Stratolink hardware**; measure with a USB power meter and correct
`profiles.HARDWARE`. Bluetooth, the screen and sensors are not included.

## Hardware notes

* **L76K above 18 km:** its behaviour above the usual GNSS altitude limit is not documented and Meshtastic exposes no
  airborne dynamic model for it. Verify (or bench-test with a simulator) before a flight expected to go higher.
* **u-blox M8 (RAK12500):** needs the airborne dynamic model above ~12 km; Meshtastic does not set it.
* **ESP32 power saving** applies only on battery and in some roles; measure.
