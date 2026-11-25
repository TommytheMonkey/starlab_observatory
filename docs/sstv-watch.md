# SSTV watch

Listens for SSTV on the HF calling frequencies, decodes every image, files it in a gallery and posts good ones to
Slack. ISS images from pass recordings join the same gallery. Modelled on the [pirate watch](pirate-watch.md): its
own service (`starlab-sstv-watch`, user tommy), receivers borrowed through the broker's lease API, a control
endpoint the broker's web page talks to, Slack through the broker.

## How it works

1. **Receivers.** Each watcher (`[[sstvwatch.watchers]]`, OBS-R10 by default) holds its receiver through
   `POST /v1/lease`. The broker ends the lease 30 s before any scheduled recording on that receiver and refuses one
   during a reservation, so the watch always yields to bookings; it retries every `retry_lease_s` and resumes after.
   `antenna_port` makes the lease select an ANTSW-01 port (only for the switch's own receiver).
2. **Where it listens.** A weighted rotation over the channels (`plan.py`): by day (local time, `day_start_hour` to
   `day_end_hour`) 20 m has the most weight and the longest dwell, then 15/10 m; at night 40 and 80 m (80 m only at
   night). Two watchers never sit on the same channel. `python -m aobs.sstvwatch plan` prints the plan.
3. **Demodulation.** The receiver is tuned 12 kHz below the channel (off its DC spike); the channel is shifted to
   0 Hz, decimated to 12 kHz and demodulated: USB/LSB as a complex band-pass (300–3000 Hz) on the right sideband,
   FM with a discriminator.
4. **Detection.** Every second, the last 3 s of audio goes through the archive decoder's parity-checked VIS check
   (`sstvdec.find_vis_headers`: 1900 Hz leader ≥ 240 ms, 1200 Hz break, leader, start bit, 7 data bits, even parity,
   stop bit). Speech, carriers, AFSK and noise do not pass it. On a header the watch keeps the audio and the channel
   IQ from 2 s before it to the end of the image time of the mode the VIS names (+5 s); a dwell never ends during a
   transmission.
5. **Decode.** Each transmission is decoded in its own process (`decodeworker.py`, sstv-py) with the mode the VIS
   named, under the processing queue's protections: a timeout (`decode_timeout_s`, 120 s) and a heartbeat
   (`decode_heartbeat_stale_s`, 30 s), SIGTERM then SIGKILL. It never waits behind the broker's METEOR decodes.
6. **Keep or discard.** The decode-quality score (0–1, `quality.py`) is the median correlation of each scan line
   with the next, scaled down for nearly flat frames: noise scores ~0, a clean picture ~0.9. Below `min_quality`
   (0.35) the transmission is filed as `noise` (not shown unless asked) with its audio and IQ kept. SNR is the
   1100–2300 Hz band over the audio noise around it.
7. **Slack.** Each good decode: a thumbnail with frequency, sideband, band, mode, local time, receiver, quality and
   SNR (notification kind `sstv`, posted with the image by the broker's Slack service). More than `batch_max` (5) in
   `batch_window_s` (10 min): the rest go out as one batched message with the best six thumbnails when the window
   ends. ISS SSTV images are posted by the broker as soon as their pass is decoded, always (even with decoded-image
   notifications off or a quiet job).

## ISS

ISS passes are booked and recorded by the broker on the receiver that has them (OBS-R07, 437.550 MHz; 145.800 MHz
when that downlink is switched on in Settings → Satellites → Downlinks), and decoded by its SSTV decoder, which now
goes ahead of SatDump decodes in the processing queue. The watch files those images in its gallery (`source = iss`)
with a link back to the recording (`/rf#recording=<id>`).

## Web page

`/sstvwatch` (the **SSTV** button in Starlab's header, shown while the watch answers, with **● RX** while it
receives): each watcher's state and channel, release/resume, and the gallery, newest first, filtered by band, mode,
source and date. An image opens with its details, the transmission's audio, its channel IQ (cf32, 12 kHz complex), or
for ISS the pass recording.

## Files

`/data/archive/sstv-watch/`: `sstvwatch.sqlite3`, `status.json`, `control.json`, and
`images/YYYY/MM/DD/<id>.{png,thumb.jpg,wav,cf32}`.

## Commands

| Command | |
|---|---|
| `python -m aobs.sstvwatch run` | the daemon (the unit runs this) |
| `python -m aobs.sstvwatch status [--json]` | watchers, channels, counts |
| `python -m aobs.sstvwatch release [2h]` / `resume` | free the receivers / take them back |
| `python -m aobs.sstvwatch images [--all] [--band 20m] [--mode "PD 120"]` | the archive |
| `python -m aobs.sstvwatch plan [--hours 24] [--summary]` | where it would listen |
| `python -m aobs.sstvwatch decode FILE.wav` | detection + decode on any WAV |

## Setup

1. `sudo ~/starlab-dev/deploy/upgrade-from-head.sh` (the broker's side: `/sstvwatch`, the `sstv` notification kind,
   the Slack upload, the queue priority).
2. The broker must lend the HF receiver: `OBS-R10` in `[broker] receivers` in `/etc/starlab/starlab.toml`.
3. `cp ~/starlab-dev/deploy/sstvwatch.example.toml ~/.config/starlab/sstvwatch.toml` and edit; the broker token is read
   from `~/.config/observatory/broker.env` like the pirate watch's.
4. `sudo cp ~/starlab-dev/deploy/systemd/starlab-sstv-watch.service /etc/systemd/system/ && sudo systemctl daemon-reload
   && sudo systemctl enable --now starlab-sstv-watch`.

## Notes

- OBS-R10 needs an HF antenna on its HF input; it is not behind ANTSW-01 (whose MA-30 loop on port 8 serves the
  switch's own receiver).
- Tests (`tests/test_sstvwatch.py`) generate Robot 36, Martin 1, Scottie 1 and PD 120 with pysstv (an encoder only),
  modulate them onto USB/LSB IQ with noise, and check detection, decode, the noise threshold, the timeout and Slack
  batching; they never open a receiver.
