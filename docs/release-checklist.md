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

## 0e. Brief 5 (inventory and signal chains)

- [ ] No schema change (still **5**). Inventory records and shared paths are new fields in the versioned
      `rf_layouts` documents. Older versions load with defaults: polarization, mount, filter type, DC pass and
      supply are all "not specified".
- [ ] The rehearsal script also validates the station's current layout with the new code, and prints the derived
      receiver fields. On 2026-10-15 layout v7 validated, and the four receivers' antenna, signal path, amplifier
      and filters were identical to what the running code computes from the same copy.
- [ ] Expect a new warning on the live layout:
      `OBS-R01: automatic recordings (2 satellites): 137.9 MHz is outside Shortwave Loop's range (2.2–30 MHz)`.
      v7 puts OBS-R01 on the shortwave loop while it still records NOAA automatically. Either the layout or the
      automatic settings should change; the release itself changes neither.
- [ ] Job checks now add warnings (never errors) when a job's frequency is outside its antenna's range or is
      blocked by a filter on the path. Automatic planning is unaffected: the planner stores its jobs directly.
- [ ] The automatic-pass radio settings moved into `hardware.auto_radio()` without changing them. The planner
      tests cover this.
- [ ] **Browser check (owner, after deploy):** open /rf, open each part and receiver once, and Cancel. Check that
      nothing is marked unsaved and nothing reads wrongly.

  Result: ______

## 0f. Brief 6 (rules, hybrid scheduling, job states)

- [ ] **Schema 6.** It adds `sched_rules` and `sched_rule_versions`, plus nullable `sched_jobs.rule_id` and
      `sched_jobs.override` columns. Existing jobs are untouched (both columns NULL).
- [ ] **Rehearsal, 2026-10-15** (`scripts/rehearse-migration.py` on a copy):
  - schema 1→6, all 14,405 rows kept;
  - OBS-R01's and RSP1B's TOML `auto` blocks were imported as **2 disabled rules**, and a second start
    imports none;
  - one rule-planning cycle changed **none** of the 367 existing jobs and created no rule jobs;
  - Scheduling stays **manual**.
- [ ] **Nothing records differently after deploy.** Rules never use a receiver that is in legacy/shadow mode
      or has a TOML `auto` block. The existing planner and the legacy recorders keep doing exactly what they do
      today.
- [ ] **Behaviour change: completion now requires a validated output.** A capture whose file is missing,
      short, cut mid-sample, or flat (RTL IQ health `suspicious_flat` / `capture_failure`) is now **FAILED**,
      not completed. The file is kept, and the IQ-health alert and Slack message still go out. A stuck R02
      dongle like the one on 2026-10-13 would now show as failed recordings.
- [ ] Web bookings now say "scheduled from the web page" instead of "scheduled from Slack".
- [ ] The header button **Passes** is now **Scheduler**. The old Passes view is still reachable from the code
      but no longer has a button.
- [ ] **Browser check (owner, after deploy):** open Scheduler and look at the Calendar and Upcoming passes.
      Under Automatic rules, open each imported rule: each should say it cannot use its receiver yet. Press
      Cancel. Nothing should be booked.

  Result: ______

## 0g. Brief 7 (reservations, remote recording, start checks, diagnostics)

- [ ] **Schema 7.** It adds `sched_reservations`, plus nullable `sched_jobs.execution` and `inv_hosts.exec_config`
      columns. Existing jobs and hosts are untouched (NULL).
- [ ] **Rehearsal, 2026-10-16** (`scripts/rehearse-migration.py` on a copy):
  - schema 1→7, all 14,463 rows kept;
  - no reservations after the upgrade;
  - no remote receivers;
  - the 8 past successful broker recordings would all still pass the new start checks;
  - nothing booked to execute at the time of the copy.
- [ ] **Start order changed.** Before taking any lock, the runner now checks, in order:
  1. frequency, sample rate, gain;
  2. the device is connected;
  3. there is room for the file (the estimate plus 256 MB);
  4. no other program holds the device (up to 20 s, as before).

  It then takes the Starlab reservation and only then the coordinator lease or ANTSW-01 lease. A failed check
  leaves nothing held.
- [ ] **Failure messages changed wording.** For example: "OBS-R02 could not start because another process has
      claimed its USB interface. (…)". Every check and error is on the job page under **Execution**, with
      timestamps and the recorder log.
- [ ] **Overruns are now stopped.** A recorder still running 90 s after its scheduled end gets SIGTERM, then
      SIGKILL 30 s later, and the job is FAILED ("did not stop"). Before, the runner waited indefinitely.
- [ ] **Backups at start time.** A *rule* job whose receiver fails a check moves to the rule's first backup
      that passes. The backup must be on the same antenna, or one the signal-path layout confirms covers the
      frequency. The move is an event plus a notification. Manual jobs and jobs changed by hand never move.
      No rules are enabled on this station, so this cannot trigger after deploy.
- [ ] **Remote receivers** (inventory host ≠ local) are now visible to the scheduler. They still need a hardware
      test (the quick test runs over SSH) before they can be booked. None exist on this station.
- [ ] **Terminate (`/radio-cancel`, confirmed)** now goes through the runner. Locally, it signals the process
      group only after `/proc/<pid>/cmdline` proves it is that job's capture process.
- [ ] **Development guard.** The live recorder refuses any non-simulated receiver under pytest,
      `STARLAB_DEV=1` or `STARLAB_NO_HARDWARE=1`. Production sets none of these. Check with
      `systemctl show observatory-broker -p Environment`.
