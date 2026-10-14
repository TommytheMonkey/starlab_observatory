# Unified scheduler

The broker (`aobs broker`, on observatory at 100.64.49.37:8751) is the single schedule and
reservation authority for every SDR. Each reservation is a row in `sched_jobs`, keyed by the
physical device serial. Manual Slack jobs, automatic METEOR passes and windows imported from the
old recorders all share that one table, and all of them appear on the Satellite Observatory calendar.

## Rules

1. Locks are per physical SDR serial (`OBS-R01`, `OBS-R02`, `OBS-R05`, `240526D360` = RSP1B).
   USB indexes are rejected. Jobs on different receivers may overlap freely.
2. Booking is transactional. Validation, the conflict check (reservation = start − pre-roll to
   end + post-roll) and the insert run under one process lock, an fcntl lock and one DB
   transaction, so concurrent requests cannot double-book a device. Multi-job requests are
   `all_or_nothing` unless they say `"transaction_mode": "allow_partial"`.
3. A running job (`preparing`/`recording`) is never interrupted, and a conflicting request is
   rejected.
4. Manual jobs beat broker-run automatic passes. The automatic job is kept with status
   `preempted`, a reason, and `preempted_by`, and Slack is told. If the manual job is cancelled,
   the next plan restores the pass. Passes within `preempt_cutoff_seconds` (180 s) of starting
   are not preempted.
5. Windows of a receiver still recorded by an old service (`legacy`/`shadow` mode) cannot be
   preempted (that service cannot be told to skip), so a conflicting manual job is rejected.
6. Settings are validated per hardware type and never rewritten. Defaults are applied only to
   non-RF optional fields and are listed in `resolved.defaults_applied`.
7. The SDR records `schedule.start` to `schedule.end`. Pre/post-roll only extend the
   reservation (setup and teardown time).
8. Receive only. No transmit path exists.

## Hardware rules (from `/radio-capabilities`)

| | RTL-SDR (R01, R02, R05) | SDRplay RSP1B |
|---|---|---|
| engine / format | `rtl_sdr` / `cu8` | `soapysdr` / `cs16` or `cf32` |
| frequency | 24–1766 MHz | 1 kHz–2 GHz |
| sample rate | 225 001–300 000 or 900 001–2 560 000 | 2–10 MS/s |
| bandwidth | must be `null` | one of 200k, 300k, 600k, 1.536M, 5M, 6M, 7M, 8M |
| gain | `gain_db` from the R820T step list, or `gain_mode: auto` | `gain_elements: {"IFGR": 20-59, "RFGR": LNA state}`; `gain_db` must be null; `auto` = IF AGC (`agc: true`) and still needs RFGR |
| must be false | agc, iq/dc correction, direct sampling, offset tuning, doppler | direct sampling, offset tuning, doppler |
| bias tee | R01/R02 have none; R05 has one but cannot be switched per serial (`false` only) | `true`/`false`, must be explicit |

R05 is fed by ANTSW-01, so `receiver.antenna` must name a switch port antenna. A job pauses
ADS-B and ends any Slack-agent lease on R05. Agent leases are capped to end 30 s before the next
reservation.

SatDump (`decode_after_recording`): the pipeline must be installed, `input_type: baseband`,
`baseband_format` must equal the recording format, and `sample_rate_hz` must equal the recorded
rate. Decoding runs as a processing attempt after the recording ends. When `satdump.frequency_hz`
differs from the center frequency, the channel is shifted to 0 Hz and decimated by the broker's own
DSP stage before SatDump runs (see docs/COMMANDS.md → Decoding).

## Job states

`scheduled → preparing → recording → recorded → processing → completed`, or `failed`,
`cancelled`, `preempted`, `skipped`, `missed`, or `partial` (a running recording terminated on
confirmed request; the IQ is kept). Legacy windows end as `recorded`/`failed`/`missed`
from the old recorder's own metadata. Every transition is in `sched_job_events`. Every
recording gets a JSON sidecar (`<raw>.json`) with the full request, resolved settings, capture
result, checksum, plots and SatDump outcome.

## Migrating the automatic recorders

Each receiver has a `mode` in `config/observatory.toml` under `[scheduler.receivers.<id>]`:

| mode | old service | broker |
|---|---|---|
| `legacy` | records | imports its predicted windows as locked rows; conflicting manual jobs are rejected |
| `shadow` | records | also plans its own automatic jobs (status `shadow`, never executed) |
| `broker` | stopped, disabled, masked | plans and records automatic jobs; manual jobs may preempt them |

Steps:

1. Run the new broker with R01 and RSP1B in `legacy` (or straight to `shadow`).
2. Leave both in `shadow` for about a day. `aobs schedule shadow` compares each shadow window
   with when the old service actually started and stopped, using the old recorder's own
   metadata. It reports `agrees: true` after at least 3 passes start within 15 s.
