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

Stored statuses: `scheduled → preparing → recording → recorded → processing → completed`, or `failed`,
`cancelled`, `preempted`, `skipped`, `missed`, `partial` (a running recording terminated on
confirmed request; the IQ is kept), `predicted` (a rule's pass while Scheduling is manual) or `shadow`.
Legacy windows end as `recorded`/`failed`/`missed` from the old recorder's own metadata. Every
transition is in `sched_job_events` with its time, reason and actor. Every recording gets a JSON
sidecar (`<raw>.json`) with the full request, resolved settings, capture result, checksum, plots and
SatDump outcome.

The web page and the `/v1/scheduler/*` API also show each job's **lifecycle state**
(`store.LIFECYCLE`); the Slack bot keeps the detailed statuses:

| Lifecycle | Stored status | Meaning |
|---|---|---|
| PREDICTED | `predicted`, `shadow` | a rule would record it; it holds no receiver and is never executed |
| SCHEDULED | `scheduled` | booked; the receiver is reserved |
| RESERVED | `preparing` | the receiver is being taken (pre-roll) |
| RECORDING | `recording` | capturing |
| PROCESSING | `recorded`, `processing` | the file is written and validated; analysis/decoding runs |
| COMPLETED | `completed` (and a legacy window's `recorded`) | the output was validated |
| SKIPPED | `skipped`, `preempted` | not recorded, with the reason (busy, unavailable, no longer predicted, rule off) |
| CANCELLED | `cancelled`, `partial` | a person cancelled it (or stopped it while recording) |
| FAILED | `failed`, `missed` | the recording or its output failed, or the broker was not running |

**Scheduled is not recorded.** A capture that reports success is checked before the job moves on
(`runner.validate_output`): the file exists, is the size the recorder reported, holds at least 98% of
the scheduled samples and a whole number of samples, and (RTL-SDR) is not flat or a capture failure.
Otherwise the job is FAILED with the reason, and the file is kept for inspection. The result is in
`result.capture.validation`.

## Automatic rules (brief 6)

Rules replace `[scheduler.receivers.*.auto]` for receivers that are not recorded by a legacy service.
Each rule is stored in `sched_rules` (schema 6) with a revision, and every save in `sched_rule_versions`.

| Field | Meaning |
|---|---|
| name, enabled | |
| satellites | NORAD numbers |
| min_max_elevation_deg | passes peaking lower are ignored |
| receiver_id, backup_receivers | preferred receiver, then backups in order |
| antenna | switch-port antenna, for a receiver fed by ANTSW-01 |
| frequency_hz | empty: each satellite's catalogued downlink |
| sample_rate_hz, gain_profile, format | empty: the receiver's defaults. Gain profiles are the receiver's profiles in the RF editor |
| pre_aos_s, post_los_s | recording margins around AOS/LOS (the reservation adds its own setup/teardown) |
| decode, pipeline | SatDump after recording; empty pipeline: the satellite's default |
| raw_iq_policy, raw_iq_hours | `default` (Storage → automatic retention), `keep`, `after_decode`, `hours` |
| priority | 1–100; a higher-priority rule may take a receiver from a lower one's job that has not started |
| output_root | storage destination (under the allowed output roots) |

**Settings → Scheduling** sets the mode:
- *manual*: enabled rules only produce PREDICTED jobs.
- *automatic* or *hybrid*: enabled rules book their passes, and people can still book, change or skip
  anything. The two behave the same; hybrid is the name to use.

A fresh installation and this station start in manual.

### Reconciliation (`rules.RulePlanner.reconcile`)

It runs every planning cycle (`plan_interval_seconds`), right after a rule or a job is changed, and on
**Re-plan now**. Passes are predicted with the built-in Skyfield predictor at the observatory location,
`horizon_hours` (default 48) ahead. Each pass is handled in turn, the most important first (priority,
then peak elevation):

- **Matching.** A pass is matched to its existing job by rule + satellite + overlapping AOS–LOS. When
  new elements move a pass by a few seconds, that job is updated in place; no second job is added.
- **Never touched:**
  - jobs that start within 5 minutes;
  - jobs that are preparing, recording or later;
  - jobs cancelled by a person (`cancelled` is only ever set by people);
  - jobs **changed by hand**: the `override` column holds who, when, what and the previous values. If the
    prediction later moves or the pass disappears, the job keeps the person's settings and gets a note.
- **Duplicates.** A satellite pass is booked once. If any other job already records it (another rule,
  a manual booking, a legacy window), the rule does not add one. Booking it again by hand needs
  "Record it again".
- **Receivers.** The preferred receiver comes first, then the backups. A receiver is passed over when:
  - another schedule owns it: a legacy or shadow receiver, or a TOML `auto` block. The existing
    automatic recording is never doubled;
  - it is not verified;
  - it is not connected;
  - it cannot use the settings (the job is validated like any booking);
  - it is busy;
  - its storage is short.
- **Waiting jobs move to the best receiver.** Jobs that have not started move back to the preferred
  receiver when it is free again. A pass no receiver can take is kept as a SKIPPED job that names each
  receiver's reason, and it is booked by a later cycle if a receiver frees up.
- **Manual jobs still win.** A rule job a manual job preempts is retried on the rule's backups.
- **Retired jobs are skipped, never deleted.** This applies to passes no longer predicted and to jobs of
  disabled or deleted rules. Re-enabling a rule books them again.
- **Raw IQ retention** of finished rule recordings follows the rule (`RulePlanner.prune`).
  `planner.prune_auto` no longer touches rule jobs.

### Changing, skipping and handing back (hybrid)

| Action | API | Effect |
|---|---|---|
| Change | `POST /v1/scheduler/jobs/{id}/change` `{changes, dry_run}` | start, end, receiver, antenna, frequencies, rate, bandwidth, gain profile or values, format, decoding off, note. Validated and conflict-checked like a booking; may preempt automatic jobs like a manual one. A PREDICTED job becomes SCHEDULED. |
| Skip | `POST /v1/scheduler/jobs/{id}/cancel` | CANCELLED; the rule never books that pass again |
| Hand back | `POST /v1/scheduler/jobs/{id}/revert` | clears the override; the next cycle sets the job from its rule again |

### Orbital elements

Settings → Satellites:

| Setting | Values |
|---|---|
| `elements_source` | `celestrak_omm` (OMM JSON, falling back to TLE), `celestrak_tle`, or `custom` (`custom_url` with `{norad}`, `custom_format` tle/omm) |
| `refresh_online` | downloading on or off |
| `refresh_hours` | automatic refresh period while downloading is on, default 12 |
| `stale_days` | warning threshold, default 14 |

- **Cache.** Downloads go to `<data>/tle/`: `starlab.tle`, `starlab.omm.json`, and `fetched.json`
  (when and from where each satellite's elements came).
- **Fallback.** A failed download keeps the cached elements. Without any, the shipped sample elements are
  used and labelled as such.
- **Warnings** appear on the Scheduler page and in each rule's planning result:
  - stale elements;
  - a satellite with no elements at all (its passes are not predicted, and its existing jobs are left
    alone);
  - a missing location.

### The Scheduler page

**Scheduler** in the web page's header opens five tabs:

- **Calendar:** a week per page, day strips, filters by satellite, receiver and state.
- **Upcoming passes:** 72 hours. **Schedule recording** opens the form pre-filled from the pass.
- **Automatic rules:** an editor with Save/Cancel, an unsaved marker, and **Preview passes**, which shows
  which receiver would take each pass and why.
- **New recording:** any frequency at any time, in local time.
- **Recording history:** 30 days, with validation results.

Times are shown in the observatory's display time zone and stored in UTC. The form sends local wall-clock
times converted to UTC in the browser, and the server also accepts local times plus `schedule.timezone`.

API (signed-in web session or bearer token):

| Method | Route |
|---|---|
| GET | `/v1/scheduler/overview` |
| GET, POST | `/v1/scheduler/rules` |
| PUT, DELETE | `/v1/scheduler/rules/{id}` (with revision) |
| POST | `/v1/scheduler/rules/validate` |
| POST | `/v1/scheduler/rules/preview` |
| GET | `/v1/scheduler/rules/{id}/history` |
| POST | `/v1/scheduler/reconcile` |
| GET | `/v1/scheduler/calendar?start&end&satellite&receiver&state` |
| GET | `/v1/scheduler/jobs/{id}` |
| GET | `/v1/scheduler/form?norad&aos&receiver_id` |
| POST | `/v1/scheduler/manual` `{form, dry_run}` |

### Migrating this station

- **On first start of schema 6,** each receiver's TOML `auto` block (OBS-R01, RSP1B) is imported once as a
  **disabled** rule that documents it. Rules cannot use those receivers while the TOML block is enabled
  or the receiver is in `legacy`/`shadow` mode, so deploying this changes nothing.
- **To move a receiver to rules:** migrate it to `broker` mode, then turn off its TOML `auto`. Then enable
  its rule and set Settings → Scheduling to hybrid. Check the PREDICTED jobs first, with Scheduling still
  on manual.

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
