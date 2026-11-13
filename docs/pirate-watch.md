# Satcom pirate watch (OBS-R05)

Watches the UHF military satcom downlink band (243–270 MHz) for intermittent voice channels,
records each burst as NFM audio, and transcribes it with Whisper on the local GPU.

Code: `src/aobs/piratewatch/`. Run: `python -m aobs.piratewatch <command>` from the starlab-dev venv.

## How it works

1. **Receiver.** The watch borrows OBS-R05 through the Starlab broker's ad-hoc lease
   (`POST /v1/lease`, owner `pirate-watch`), the same way the Slack agent does. Scheduled
   recordings still win: the broker caps the lease before a reservation, and a job that starts
   ends it. The watch renews every 5 s and stops its radio as soon as the lease ends or is
   about to end, then retries every 60 s. It never starts while another process (for example,
   SDR++ server, which bypasses the broker) has the dongle open.
2. **Sweep.** `rtl_power -f 243M:270M:10k -i 2 -c 20%` runs continuously. rtl_power rounds to a
   power-of-two FFT, so the bins are about 5.07 kHz (10,600 per sweep). There is one full sweep
   every 2 s. Every sweep is appended to `history/` (uint8, 0.5 dB steps).
3. **Baseline.** The first 600 sweeps (about 20 min, persisted in `baseline.npz`) set a per-bin
   noise floor: the 25th percentile, smoothed by a 300 kHz running median so carriers don't
   lift their own floor. They also give a duty cycle. Bins above the threshold in half or more
   of those sweeps are steady carriers or spurs, and are masked (±1 bin). Detection already
   runs on a provisional baseline after 20 sweeps. A clip whose carrier never drops for
   `max_clip_s` is a steady carrier too, and its channel is masked afterwards.
   `comb_masks` adds configured ranges that are always masked, learned or not: each entry is
   `[a tooth Hz, spacing Hz, half width Hz]`, and every tooth across the band is masked. R05's chain has a 750 kHz
   comb (exact harmonics of ~750.014 kHz, present at all 36 positions 243.75-269.25 MHz, often bursting 20 dB+ above
   the floor), so the station config sets `comb_masks = [[245_254_000, 750_000, 10_000]]`.
4. **Detect.** A run of unmasked bins more than 8 dB above the floor (after correcting for
   band-wide gain drift), 15–25 kHz wide, is a candidate. The strongest one wins.
5. **Record.** rtl_power stops and `rtl_fm -M fm -s 25000` tunes the run's power centroid. A
   noise squelch watches audio power above 5 kHz: an open channel measures about 6300 on R05,
   a carrier quiets it, and the reference adapts. The clip ends after 10 s without a carrier,
   the tail is trimmed to 1 s, and sweeping resumes. If rtl_fm or rtl_power stops producing data for `stall_s`
   (15 s), the receiver has gone away (R05 dropping off USB leaves rtl_fm hung rather than exiting): the clip so far
   is kept, the watch shows state `error`, hands the lease back and retries after 30 s.
6. **Transcribe.** `whisper_worker.py` runs in its own venv (`~/.local/share/starlab/whisper-venv`,
   faster-whisper large-v3, CUDA float16). It uses auto language and Silero VAD, plus a
   `translate` pass when the language is not English. A clip with no VAD speech, or with every
   segment's `no_speech_prob` above 0.6, is non-speech: the hit is marked `no_speech`, with what Whisper heard.
   Its WAV is kept for `no_speech_keep_h` (24 h), so Whisper's calls can be checked by ear, and then deleted.
7. **Log.** `piratewatch.sqlite3` holds:
   - `hits`: frequency, channel, UTC start and end, time on the air, peak dB, SNR, transcript,
     translation, audio path, status.
   - `activity`: per channel (5 kHz raster) and UTC hour, the detections, seconds on the air and
     speech clips.

   `heatmap.html` is re-rendered after every hit.

Data lives in `/data/archive/pirate-watch/` (clips under `clips/YYYY-MM-DD/`).

## Antenna and bias tee

OBS-R05 is wired straight to the discone (owner, 2026-11-11), so `antenna_port = "direct"` (the default):
the lease never selects an ANTSW-01 port. There is no amplifier in that line, so the bias tee must stay off:

- the rtl_power and rtl_fm commands never carry `-T`, and the radio layer refuses any command that does;
- under every new lease, before any radio work, the watch reads the EEPROM (`rtl_eeprom -d N`, read only). If it
  reports "Bias Tee always on: yes", librtlsdr would switch the tee on at every open whatever the command says, so
  the watch hands the lease back, shows state `blocked`, and checks again every 5 minutes;
