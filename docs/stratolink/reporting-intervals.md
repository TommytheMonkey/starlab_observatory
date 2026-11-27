# Why 60-second reports revert to 3600 / 1800 seconds

**Observed** on Stratolink-01 (`!f661fb94`, Heltec V4, Meshtastic **2.7.26.54e0d8d**), primary channel LongFast with
the default key: setting `position.position_broadcast_secs = 60` and `telemetry.device_update_interval = 60` is
accepted, then the device reports 3600 and 1800. Smart broadcasting was on (100 m, 300 s). The observatory's own log
agrees: one position at 17:01, then none; telemetry at 17:21 and 17:52 (≈ 1800 s).

## Cause (firmware source, tag v2.7.26.54e0d8d, commit 54e0d8d0ab2ff56b3a9ce967e53f79e49af560fb)

`src/mesh/Default.h` lines 16-22:

```c
#define min_default_telemetry_interval_secs IF_ROUTER(ONE_DAY / 2, 30 * 60)   // 1800 s (routers 43200)
#define min_default_broadcast_interval_secs IF_ROUTER(ONE_DAY / 2, 60 * 60)   // 3600 s (routers 43200)
#define min_default_broadcast_smart_minimum_interval_secs 5 * 60              // 300 s
```

`src/mesh/NodeDB.cpp` lines 333-365, in the `NodeDB` constructor, which runs **at every boot**:

* if `channels.hasDefaultChannel()`, every telemetry interval (device, environment, air quality, power, health) is
  raised to at least 1800 s;
* if the channel that carries positions (the first with `position_precision != 0`) is a default channel,
  `position_broadcast_secs` is raised to at least 3600 s and `broadcast_smart_minimum_interval_secs` to at least 300 s.

`Default::getConfiguredOrMinimumValue` (Default.cpp line 63) leaves 0 (meaning "use the default") alone and raises
anything below the minimum. The admin path that writes the config does not clamp, so the value is accepted and
saved; it is the next boot (the reboot that applying settings causes) that raises it. That is the "accepted, then
reverts" behaviour.

What counts as a **default channel** (`src/mesh/Channels.cpp` lines 376-401): PSK exactly `AQ==` (1 byte, 0x01)
**and** a name equal to the modem preset's display name ("LongFast" for LONG_FAST; an empty name shows as that).
`hasDefaultChannel()` additionally requires `use_preset`, the preset's default frequency slot, and no
`override_frequency`.

Not a cause: the role. TRACKER is exempt from congestion scaling of intervals (Default.cpp, `getConfiguredOrDefaultMsScaled`),
and `IF_ROUTER` applies the longer limits only to ROUTER/ROUTER_LATE.

Also relevant for balloons (`src/modules/PositionModule.cpp` lines 438-470 and 508-520): smart broadcasting compares
**horizontal** distance only, so a balloon climbing nearly straight up does not trigger it; and a TRACKER skips sending
while channel utilisation is above 40 %.

## What to do

Choose per tracker; nothing is changed on existing hardware automatically.

| Option | Positions | Telemetry | Seen by the public mesh | Ground stations |
|---|---|---|---|---|
| **Public LongFast** (today) | ≥ 3600 s | ≥ 1800 s | yes | decode with the public key |
| **Private Stratolink channel** | as configured (e.g. 60 s) | as configured | no (relays still forward it) | decode with the key in `stratolink-keys.toml` |
| Private + LongFast as secondary | as configured | ≥ 1800 s (a default channel still exists) | node info only | as above |

The private channel keeps the RF configuration exactly: the primary channel's name would normally choose the
frequency slot, so the provisioning tool pins `lora.channel_num = 20`, which is LONG_FAST's US slot,
906.875 MHz (902 + 0.125 + 19 × 0.25 MHz). The SenseCAP firmware and gateway config are untouched; the gateway
forwards every frame with a valid CRC regardless of channel, and the observatory decodes both the public channel and
the private one (`config.channels`).

Respect the public mesh: the minimums exist because frequent positions on the shared default channel use everyone's
airtime. A private channel's packets still occupy the same frequency; the profiles keep positions at ≥ 60 s.

## In Stratolink

* Profiles and bundles show **requested** and **will persist** for every interval the firmware rule changes
  (`profiles.firmware_effect`, rules version 2.7.26.54e0d8d).
* Trackers → Provision → *Channel*: **Public LongFast** or **Private Stratolink channel**. The private channel key
  (AES-256, made once from the OS random source) is created on the observatory and stored only in the mode-600 key
  file; it is never replaced from the page. A private bundle file contains the key (mode 600 on the observatory):
  delete your downloaded copy after provisioning.
* The tool reads every setting back after the reboot and reports values the firmware raised as **persisted
  differently** (with the rule), not as failures.
* The Trackers table shows, per tracker, **requested / persisted / observed** for positions and telemetry; observed is
  measured from what the ground stations actually received.

To move Stratolink-01 to the private channel later: make a private bundle for it (same name) and run the tool with it.
Until then it stays on LongFast, and Stratolink shows its 3600 s / 1800 s as persisted.