- [ ] **Remote host check (owner, optional, before relying on a remote receiver):**
  1. On the remote host, install Python 3 with numpy, plus rtl_sdr or SoapySDR.
  2. Settings → Receiver hosts → set *Python on the host* and *Work folder*.
  3. Run the receiver's quick test.
  4. Book a short manual recording. Confirm the job's Execution card says "copied from <host> … verified by
     size and SHA-256", and that the folder under `<work>/recordings/` on the host is gone afterwards.

  Result: ______

## 0h. Brief 8 (recording archive, decoder profiles, retention, transfer)

- [ ] **Schema 8.** It adds `decoder_profiles` and `decoder_profile_versions`, plus nullable
      `sched_jobs.archive`, `artifacts.host` and `artifacts.deleted_reason`. Existing rows are untouched.
- [ ] **Rehearsal, 2026-10-16** (`scripts/rehearse-migration.py` on a copy):
  - schema 1→8, all 14,510 rows kept;
  - the effective configuration equals the file, apart from six new Storage settings at their defaults
    (`retention_enabled=true`, `auto_raw_policy=hours`, `manual_raw_policy=keep`, `manual_raw_hours=720`,
    `test_raw_policy=keep`, `test_raw_hours=720`);
  - one decoder profile created (METEOR-M2 LRPT), none on a second start;
  - 60 recordings, highest outcome reached: 1 image, 9 signal, 49 recorded, 1 failed;
  - **nothing due for deletion**.
- [ ] **Deletion behaviour is unchanged by default.**
  - Automatic recordings: 48 h, as before.
  - Rule recordings: their rule's policy, as before.
  - Manual recordings and tests: kept, as before.
  - *retain raw IQ: no*: deleted after an image, as before; it now waits until no attempt is still running.

  New: the Keep flag, and the master switch. To turn all automatic deletion off before deploying, set Settings →
  Storage → *Delete raw IQ by the policies below* = off.
- [ ] **Decoding without a pipeline now uses decoder profiles.** The built-in profile reproduces the old METEOR
      default, including the ±6 kHz fine search. `/radio-decode` with no pipeline behaves as before for METEOR.
- [ ] **Post-recording decode that cannot start.** The job now completes with "decode not started: …". Before, it
      read "processing could not be queued", even though the analysis ran.
- [ ] **Recordings page.** After the broker restart, the **Recordings** button opens the library. Check:
  1. the 2026-10-12 RSP1B METEOR pass shows *image*;
  2. a recording with no decode shows *frames —*;
  3. Storage & retention shows *Due now: 0*.

  Result: ______
- [ ] **Receiver hosts.** None exist on this station. *Keep on the host* is opt-in per host.

## 0i. Brief 9 (optional Slack integration)

- [ ] **Nothing changes on deploy.** The new "slack" settings document is created disabled. No Slack thread starts,
      and `/slack/commands` answers 404. Check that Settings → Slack setup says "off".
- [ ] **The observatory bot is unaffected.** It still reads `/v1/notifications` with its own tokens. Do not enable
      this integration in #star-lab unless the bot is retired there; otherwise both would post.
- [ ] **Socket Mode needs `slack_sdk`** in the broker's environment: `.venv/bin/pip install 'slack_sdk>=3.33'` in the station's checkout (fresh installs: `install.sh --with-slack`). It is only
      needed if you enable Socket Mode; notifications and HTTP mode do not need it.
- [ ] **Optional live check (owner):**
  1. Create a test app from the generated manifest in a test channel.
  2. Paste the tokens, Test connection, Send a test message, Apply.
  3. Run `/starlab status`.

  Result: ______

## 0j. Opening Starlab from other computers (installer access, Tailscale, first-run links)

- [ ] No schema change.
- [ ] **New client check on the station's broker.** `[broker] allowed_clients` defaults to `["private"]`: loopback,
      RFC 1918, link-local, IPv6 ULA and Tailscale `100.64.0.0/10`. The station listens on its Tailscale address,
      and its clients are the owner's browsers over Tailscale and monkeybrain (`100.100.105.88`), so all are still
      answered. Anything from a public address is now refused with 403.
- [ ] Sign-in links: unchanged after setup (15 minutes). The station's setup document is completed, so 24-hour
      first-run links are never issued or accepted there.
- [ ] `starlab web-link` now prints one link per reachable address. The bot's `/v1/rf/link` is unchanged.

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
  - `migrating database schema 1 -> 7 (backup: …)`;
  - `database schema now 2` … `now 7`;
  - no tracebacks.
- [ ] `.venv/bin/aobs db status` shows schema 7, "up to date", and the `pre-migration-v1-v7` backup is listed.
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

- SDR++ sessions, wideband scans, gain sweeps and satellite tests on remote receiver hosts (remote receivers record
  and run the quick test over SSH).
- Receiver agents (the backend exists only as a placeholder that refuses clearly).
- Wideband scans, gain sweeps and satellite tests for SoapySDR receivers (they get the quick test and Verify).
- Automatic recording for SoapySDR receivers.
- Gain profiles used by signal-path tests (rules and the recording form use them; tests still state their own gain).
- Rotator control (rotators are inventory records only).
- Moving OBS-R01 and RSP1B from their TOML automatic settings to rules. That needs their cutover to broker mode
  (`migrate-recorders.sh`) first, then turning off the TOML `auto` block and enabling the imported rule.
- Explicit `AOBS_CONFIG` in the unit and removal of the repo-config fallback (Phase 2a).

## Sign-off

| Step | By | Date | Notes |
|---|---|---|---|
| Merge approved | | | |
| Deployed (restart window) | | | |
| Migration verified (§3) | | | |
| Existing receivers verified (§4) | | | |
| Hardware verification (§6) | | | |