- otherwise it switches the bias tee off by serial (librtlsdr, as the scheduler's recorder does), since the GPIO
  keeps whatever the previous user of the dongle left.

R05's EEPROM flag was cleared on 2026-11-11 (`rtl_eeprom -d 1 -b 0`, read back "Bias Tee always on: no"); it took
effect at the replug on 2026-11-12; the watch also switches the tee off by serial. The chain is Discone > Nooelec LaNA
(own USB power) > OBS-R05. Gain check 2026-11-12 with the LaNA powered (243-270 MHz, worst of three 2.4 MHz windows):

| gain dB | 7.7 | 12.5 | 16.6 | 20.7 | 25.4 | 29.7 | 38.6 | 49.6 |
|---|---|---|---|---|---|---|---|---|
| IQ RMS (LSB) | 3.0 | 5.4 | 9.5 | 15.6 | 23.7 | 38.3 | 54.9 | 58.7 |
| ADC clipping | 0 | 0 | 0 | 0 | 0.04% | 0.8% | 7% | 7% |

`gain_db` is 16.6: no clipping, about 9 dB below where it starts, and with the LaNA in front more dongle gain buys
no sensitivity. Run `relearn` after any change to the chain or the gain.

## Web page

`/piratewatch` on Starlab (same address and sign-in as `/rf`; a **Pirate Watch** button appears in the `/rf` header
while the watch answers, with **● LIVE** while it records). Works on a phone.

- Status: running / learning / recording / blocked / released, the channel being recorded, uptime, baseline progress,
  and Release (30 min to 4 h, or until resumed) / Resume.
- Hits, newest first, with UTC and local time, duration, peak and SNR, language, transcript, English translation and an
  audio player. Filters: speech only (on by default; off shows non-speech and missed bursts too), frequency (one
  channel like `260.2545`, or a range like `260.2-260.3`), and a date range. The filters live in the URL.
- Heatmap: channels with activity x UTC hour of day, by detections, seconds on the air or speech clips; tap a cell to
  list its hits.
- What the watch is ignoring (`/piratewatch#ignored` opens it): the learned steady carriers and spurs, with how often
  each sat above the floor, and channels auto-masked after a clip that never went quiet. Read-only.

Starlab never opens the watch's files: the broker forwards the page's calls to the watch's control endpoint on
127.0.0.1:8752 with its own bearer token (`[piratewatch] url` in starlab.toml overrides the address). Only the
read-only views, the clips (by hit id), and release / resume are forwarded. When the watch is stopped the page says so.

Install or update it with one command, which waits for a quiet moment (watch not recording, no scheduled capture
running or due within 10 minutes) before restarting anything, and keeps a backup of what it replaces:
`sudo /home/tommy/starlab-dev/deploy/install-piratewatch-web.sh`

## Slack

The watch serves `http://<observatory-tailscale-ip>:8752/v1/piratewatch` (Tailscale address, and 127.0.0.1 for the web page;
broker bearer token).
The Slack agent's `pirate_watch` tool uses it: "free R05 for an hour" → release for 60 minutes (it also drops the
agent's own R05 lease), "give R05 back to the pirate watch" → resume, or status.

## Commands

| | |
|---|---|
| `run` | the daemon (the systemd unit runs this) |
| `status` | state, lease, baseline progress, hit counts |
| `release [2h]` | stop the radio and hand R05 back, so SDR++ server can use it; with a duration, resume automatically (also from Slack) |
| `resume` | take R05 back (waits until SDR++ has let go) |
| `hits [--all]` | speech hits, or every detection with `--all` |
| `heatmap` | ASCII heatmap, and writes `heatmap.html` |
| `baseline` / `relearn` | show or relearn the masked steady carriers |
| `transcribe FILE` | run the Whisper worker on one file |

## Setup

1. Copy the production broker token, once (needs sudo, since `/data/starlab/secrets` is starlab-only). The broker
   reads it from `[broker] token_file`, which defaults to `<data_root>/secrets/api_token` (a bare token) and is
   usually not written in starlab.toml, so resolve it with Starlab's own config loader:
   `sudo install -m 600 -o tommy -g tommy "$(sudo /opt/starlab/venv/bin/python3 -c 'from aobs.config import load_config; print(load_config("/etc/starlab/starlab.toml").section("broker")["token_file"])')" /home/tommy/.config/observatory/broker.env`
2. `cp deploy/piratewatch.example.toml ~/.config/starlab/piratewatch.toml` (`antenna_port = "direct"`).
3. `sudo install -m 644 deploy/systemd/starlab-pirate-watch.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now starlab-pirate-watch`

The unit runs from `~/starlab-dev` as tommy, so a restart picks up whatever that tree holds.

## Notes

- While the watch holds R05, the Slack agent's R05 tools get "leased by pirate-watch"; the agent can release it.
- Audio clips and transcripts stay on this machine. Nothing is posted to Slack.
