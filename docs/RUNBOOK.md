# Operator runbook

**Hosts:**
- observatory: the broker, recorders and data. The broker API is on 100.64.49.37:8751 (tailnet only).
- monkeybrain: the Slack bot (`systemctl --user … obsagent`).

**Canonical state:**
- Database: `/data/autonomous-observatory/db/observatory.sqlite3`. The `sched_jobs`, `proc_attempts`
  and `artifacts` tables are authoritative for reservations, processing and files.
- Recordings made by the broker: `/data/observatory/jobs/<job>` (manual) and
  `/data/observatory/auto/<receiver>` (automatic).
- Older recordings stay where they are (`/data/observatory-recordings`, `/data/sdrplay/meteor`)
  and are indexed in place.

## Schedule a recording
1. Run `/radio-capabilities` and give the JSON to ChatGPT, or follow `docs/SCHEDULER.md`.
2. Run `/radio-schedule dry-run {…}`, then `/radio-schedule {…}`. It replies **SCHEDULED** with a
   job id only if the job was stored.
3. The job appears on the Satellite Observatory calendar within a minute. You get a DM thread when it
   starts and when it finishes.

## Find, decode, analyze, retrieve
1. `/radio-recordings latest` (or `today`, `satellite=METEOR-M2-4`, `decoded=false`, …)
2. `/radio-decode recording=<id>`: queued at low priority; you are told when it finishes
3. `/radio-analyze recording=<id>` or `/radio-waterfall recording=<id> frequency=137.9 bandwidth=250000`
4. `/radio-results <id> type=images` uploads images. `link=<artifact_id>` gives an expiring tailnet
   link for raw IQ.

To try other settings, run `/radio-decode` again with new parameters. Earlier attempts and images
are kept.

## Compare receivers
`/radio-analyze compare=<rec1>,<rec2>` reports each one's dB over its own noise floor, clipping and
overload findings, and warnings about what is not comparable. Prefer recordings of the same pass
(for example R01 and RSP1B both record every METEOR pass).

## Inspect receivers
`/radio-status` shows everything. `/radio-status receiver=OBS-R01` shows:
- the processes holding the dongle;
- any legacy service controlling it;
- its lease, current and next job, and settings;
- its last good recording and last error.

## Troubleshoot a failed job
1. `/radio-jobs failed`, then `/radio-jobs <job_id>`: the history and reason.
2. `/radio-results <id> type=logs` shows the capture log, SatDump log and worker log.
3. `/radio-health` gives FAILs and what to do about them.

| Typical reason | Meaning / fix |
|---|---|
| "receiver unavailable" | USB disconnected |
| "held by sdrpp_server[…]" | SDR++ or another program has the dongle |
| "not enough storage" | free space under /data |
| decode "no images" | no frame sync; check the spectrum (`/radio-analyze`) for a visible signal at the target |

## A receiver alert says POSSIBLE RECEIVER FAILURE

The recording finished, but its IQ samples barely move (see docs/RTL_SDR.md). Antenna or LNA faults lower the level but leave normal noise, so this points at the dongle or its USB link.
1. Don't touch it during a pass. `/radio-status <receiver>` shows the next job.
2. When it's idle, schedule two ~30 s recordings at gains ≥10 dB apart (for example 8.7 and 38.6 dB). If both stay flat at the same level, the alert's "Gain response" line says so.
3. Physically reconnect the dongle's USB cable. That is how OBS-R02 recovered on 2026-10-13. Then repeat step 2.
4. If it's still flat, try another port or cable, then replace the dongle.
5. To check any file by hand: `.venv/bin/aobs schedule iq-health --paths <file.cu8>`

## Restart services safely
- **Broker** (dev process until migration; `observatory-broker.service` after):
  1. Run `/radio-status` and confirm nothing is RECORDING or about to start.
  2. Restart it.
  Captures run in their own process session, so a restart never cuts a recording. The new broker
  adopts the capture, and the R05 lease is kept while it runs. Processing attempts that were running
  are adopted if alive; otherwise they are marked failed, and post-recording ones are re-queued.
- **Bot:** `systemctl --user restart obsagent` on monkeybrain, only when idle (no `tool`/`usage`
  lines in the last 90 s of `journalctl --user -u obsagent`).

## Recover from a broker failure
1. Check `aobs schedule jobs` and `/radio-health` → "stale running jobs" and "unexecuted past jobs".
2. Start the broker. On startup it:
   - restores leases from the previous process, except those of recordings still running;
   - marks reservations that passed while it was down as **missed** (it never starts a late
     recording that could collide with the next one);
   - re-queues post-recording processing.
3. If Google Calendar sync failed meanwhile, the next sync repairs every event from the database. The
   calendar is a mirror and never a lock.

## Backup
- `sqlite3 /data/autonomous-observatory/db/observatory.sqlite3 ".backup /data/autonomous-observatory/db/backup-$(date +%F).sqlite3"`
- `config/observatory.toml` is in the repository.
- Secrets live in `/data/autonomous-observatory/secrets` and `~/.config/observatory` on both hosts.

## Receiver migration
See `docs/SCHEDULER.md` → "Migrating the automatic recorders".

- **Before:** `aobs schedule shadow` must show `agrees: true`.
- **Cut over:** `sudo deploy/sudo/migrate-recorders.sh`.
- **Roll back:** `sudo deploy/sudo/rollback-recorders.sh`.
