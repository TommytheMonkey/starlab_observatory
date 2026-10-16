# Release readiness — Starlab Observatory (2026-10-16)

Assessment of the `feature/observatory-updates` branch after briefs 1–10. It states what was tested and how, and
what was not tested.

**Verdict: ready for a public beta (recommended version `0.9.0b1`). Not production-ready (1.0).** Installation,
first-run setup, scheduling, the archive and the optional Slack integration all pass automated tests and
clean-container installs. The branch's recording code has not yet recorded from real hardware: development is
isolated from this station's receivers by design. Raspberry Pi / ARM64, a real remote receiver host and a real
Slack workspace are also untested.

## How it was tested

| What | How | Result |
|---|---|---|
| Unit and integration tests | `pytest`: 493 tests. Fakes stand in for receivers, SatDump, SSH (plus one real local sshd), Slack, the calendar and the antenna switch. | **492 passed, 1 skipped** (a station-only test) |
| Fresh install, Ubuntu 24.04 x86_64 | `tests/install/container-test.sh ubuntu:24.04`: a clean container with no USB devices; install, first-run wizard over HTTP, simulated receiver test, booking, archive, Slack defaults, rerun, upgrade, uninstall and reinstall | **38/38 end-to-end checks, all install stages passed** |
| Fresh install, Debian 13 (trixie) x86_64 | The same script with `debian:trixie` (the base of Raspberry Pi OS 13) | **38/38, all stages passed** |
| Upgrade of a real database | `scripts/rehearse-migration.py` on a read-only copy of this station's database | Schema 1→8; all 14,510 rows kept; configuration unchanged; no rule books anything; nothing due for deletion |
| Web UI | Headless Chrome against an isolated development broker, with simulated recordings decoded by the real SatDump | Recordings, recording detail, decoder profiles, storage and Slack setup render and work |
| Hardware isolation | Tests and development refuse to open real receivers (`STARLAB_NO_HARDWARE`, `STARLAB_DEV`) | Enforced by a test |

## The 20 scenarios

| # | Scenario | Status | Evidence |
|---|---|---|---|
| 1 | Fresh installation on Ubuntu | ✅ Tested | Container test, Ubuntu 24.04 |
| 2 | Fresh installation on Raspberry Pi OS | ⚠️ Partly | Debian 13 x86_64 container only. **Not tested on a Raspberry Pi or on ARM64.** |
| 3 | Installation without SDR hardware | ✅ Tested | Both containers have no USB devices; simulation mode, end to end |
| 4 | Installation with one RTL-SDR | ⚠️ Partly | Discovery, identification, validation, IQ health and the capture command are tested with fakes. The capture process has recorded real RTL-SDR passes daily on this station (main branch). **The branch was not run against a real receiver.** |
| 5 | Installation with multiple RTL-SDRs | ⚠️ Partly | Tests: duplicate and missing serials, USB-path identity, per-device reservations. Not on real hardware. |
| 6 | Installation with Airspy | ⚠️ Partly | Discovery and "identify before adding" are tested with fakes. This station's Airspy is seen passively over USB. **No Airspy recording has been made.** |
| 7 | Remote receiver discovery over SSH | ⚠️ Partly | Tested against a real OpenSSH server on 127.0.0.1: key-only login, host-key pinning, discovery, recording, dropped connection, verified copy, keep-on-host, transfer. **No real remote host (e.g. a Pi) yet.** |
| 8 | Receiver disconnection and reconnection | ✅ Tested (fakes) | Presence tests, start checks ("disappeared" is reported before anything is held), Slack offline/back alerts |
| 9 | Automatic satellite scheduling | ✅ Tested | Rules, hybrid mode, backups, re-plan without duplicates; rehearsed on the live database copy |
| 10 | Manual satellite scheduling | ✅ Tested | Booking form and API, the container booking a predicted pass |
| 11 | Overlapping recording jobs | ✅ Tested | Conflict rejection, all-or-nothing, concurrent booking race, preemption rules |
| 12 | Receiver busy errors | ✅ Tested | Claimed USB interface, another Starlab job, SDR++ sessions; plain-language messages |
| 13 | Failed recording recovery | ✅ Tested | Recorder crash, partial and cut files, disk full, overrun stop/kill, failed validation keeps the file |
| 14 | Application restart during scheduling | ✅ Tested | A restarted broker adopts a running recording and its reservation; processing attempts resume or re-queue |
| 15 | Configuration backup and restore | ✅ Tested | Versioned settings, export/import (no credentials), database backup and restore commands |
| 16 | Database migration | ✅ Tested | Every step 1→8 tested; automatic pre-migration backup; refuses a newer schema; live-copy rehearsal |
| 17 | Slack integration disabled | ✅ Tested | Default off; nothing runs; endpoint 404; the observatory works normally (tests and containers) |
| 18 | Slack integration enabled | ⚠️ Mocked | Manifest, credentials, signatures, commands, notifications and Socket Mode against a fake Slack. **Never connected to a real workspace.** |
| 19 | Recording and decoding | ⚠️ Partly | End to end with a fake SatDump, plus the real SatDump on simulated IQ (no frames, correctly reported). A real METEOR image was decoded on this station from the RSP1B (2026-10-12), with the same decoder code. Separate outcomes are tested. |
| 20 | Storage cleanup and retention | ✅ Tested | Every policy, keep flag, master switch, imported/legacy never deleted, out-of-root refused, remote deletion; the live copy shows nothing due |

