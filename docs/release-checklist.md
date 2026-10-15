# Release checklist: receiver inventory + persistent configuration

This covers the feature-branch work up to this document (`feature/observatory-updates`): database schema 1 → 3,
the receiver inventory (discovered → configured → verified), and versioned settings. Nothing here is deployed.
**Merging into `main` and deploying need the owner's approval.** Every step that touches the station is marked
**(owner)**.

Production facts this checklist relies on (checked 2026-10-15):

- `observatory-broker.service`: `User=tommy`, `WorkingDirectory=/home/tommy/auto-observe`, `ExecStart=.venv/bin/aobs broker`, no `AOBS_CONFIG` (so it uses the checkout's `config/observatory.toml`).
- The venv has an *editable* install, so the broker runs the checkout's `src/` directly: merging changes the code, and the broker uses it at its next restart. No `pip install` is needed: no new dependencies were added.
- Database: `/data/autonomous-observatory/db/observatory.sqlite3`, schema 1, about 53 MB.
- Independent recorders outside Starlab's control:
  - `record.py` (`observatory-record@OBS-R01`), which uses OBS-R01;
  - `meteor-auto.py`, which uses the RSP1B;
  - `dump1090` (ADS-B), which shares OBS-R05.

## 0. Before merging

- [ ] **(owner)** Review the branch: `git log main..feature/observatory-updates`, `docs/receivers.md`, this file.
- [ ] Full test suite passes in `~/starlab-dev` (`pytest -q`). Last run: 336 passed, 1 skipped.
- [ ] Migration rehearsed on a fresh read-only copy of the production database, on the day of the deploy:
  ```bash
  cd ~/starlab-dev && . .dev/env.sh
  python scripts/rehearse-migration.py --config ~/auto-observe/config/observatory.toml   # exit 0 = all checks pass
  ```
  It copies the live database through SQLite's online backup, opened read-only, and migrates the copy. Last run,
  2026-10-15:
  - schema 1 → 3;
  - 14,279 rows in 25 tables preserved;
  - the 4 TOML receivers imported, the 5 settings documents seeded;
  - **effective configuration identical to the file**;
  - a second start changed nothing.
- [ ] `~/auto-observe` working tree is clean apart from the known untracked files (`git status`).
- [ ] **(owner)** Approve the merge.

## 0b. Brief 3 (installer) checks for this station

Brief 3 changed how the configuration file is found. The new search order is `--config`, `$STARLAB_CONFIG`,
`$AOBS_CONFIG`, `/etc/starlab/starlab.toml`, `~/.config/starlab/starlab.toml`,
`/etc/autonomous-observatory/observatory.toml`, then the checkout's `config/observatory.toml`.

- [ ] Neither `/etc/starlab/starlab.toml` nor `~tommy/.config/starlab/starlab.toml` exists on the observatory.
      Either would be picked up **instead of** the checkout's `config/observatory.toml`. Checked absent
      2026-10-15. Never run `install.sh` on the observatory itself.
- [ ] The station keeps its integrations. Its TOML has `[legacy] station_config`, so the legacy integration
      stays on. It also sets `adsb_serial`, `broker.receivers` and `antenna_switch.receiver` explicitly, which
      matters because the new defaults for those three are empty. Rehearsal 2026-10-15: effective configuration
      identical, 9 settings documents seeded.
- [ ] The station is not sent into the first-run wizard: its setup document is seeded as completed, because it
      has TOML receivers and the legacy integration. `/` redirects to `/rf`, and Slack sign-in links land there too.
- [ ] Brief 3 has no schema change: the database is still schema 3.

## 0c. Brief 3a (SDR++ sessions and dev isolation)

- [ ] The schema is now **4** (migration 4 adds `rx_sessions`; additive only). The rehearsal script covers it.
      2026-10-15: 1 → 4, every row preserved, effective configuration identical.
- [ ] After deploying, only OBS-R02 is eligible for SDR++ sessions. The others are refused:
  - OBS-R01: its legacy recorder owns it;
  - RSP1B: an SDRplay device can't be isolated;
  - OBS-R05: it is behind the antenna switch.
- [ ] A **hand-started SDR++ server** (see the hazards notes) bypasses Starlab. Starlab sees it only as
      "receiver in use" and refuses sessions and tests on that receiver. Retire hand-started servers in favour of
      managed sessions.
- [ ] **Hardware check (owner, idle window, after deploy):**
  1. On OBS-R02, Test ▾ → SDR++ Server → Start, on the Tailscale address.
  2. Connect SDR++ from another computer (source *SDR++ Server*).
  3. Confirm the device list shows only OBS-R02, the waterfall shows real signals, and the page reports
     "SDR++ has /dev/bus/usb/… open".
  4. Stop, and confirm the receiver is free again: a Verify test starts.

  Result: ______
- [ ] bubblewrap is installed on the observatory (it is: 0.11.1). Without it no session is offered.
- [ ] Development isolation: development uses `scripts/dev start|stop|status`, which works only by PID file. The
      `.dev/` sandbox has its own config, database, token, broker port (18751) and session ports
      (15259–15299). `STARLAB_DEV=1` makes every command refuse production's config, database, token or ports.
      A broker lock prevents two brokers on one data folder.

## 0d. Brief 4 (hardware discovery, receiver hosts)

- [ ] The schema is now **5**. Migration 5 adds `inv_hosts` and rebuilds `inv_receivers` with `identity_key`
      and `identification`, copying the rows unchanged; it refuses if the row count differs. The rehearsal
      script covers it.
- [ ] The scheduler sees only receivers on this computer, so the four TOML receivers are unaffected. Their
      identity keys are `sn:<serial>`.
- [ ] **Identify now opens RTL-SDRs** (`rtl_test -t`, `rtl_eeprom`) when they are free and not booked. On this
      station, OBS-R01 belongs to its legacy recorder and OBS-R05 to dump1090, so Identify is refused while they
      run; that is expected. Do not identify the RSP1B while an independent recorder may be writing.
- [ ] **Hardware check (owner, idle window, after deploy):** Identify OBS-R02 and the Airspy, and confirm:
  - the tuner and gain steps (OBS-R02), and the firmware (Airspy), match the hardware;
  - nothing else was disturbed: other receivers keep recording, and dump1090 keeps decoding.

  Result: ______
- [ ] **Remote host (owner, optional):** add one remote Linux computer.
  - Its fingerprint must match `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` run on that host.
  - Discover devices, and Identify one free receiver there.
  - Do **not** register comet: its radio belongs to SatNOGS.

  Result: ______
- [ ] `ssh`, `ssh-keygen` and `ssh-keyscan` are installed on the observatory (they are: OpenSSH 10.2).

## 1. Choose the window

The broker restart must not overlap a recording. A broker restart never stops a capture that is already running
(captures run in their own process session and are adopted again), but do not rely on that.

- [ ] Nothing scheduled for the next 30 minutes on any receiver: `~/auto-observe/.venv/bin/aobs schedule jobs`.
- [ ] Neither independent recorder is in a pass: `record.py` for OBS-R01 and `meteor-auto.py` for the RSP1B.
      Check today's METEOR pass times and pick a gap of at least 30 minutes.
- [ ] At least 1 GB free next to the database for the pre-migration backup: `df -h /data`.

## 2. Back up and deploy

- [ ] Note the current state, for comparing afterwards:
  ```bash
  cd ~/auto-observe
  git rev-parse HEAD > ~/pre-release-commit.txt
  git tag pre-inventory-$(date +%Y%m%d) HEAD                  # the rollback point
  .venv/bin/aobs schedule jobs > ~/pre-release-jobs.txt
  curl -s -H "Authorization: Bearer $AOBS_TOKEN" http://<broker-address>:8751/v1/scheduler/status > ~/pre-release-status.json
  ```
- [ ] Take a manual copy as well; the old code has no `aobs db backup`, so use SQLite's online backup:
  ```bash
  sqlite3 /data/autonomous-observatory/db/observatory.sqlite3 ".backup /data/autonomous-observatory/db/backups/manual-pre-release-$(date +%Y%m%dT%H%M).sqlite3"
  ```
- [ ] **(owner)** Merge: `git -C ~/auto-observe merge --ff-only feature/observatory-updates` (fast-forward only).
- [ ] Check the new code sees the old database correctly, without changing it:
      `.venv/bin/aobs db status` should report schema 1, "will be migrated at the next start (backup first)".
- [ ] **(owner)** In the chosen window: `sudo systemctl restart observatory-broker`.

## 3. Verify the migration

- [ ] `journalctl -u observatory-broker -n 50` shows:
  - `migrating database schema 1 -> 3 (backup: …)`;
  - `database schema now 2` and `now 3`;
  - no tracebacks.
- [ ] `.venv/bin/aobs db status` shows schema 3, "up to date", and the `pre-migration-v1-v3` backup is listed.
- [ ] Record counts equal the pre-release counts:
  ```bash
  sqlite3 -readonly /data/autonomous-observatory/db/observatory.sqlite3 \
    "select (select count(*) from sched_jobs),(select count(*) from rf_layouts),(select count(*) from rf_tests),(select count(*) from artifacts)"
  ```
- [ ] `GET /v1/health` is OK. Compare `/v1/scheduler/status` modes with `~/pre-release-status.json`; they must be identical.

## 4. Existing receivers keep working

All four TOML receivers are imported as *configured · in service*. They keep their IDs, serials, modes and
automatic rules, and keep recording exactly as before. They do **not** show as *verified* until a Starlab
hardware test proves it.

- [ ] Receivers page: OBS-R01, OBS-R02, OBS-R05, RSP1B listed as "Configured · not verified", automatic recording
      as before (R01 and R02 on), serials unchanged.
- [ ] Upcoming jobs after the next planning cycle (≤ 5 min) match `~/pre-release-jobs.txt`: same receivers and
      same automatic passes, none dropped or added.
- [ ] Settings page: values match `observatory.toml`, with no "observatory.toml says something else" warnings.
- [ ] The RF editor loads the same layout version as before the restart.
- [ ] The calendar keeps updating (next sync within a few minutes), and Slack still gets job notifications.
- [ ] The cutover scripts and `aobs schedule set-mode` still work as before. They edit `observatory.toml`; the
      broker re-syncs those receivers from the file at its next restart, the same timing as today.

## 5. Device ownership

What the new code does and does not touch:

| Action | Opens a device? | Safeguards |
|---|---|---|
| Receivers page and discovery | No | Reads sysfs and /proc only; shows "in use" from claimed USB interfaces and open handles |
| Identify (Airspy and other SoapySDR) | Yes, for a few seconds | Refused if any process holds it, if its USB interface is claimed, or if Starlab has a booking within 3 minutes. Checked twice, the second time right before opening. One probe at a time |
| Verify hardware and Test | Yes, for the test | Normal exclusive reservation. Refused when:<br>• the device is not on USB<br>• it is held or claimed by another program<br>• it is leased<br>• a booked pass (including imported legacy windows) starts within 2 minutes of the test's end<br>• the RSP1B legacy recorder is writing |
| Automatic recording | Only for receivers already configured for it in `observatory.toml` | New receivers never record automatically. Recording jobs need a *verified* receiver, or one imported while already in service |

- [ ] **(owner)** Do not Identify or Verify OBS-R01 or the RSP1B while their independent recorders own them.
      The checks refuse when a recording is visibly in progress. A test that ends just before a pass the
      independent recorder starts on its own is still possible, because Starlab only knows about those passes
      through the imported legacy windows. Prefer verifying them after their cutover to broker mode, or in a
      long gap between passes.
- [ ] OBS-R05 and ADS-B sharing: unchanged (lease path, dump1090 stop/start).

## 6. Verify against actual hardware

The owner runs these after the deploy, in an idle window, from the Receivers page. Write the results here.

- [ ] **Airspy R2:**
  1. Receivers → Discovered hardware shows "Airspy R2 or Airspy Mini", its serial, port 5-4, "not in use".
  2. Identify, then check the capabilities match the R2: 24–1800 MHz, 2.5 and 10 MS/s, LNA/MIX/VGA 0–15, CS16/CF32.
     A Mini reports different rates.
  3. Add as receiver, with "Use the diagram entry EXT-1" ticked so its parts are kept.
  4. Verify hardware. Expected: about 10 s capture at 2.5 MS/s and "Verified", with samples checked in three windows.
  5. Automatic recording stays off.

  Result: ______
- [ ] **YARD Stick One:** listed as "not an IQ receiver"; no Identify/Add buttons; can only be documented.
- [ ] **OBS-R02:** Verify hardware in a gap. Expected: "Verified" (RTL-SDR, cu8, IQ checks plus the existing IQ-health
      classifier). Result: ______
- [ ] **OBS-R05:** Verify hardware in a gap with ADS-B idle; uses the switch-port lease path. Result: ______
- [ ] OBS-R01 and RSP1B: see section 5; verify only when appropriate. Result: ______
- [ ] A failed verification leaves the receiver "Configured · last test failed" with reasons, and does not change its
      automatic recording.

## 7. Rollback

The old code refuses to start on schema 3 ("Database schema 3 is newer than this code"). Rolling back the code
therefore also means restoring the database, which loses whatever was written after the migration:
- new jobs, recordings index rows, tests and settings;
- recording files themselves stay on disk.

1. **(owner)** `sudo systemctl stop observatory-broker`, in an idle window.
2. Restore the automatic pre-migration backup **while the new code is still checked out**. Only the new code has `aobs db restore`:
   ```bash
   cd ~/auto-observe && .venv/bin/aobs db list
   .venv/bin/aobs db restore observatory-pre-migration-v1-v3-<time>.sqlite3 --yes   # takes a safety copy first
   ```
   If that is not possible: `cp` the backup over the database file and delete any `-wal` and `-shm` files beside it.
3. `git -C ~/auto-observe switch --detach pre-inventory-<date>`, or `git reset --hard` to it on `main` if the merge
   should be undone.
4. **(owner)** `sudo systemctl start observatory-broker`; check `/v1/health` and `aobs schedule jobs`.
5. Recordings made between the migration and the rollback are still on disk. Re-index them with
   `aobs schedule import --apply` if they are needed in the catalogue.

## 8. Not in this release

- Recording, testing or SDR++ sessions on remote receiver hosts (they can be discovered, identified and configured).
- Wideband scans, gain sweeps and satellite tests for SoapySDR receivers (they get the quick test and Verify).
- Automatic recording for SoapySDR receivers.
- Satellite selections and automatic rules editable on the web. They are shown read-only and stay in `observatory.toml` until the scheduling-rules phase.
- Explicit `AOBS_CONFIG` in the unit and removal of the repo-config fallback (Phase 2a).

## Sign-off

| Step | By | Date | Notes |
|---|---|---|---|
| Merge approved | | | |
| Deployed (restart window) | | | |
| Migration verified (§3) | | | |
| Existing receivers verified (§4) | | | |
| Hardware verification (§6) | | | |