3. Run `sudo deploy/sudo/migrate-recorders.sh` (optionally naming `OBS-R01` or `RSP1B`). It
   refuses unless a preflight passes:
   - no broker recording is running;
   - nothing on those receivers starts in the next 20 minutes;
   - the old service isn't writing a file;
   - the shadow comparison agrees.

   It then:
   - stops, disables and masks the old units (plus the dormant `observatory-record@OBS-R02`
     and `observatory-sdr-scheduler`);
   - sets the modes to `broker`;
   - retires satops' calendar events for R01;
   - installs `deploy/broker/observatory-broker.service` (`KillMode=process`, so a restart
     never kills a recording).
4. Undo with `sudo deploy/sudo/rollback-recorders.sh`.

## Slack

The full command reference and the Slack app setup are in `docs/COMMANDS.md` and the final
deployment checklist. Replies and notifications go to the owner's DM with the bot, one thread per
recording. You can also paste `/radio-schedule` JSON straight into that DM.

### Generating jobs with ChatGPT

Run `/radio-capabilities`. Give ChatGPT the attached `observatory-capabilities.json` together
with the job schema, ask it for a `/radio-schedule` JSON, and paste the result. Use `dry-run`
first if you want to check without booking.

## Example jobs

METEOR M2-4 on R02 (RTL-SDR, discone), decoded after recording:

```json
{"jobs": [{
  "job_name": "m2-4-discone-test",
  "target": {"type": "satellite", "name": "METEOR-M2-4", "satellite": "METEOR-M2-4", "mode": "LRPT"},
  "schedule": {"start": "2026-10-14T00:47:00-04:00", "end": "2026-10-14T01:03:00-04:00",
               "timezone": "America/New_York", "pre_roll_seconds": 60, "post_roll_seconds": 60},
  "receiver": {"receiver_id": "OBS-R02", "driver": "rtlsdr", "serial": "OBS-R02", "exclusive_lock": true},
  "radio": {"center_frequency_hz": 137500000, "target_frequency_hz": 137900000, "sample_rate_hz": 2400000,
            "bandwidth_hz": null, "gain_mode": "manual", "gain_db": 33.8, "agc": false, "ppm_correction": 0,
            "bias_tee": false, "direct_sampling": false, "offset_tuning": false, "iq_correction": false,
            "dc_offset_correction": false, "doppler_tracking": false},
  "recording": {"enabled": true, "engine": "rtl_sdr", "format": "cu8",
                "output_directory": "/data/satellite-captures/{job_name}", "minimum_free_space_gb": 10},
  "satdump": {"enabled": true, "operation": "decode_after_recording", "pipeline": "meteor_m2-x_lrpt",
              "input_type": "baseband", "baseband_format": "cu8", "sample_rate_hz": 2400000,
              "frequency_hz": 137900000, "output_directory": "/data/satellite-captures/decoded/{job_name}"},
  "postprocess": {"generate_waterfall": true, "generate_spectrum": true, "decode": true,
                  "retain_raw_iq": true, "checksum": true, "record_metadata": true}
}]}
```

The same pass on the RSP1B differs only in these blocks:

```json
"receiver": {"receiver_id": "RSP1B", "driver": "sdrplay", "serial": "240526D360", "exclusive_lock": true},
"radio": {"center_frequency_hz": 137500000, "target_frequency_hz": 137900000, "sample_rate_hz": 2000000,
          "bandwidth_hz": 1536000, "gain_mode": "manual", "gain_db": null,
          "gain_elements": {"IFGR": 50, "RFGR": 8}, "agc": false, "ppm_correction": 0, "bias_tee": false,
          "direct_sampling": false, "offset_tuning": false, "iq_correction": false,
          "dc_offset_correction": true, "doppler_tracking": false},
"recording": {"enabled": true, "engine": "soapysdr", "format": "cs16", "output_directory": "/data/satellite-captures/{job_name}"},
"satdump": {"enabled": true, "operation": "decode_after_recording", "pipeline": "meteor_m2-x_lrpt",
            "input_type": "baseband", "baseband_format": "cs16", "sample_rate_hz": 2000000,
            "frequency_hz": 137900000, "output_directory": "/data/satellite-captures/decoded/{job_name}"}
```

## Operations

- `aobs schedule jobs` lists the unified schedule. `aobs schedule validate job.json` does a dry run.
- `aobs schedule shadow` prints the shadow comparison. `aobs schedule plan` runs one planning pass.
- Broker API (bearer token): `POST /v1/schedule`, `GET /v1/capabilities`, `GET /v1/jobs`,
  `GET /v1/jobs/{id}`, `POST /v1/jobs/{id}/cancel`, `GET /v1/recordings`,
  `GET /v1/jobs/{id}/file?path=`, `GET /v1/notifications?after=`, `GET /v1/scheduler/status`.
- Raw IQ from automatic jobs is pruned after `auto_retention_hours`. Manual recordings are
  never pruned; the storage check counts bytes already committed to scheduled jobs.