## Release requirements

| Requirement | Status |
|---|---|
| No hardcoded developer-specific paths | ✅ In the application. Paths come from the installer's config (data folder); the storage status no longer assumes `/data` (fixed). Station-only paths remain only in the legacy-recorder integration, which runs only when `[legacy] enabled`. |
| No hardcoded geographic coordinates | ✅ None in code, docs or tests. The location is entered in the setup wizard. |
| No hardcoded receiver serial numbers | ✅ In the application. The broker's lease and the switch push no longer default to this station's receiver (fixed). Station serials remain in comments and in `config/observatory.toml` (this station's file). |
| No plaintext credentials in the repository | ✅ Tree and history scanned; the only token-like string is a deliberate fake in the tests. |
| No required Slack integration | ✅ Off by default; its package is an optional extra. |
| No required Tailscale | ✅ Default bind 127.0.0.1; nothing requires Tailscale. |
| No dependency on our homelab | ✅ For the application. ⚠️ The repository still carries this station's deployment files (see Remaining issues). |
| No automatic recording until explicitly enabled | ✅ Fresh install: scheduling mode manual, no rules; the wizard confirms "automatic recording off on every receiver". |
| Persistent configuration across restarts | ✅ Settings documents, inventory, hosts, rules and profiles live in the database; tested across reinstall. |
| Clear diagnostics for hardware failures | ✅ Start checks and messages that name the receiver and say what to do; IQ health (detailed for RTL-SDR, basic otherwise); the job's Execution record. |
| Documented upgrade and rollback | ✅ `docs/installation.md` (upgrading) and `docs/backup-and-restore.md` (rollback). |

## Completed features (briefs 1–10)

- Persistent, versioned settings with export/import and database backups.
- Installer, first-run wizard and simulation mode.
- Hardware discovery, local and over SSH.
- Receiver inventory and signal chains.
- A built-in pass predictor; rules with manual, automatic and hybrid scheduling.
- Reservations, the SSH recording backend, start checks and diagnostics.
- The recording archive: four separate outcomes, decoder profiles, storage, transfer and retention.
- A basic IQ health check for every recording.
- Optional Slack integration with manifest generation.
- The signal-path editor and test lab; SDR++ listening sessions.

## Known limitations

- **Hardware tested on real devices:** RTL-SDR and SDRplay RSP1B, through the production station's earlier code.
  HackRF, LimeSDR, PlutoSDR and USRP are supported through SoapySDR in code only.
- **The basic IQ check is a sanity check.** It is not calibrated per receiver.
- **Signal detection from the spectrum is a candidate.** It does not identify the transmitter.
- **Recordings kept on a receiver host** get no analysis, decode or IQ health check until they are transferred.
- **Retention removes raw IQ only.** Decoded products and plots are kept indefinitely.
- **Database:** SQLite only. PostgreSQL is a design goal but untested.
- **No user accounts.** The web UI uses an owner session (signed link) and the API a bearer token.
- **Slack:** one channel; no interactive buttons.

## Remaining issues before 1.0

1. **Run the branch on real hardware.** Deploy it on this station (release checklist) and record real passes on
   the RTL-SDRs and the RSP1B through the new runner.
2. **Install on a Raspberry Pi 4/5** (64-bit Raspberry Pi OS) with one RTL-SDR: installer, wizard, a recorded pass.
3. **One real remote host:** keep-on-host, transfer, and remote retention.
4. **Connect Slack to a real (test) workspace:** manifest import, Socket Mode, a command, a notification and an
   image upload.
5. **Separate this station from the product:**
   - `config/observatory.toml`, `deploy/` (sudo scripts, `temp-r02`, the legacy units) and `agent/` (the
     conversational bot, with its built-in station description) are this station's. Move them to a station folder
     or exclude them from release archives.
6. **Rerunning `install.sh` without `--upgrade` after pulling a new version** installs and migrates while the old
   service keeps running. It should stop the service, as `--upgrade` does. The docs say to always use `--upgrade`.
7. `starlab db restore` treats a relative path as a name in `backups/`; give an absolute path for other files.
8. SDR++ listening sessions under the installer's hardened systemd unit (bubblewrap, `NoNewPrivileges`, Ubuntu's
   AppArmor user-namespace rule) are unconfirmed on fresh installs. Google Calendar on a fresh install is untested.
9. FastAPI `on_event` deprecation warnings (should move to lifespan handlers before FastAPI removes them).
10. The legacy-recorder integration (`[legacy]`) is specific to this station and should be documented as such or
   removed once the cutover is done.

## Installation

See [installation.md](installation.md). In short:

```bash
git clone <repository> starlab && cd starlab
sudo ./install.sh                 # add --with-slack for Slack Socket Mode, --udev for USB SDR permissions
```

Then open the sign-in link the installer prints and follow the setup wizard
([first-run-setup.md](first-run-setup.md)).

## Recommended version

**`0.9.0b1`** — the first portable beta:
- feature-complete for the ten briefs;
- installs cleanly on Ubuntu and Debian;
- has not been exercised on real hardware in this form, or on ARM.

Promote to **1.0.0** after remaining issues 1–5 pass.
