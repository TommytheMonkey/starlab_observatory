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
4. **Detect.** A run of unmasked bins more than 8 dB above the floor (after correcting for
   band-wide gain drift), 15–25 kHz wide, is a candidate. The strongest one wins.
5. **Record.** rtl_power stops and `rtl_fm -M fm -s 25000` tunes the run's power centroid. A
   noise squelch watches audio power above 5 kHz: an open channel measures about 6300 on R05,
   a carrier quiets it, and the reference adapts. The clip ends after 10 s without a carrier,
   the tail is trimmed to 1 s, and sweeping resumes.
6. **Transcribe.** `whisper_worker.py` runs in its own venv (`~/.local/share/starlab/whisper-venv`,
   faster-whisper large-v3, CUDA float16). It uses auto language and Silero VAD, plus a
   `translate` pass when the language is not English. A clip with no VAD speech, or with every
   segment's `no_speech_prob` above 0.6, is non-speech: its WAV is deleted and the hit is
   marked `no_speech`.
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

## Slack

The watch serves `http://100.64.49.37:8752/v1/piratewatch` (Tailscale address only, broker bearer token).
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

1. Copy the production broker token, once (needs sudo, since `/etc/starlab` is root-only):
   `sudo sh -c 'install -m 600 -o tommy -g tommy "$(python3 -c "import tomllib;print(tomllib.load(open(\"/etc/starlab/starlab.toml\",\"rb\"))[\"broker\"][\"token_file\"])")" /home/tommy/.config/observatory/broker.env'`
2. `cp deploy/piratewatch.example.toml ~/.config/starlab/piratewatch.toml` (`antenna_port = "direct"`).
3. `sudo install -m 644 deploy/systemd/starlab-pirate-watch.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now starlab-pirate-watch`

The unit runs from `~/starlab-dev` as tommy, so a restart picks up whatever that tree holds.

## Notes

- While the watch holds R05, the Slack agent's R05 tools get "leased by pirate-watch"; the agent can release it.
- Audio clips and transcripts stay on this machine. Nothing is posted to Slack.
