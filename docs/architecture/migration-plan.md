# Starlab Observatory — migration plan

How to get from `current-state.md` to `target-architecture.md` in small steps, without breaking the running
station. Phases follow the briefs in `prompts/` (2–10); each phase ends with a review before the next starts.

## Ground rules for every phase

- **Production is `~/auto-observe` on `main`.** Development happens in the `~/starlab-dev` worktree on a feature
  branch, using the isolated sandbox in `.dev/` (own config, database, data, secrets; broker on
  `127.0.0.1:18751`; calendar, switch and auto rules off; receiver serials that match no real device). Always
  `. .dev/env.sh` first: without `AOBS_CONFIG` the code falls back to the production config.
- **Do not start a dev broker against real hardware** until the simulation driver exists (Phase 3). Probing an SDR
  that production is recording from can break the recording.
- A phase merges to `main` only when: the full test suite passes, new behaviour has tests, the phase's migration
  has been run against **a copy** of the production database, and the owner has reviewed it.
- Deploying to production = merge, then a broker restart **when nothing is recording** (owner runs
  `sudo systemctl restart observatory-broker`). Database migrations always take a backup first.
- No phase removes existing functionality. Old paths are kept behind compatibility switches until a later phase
  explicitly retires them with the owner's agreement.

## Phase 0 — Separate production and development ✅ (2026-10-14)

Done: `main` fast-forwarded to the day's fixes, production checkout back on `main`, `~/starlab-dev` worktree,
`.dev/` sandbox (git-ignored), separate venv, tests passing (303). MIT license added on the feature branch.

## Phase 1 — Audit and architecture ✅ (this document set)

## Phase 2 — Foundations and persistent configuration (brief 2)

Split into reviewable steps:

**2a. Safe configuration resolution.**
- Production first: add `Environment=AOBS_CONFIG=/home/tommy/auto-observe/config/observatory.toml` to the
  production unit (owner, sudo; restart when idle). Behaviour is unchanged, but now explicit.
- Then remove the "config file inside the source tree" fallback; the code refuses to start without an explicit
  config and says how to fix it. Add `STARLAB_CONFIG` (with `AOBS_CONFIG` as an alias).
- Make optional integrations really optional at startup: a missing switch token, calendar credentials or Tailscale
  address disables that feature with a clear health warning instead of a crash.

**2b. Versioned database migrations and backups.**
- Numbered migrations (`src/aobs/db/migrations/NNNN_name.py`) applied in order from `schema_info.schema_version`;
  `create_all` only for a brand-new database, which is then stamped with the latest version.
- Before any migration: an online SQLite backup to `<data>/backups/pre-migration-<from>-<to>-<time>.sqlite3`;
  migrations run in a transaction; failure leaves the original untouched.
- Migration 0002 brings older databases up to today's schema (e.g. the `rf_tests` columns). Production already
  matches, so it is a no-op there, which is the point: it proves the runner on the live schema shape.
- `aobs db status|migrate|backup|restore` commands.

**2c. Versioned settings documents.** Generalize `rf_layouts` into a settings store; first documents:
`observatory` (identity, location, timezone), `storage`, `notifications`, `integrations`. Seed from the existing
TOML on first start. Read/validate/update/history/restore/export/import endpoints. Secrets directory with
write-only API.

