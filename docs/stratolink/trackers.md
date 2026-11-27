# Trackers

## Provisioning a new tracker (Heltec V4)

The observatory cannot reach your Mac's USB ports, so provisioning is split: Starlab makes a **bundle**, a small
Python tool on the Mac applies it over USB and writes a **report**, and Starlab registers the tracker from the report
and confirms over RF. Starlab → Stratolink → Trackers → *Provision a tracker* walks through it.

1. **Tool** (once per Mac): download `stratolink_provision.py` from the page, then
   ```bash
   python3 -m venv ~/.stratolink && ~/.stratolink/bin/pip install "meshtastic>=2.7"
   ~/.stratolink/bin/python stratolink_provision.py detect      # lists USB serial devices, identifies the board
   ```
2. **Bundle**: name (e.g. Stratolink-02), short name (SL02), hardware, profile → *Make bundle* downloads
   `Stratolink-02.bundle.json`. The profile is resolved for the hardware and validated first; problems block it.
3. **Run**: `~/.stratolink/bin/python stratolink_provision.py provision Stratolink-02.bundle.json`
   * finds the board (USB ID hint, then the firmware's own `hw_model`; anything else is refused);
   * reads node ID, hardware model and firmware version; firmware older than the hardware's minimum is refused with a
     pointer to the official web flasher (https://flasher.meshtastic.org/, target `heltec-v4`, **Update**, not full
     erase, so the board keeps its identity). The tool never flashes;
   * lists every setting it will change and asks; applies the owner name and all settings in one settings
     transaction (the board reboots);
   * reconnects, reads every setting back and records mismatches;
   * waits up to 5 min for a GPS fix (take it outdoors);
   * sends `STRATOLINK TEST <name> <id>` on the primary channel.
4. **Report**: upload `Stratolink-02.bundle.report.json` on the page. Starlab registers the tracker under the node
   ID the board reported, and shows *RF verified* once a ground station decodes the test message (with the gateways,
   RSSI and SNR).

The tool never reads, copies or writes private keys or channel keys; the only channel setting it touches is
position precision on the primary channel. Each tracker keeps the identity its firmware generated.

Manual steps that remain: installing the tool's Python environment once, firmware updates (web flasher), and
physically fitting the GPS antenna and battery.

## Channel: public LongFast or the private Stratolink channel

On the public LongFast channel Meshtastic 2.7.26 raises positions to ≥ 3600 s and telemetry to ≥ 1800 s at every
boot, whatever you request ([reporting-intervals.md](reporting-intervals.md)). For faster reporting choose
**Private Stratolink channel** in the wizard: the observatory creates one AES-256 key (once, in its mode-600 key
file), the bundle carries it, and the tool sets the primary channel to that name and key, pinned to slot 20
(906.875 MHz) so the ground stations still hear it. Optionally keep LongFast as a secondary channel (node info stays
public; telemetry is then still held to ≥ 1800 s). The bundle file contains the key: delete your copy afterwards.

The wizard and the Trackers table show **requested / persisted / observed** intervals: what the bundle asked for,
what the device kept after its reboot (the tool reads it back), and what the ground stations actually receive.

Re-provisioning an existing tracker (for example Stratolink-01 onto the private channel) uses its existing name; the
report must come from the same node ID.

## Discovered nodes and registration

Every node heard is listed under Trackers → Discovered nodes with its name, hardware, role, packet count and last
signal. *Register as tracker* starts tracking it (positions, telemetry, flights) from then on. Do not register other
people's nodes: their positions are deliberately kept only to ~1 km.

CLI: `python -m aobs.stratolink tracker register '!0a0b0c0d' Stratolink-02 --short-name SL02 --hardware heltec_v4`.

## Private channels

Trackers on the public LongFast channel need nothing. For a tracker on a private channel, Trackers → *Key* stores
the channel name and base64 PSK in `~/.config/starlab/stratolink-keys.toml` (mode 600, rewritten atomically; the
daemon refuses the file if its group or others can read it; it is never in the database or logs). The receiver
re-reads keys every minute. A packet on a channel with no key is filed as `encrypted`, with its header (sender,
packet ID, hops, signal) still recorded.

Note: encryption is not permitted on amateur-radio frequencies. If you operate under Part 97, use an unencrypted
channel; under Part 15 (licence-free ISM), encryption is allowed.

## Flights

Flights → New flight: name, tracker, mode (Flight / Ground test), optional loss-of-contact thresholds (default 180 s
late, 600 s lost), start now. A flight is a time window over a tracker's data, so replay, statistics and exports
(CSV, GeoJSON, KML) come from the same stored fixes. Only valid GPS fixes are exported; rejected ones are listed
with the reason on the page.

## Preflight

Preflight lists **verified** checks, computed from what the observatory actually received (registered, GPS fix,
recent POSITION over RF, battery voltage, observed position interval vs the provisioned one, a station online, RF
reception, flight recording, last known position), separately from **manual** confirmations (airspace, launch,
radio compliance), which are recorded with who and when and are never ticked automatically.