**2d. Settings UI shell.** A Settings section around the existing page, with Save/Cancel, unsaved indicators,
server-confirmed saves and draft recovery (reusing the RF editor's pattern).

Compatibility: the broker keeps reading every value it reads today; settings documents override TOML values only
for keys that have moved, one by one, each covered by a test.

## Phase 3 — Installer, first-run wizard, simulation mode ✅ (2026-10-15, feature branch; see `INSTALL.md`)

Done:
- `install.sh` (system or user installs, every path configurable, safe to rerun, `--upgrade` with a database
  backup) and `uninstall.sh` (keeps data unless `--purge` is confirmed);
- bootstrap config search order and defaults, so a minimal `starlab.toml` with only `[paths] data_root` is a
  complete installation;
- the legacy integration and the antenna switch made optional;
- a built-in Skyfield predictor with bundled, labelled sample elements and an opt-in CelesTrak refresh. It matched
  the legacy predictor on 12/12 METEOR passes at this station: identical rise and set, peak within 0.05°;
- the Passes view with booking;
- the `sim` driver: simulated devices only in simulation mode, synthetic labelled recordings, and readiness
  "simulated" that never becomes "verified";
- the 10-step first-run wizard with progress in the database;
- `starlab web-link` and `starlab db migrate`;
- `tests/install/` container test.

Not done:
- remote receiver hosts in the wizard (Phase 4);
- local accounts with passwords: sign-in remains signed links;
- automatic-recording rules from the wizard (Phase 6);
- Slack in the wizard (Phase 9);
- a Dockerfile for running Starlab itself (the layout is container-ready: one process, one data folder, config
  from `STARLAB_CONFIG`).

## Phase 3 — Installer, first-run wizard, simulation mode (brief 3, original plan)

- `starlab install` + bootstrap script, service user, configurable paths, systemd unit template, udev rules,
  database init, URL printout; tested in throwaway directories and a container/VM, never against production.
- Local accounts and sessions (admin created in the wizard); named API tokens; the shared bearer token stays valid.
- Wizard steps persisted and resumable; steps for features from later phases (discovery, rules, Slack) are present
  but simple until those phases fill them in.
- **Simulation driver** (synthetic IQ) so the full pipeline runs without hardware; from here on, the dev broker can
  run safely.
- Default bind `127.0.0.1`; OpenAPI docs behind login.

## Phase 2 — Persistent configuration ✅ (2026-10-15, feature branch; see `docs/configuration.md`)

Done:
- schema 3 with `settings_docs`, versioned documents for observatory, storage, notifications, integrations and UI;
- seeded once from the TOML, then the database wins (applied over the TOML in memory, so nothing else changed);
- one `/v1/config` API also covering signal paths, receivers and the read-only TOML scheduling rules (read,
  validate, update, history, restore, export, import);
- the Settings page with Save/Cancel, unsaved indicators, server-confirmed saves and draft recovery;
- `aobs db status|backup|list|restore`;
- `scripts/rehearse-migration.py`;
- `docs/release-checklist.md`.

Deferred:
- 2a: explicit `AOBS_CONFIG` in the unit, and removing the repo-config fallback; it needs the owner's sudo.
- Satellite selections and automatic rules: still in the TOML, shown read-only, until Phase 6.
- Receiver hosts: Phase 4 (remote).

## Phase 3a — Managed SDR++ server sessions ✅ (2026-10-15, feature branch; see `docs/sdrpp-sessions.md`)

Done:
- sessions as scheduler-visible reservations: `rx_sessions`, schema 4;
- the SDR++ server sandboxed to exactly one receiver with bubblewrap, because SDR++ itself can silently fall
  back to another device and lets clients switch;
- a policy to end sessions before bookings, with warnings;
- a per-session process group with an identity marker, a watcher, recovery after restarts, and a hard lifetime;
- loopback, Tailscale or trusted binding only;
- a simulated backend and a mock SDR++;
- development isolation after the 2026-10-15 incident: `scripts/dev`, `STARLAB_DEV` guards, broker lock,
  regression tests.

The backend interface is ready for a remote (SSH) backend in Phase 4.

## Phase 4/5 (part 1) — Local receiver discovery and inventory ✅ (2026-10-15, feature branch)

Done ahead of 2a and 2c, at the owner's request (see `docs/receivers.md`):
- schema 2 with a migration runner and automatic pre-migration backup (the core of 2b);
- `inv_receivers`/`inv_events` inventory with TOML import, and passive USB discovery;
- SoapySDR identify, with capabilities taken only from the driver probe;
- the airspy and generic SoapySDR capture driver (quick tests);
- IQ sample verification and the discovered → configured → verified lifecycle;
- the rule that recording jobs need a verified (or in-service) receiver;
- the Receivers page.

Still open from briefs 4 and 5:
- remote SSH hosts;
- wideband scans, gain sweeps and satellite tests for SoapySDR receivers;
- automatic recording for SoapySDR receivers;
- antenna and inventory records beyond the signal-path layout.

## Phase 4 — Local and remote discovery (brief 4)

Driver adapters (rtlsdr, sdrplay, then airspy, hackrf, generic soapy) with capability reports from real probe
output; mocked outputs in tests. Receiver hosts with SSH (pinned host keys, key secrets, connection test) and the
`starlab-remote` forced-command helper. Receiver identity `(host, driver, serial)` with separate friendly IDs.
**Discovery must never open a device that has an active reservation** (checked against `sched_jobs`).

## Phase 5 — Inventory and signal chains (brief 5)

Bring the `receivers`/`antennas` tables into service (or replace them in a migration), generalize the RF editor:
rotators, polarization, mounting, more validation; switches become component types with drivers. The `rf_layouts`
history is migrated, not discarded; per-recording `rf_path` snapshots stay valid.

## Phase 6 — Built-in predictor and rules-based scheduler (brief 6)

Skyfield predictor with TLE/OMM sources and one cache; observatory location from settings; automation rules
replacing `[scheduler.receivers.*.auto]` (imported on migration). The legacy predictor stays only as the adapter for
`legacy`/`shadow` receivers. Run the new planner in **shadow** against the current one on this station (the
existing shadow-report mechanism) before it is allowed to execute. Manual overrides are never overwritten.

## Phase 7 — Execution backends and reservations (brief 7)

`LocalBackend` (refactor of today's runner, behaviour-identical) and `SshBackend`. Reservation locking moves into
the database. Backup receivers (compatible only). Device-sharing policies replace hard-coded ADS-B logic; the
OBS-R05/dump1090 arrangement becomes the first configured policy. Error translation into actionable messages.

## Phase 8 — Recording library, decoding, archive (brief 8)

Decoder profiles, separate outcome levels, storage locations and transfer policy, retention policies (existing
auto-retention becomes the default policy), library UI.

## Phase 9 — Optional Slack integration (brief 9)

Manifest generator, credentials via the secrets API, notification settings, `/starlab` commands without an LLM,
the conversational agent as a separate opt-in. This station's bot keeps its tokens and behaviour; its hard-coded
station description is replaced by data from the API.

## Phase 10 — Release preparation (brief 10)

Fresh-install tests (Ubuntu, Raspberry Pi OS, no SDR, one RTL-SDR, several, Airspy if available), upgrade and
rollback rehearsal, documentation set, honest readiness report, version number.

## Database migration requirements

| Need | Approach |
|---|---|
| Versioning | `schema_info.schema_version`, numbered migrations, refuse newer schema (existing) |
| Safety | Online backup before migrating; one transaction per migration; verify row counts on critical tables afterwards |
| SQLite limits | Table rebuild (create new, copy, swap) for column changes SQLite can't `ALTER` |
| PostgreSQL | Same migrations must run on PostgreSQL; tested in CI once CI exists |
| Data to carry over | `sched_jobs` and events, `proc_attempts`, `artifacts`, `rf_layouts`, `rf_tests`, presets, calendar mappings, `satellite_passes` history |
| New data | settings documents (seeded from TOML), users, API tokens, receiver hosts, rules, decoder profiles, retention policies |
| Unused tables | Adopt or drop explicitly in a migration; never silently |

## Backward compatibility

- Config: `AOBS_CONFIG` stays an alias; the TOML is imported, then read-only. `aobs schedule set-mode` and the
  migrate/rollback scripts are changed to write settings, not the tracked TOML, in the phase that moves receiver
  modes.
- API: existing `/v1/*` routes and the bearer token keep working until Phase 9 at the earliest; new routes are
  additive. The Slack agent is updated in lock-step when a route changes.
- Receivers keep their IDs; jobs and recordings keep their `device_serial` keys.
- CLI: the `aobs` command remains; a `starlab` alias is added.
- Package/module rename (`aobs` → `starlab`) is cosmetic and deferred to the end, if done at all.

## Service migration strategy (this station)

1. Phase 2a: make the production unit's config explicit (sudo, owner).
2. Each merged phase: restart the broker when idle; health check (`/v1/health`, `aobs schedule preflight`).
3. The legacy → broker receiver cutover (`migrate-recorders.sh`) is independent and can happen whenever the owner
   decides; Phase 6 must work in both states.
4. When the installer exists, re-home this station onto it (service user, `/etc/starlab`, data under `/data`
   kept), as a deliberate, scheduled step with a tested rollback — not as a side effect of a phase.

## Testing strategy

- Unit and integration tests stay hardware-free (fakes for hardware, runner, SSH, Slack, calendar, switch,
  SatDump). Every phase adds tests for its behaviour and for migrations of a database snapshot from the previous
  version.
- A **sanitized copy of the production database** (no secrets; coordinates never stored there) is used to test each
  migration before it touches production.
- Installer tests in temporary prefixes and a disposable container/VM.
- The simulation driver gives end-to-end tests: schedule → capture → validate → process → archive.
- Real-hardware checks on this station are explicit, manual, owner-approved steps, recorded in the phase report.
- CI (GitHub Actions) is added in Phase 2 for tests on push, which also catches a missing dependency declaration.

## Rollback procedures

| Change | Rollback |
|---|---|
| Code (any phase) | `git switch` production to the previous `main` commit (tag each deployment), restart when idle |
| Database migration | Stop broker, restore the automatic pre-migration backup, deploy the previous code, start |
| Settings change | Restore an earlier settings version from history (UI or API) |
| Unit/config change | Each production change is a one-file edit, recorded in the phase notes with its inverse |
| Receiver cutover | Existing `deploy/sudo/rollback-recorders.sh` |

## Known risks

1. **Repo-config fallback**: running code from a checkout without `AOBS_CONFIG` touches production. Mitigated by
   the dev sandbox now and removed in 2a.
2. **Hardware contention during development**: any probe of a device production is recording from can ruin a
   pass. Mitigated: dev config matches no real serial; no dev broker until simulation mode exists.
3. **Legacy dependency depth**: prediction, TLEs, coordinates and the device coordinator are all external. Phase 6
   is the largest single change; it runs in shadow first.
4. **SQLite migrations** need table rebuilds for some changes; mistakes there lose data without backups — hence
   mandatory backups and copy-first testing.
5. **Scope**: ten briefs is a large programme. Each phase must leave the system releasable on its own.
6. **Untestable hardware**: Airspy, HackRF, Pluto, Lime, USRP and Raspberry Pi installs aren't available here; they
   will be tested with recorded tool output only, and the release report must say so.
7. **Secrets in history**: the public repository's docs contain internal addresses and identifiers (e.g.
   `docs/ENVIRONMENT.md`); they should be scrubbed from the docs before release. A full secret scan of the git
   history belongs in Phase 10.
8. **Single maintainer, live station**: deployments only when idle, one phase at a time.

## Recommended first implementation step

**Phase 2a + 2b: explicit configuration resolution and a versioned migration runner with automatic backups.**
Everything later (settings store, installer, rules, new tables) depends on being able to change the schema safely
and on never touching the wrong config. Both are small, testable, invisible to users, and reduce risk immediately.
The one production change (adding `AOBS_CONFIG` to the unit) is a single line for the owner to apply with sudo.
