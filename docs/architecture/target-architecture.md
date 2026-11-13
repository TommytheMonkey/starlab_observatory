# Starlab Observatory — target architecture

Where the system should end up so that someone else can install it on their own Linux computer, connect their own
receivers, and run it without Slack, Tailscale, or this station's legacy software. It is built from the existing
code (see `current-state.md`), not a rewrite. Implementation order is in `migration-plan.md`.

## Design principles

1. **Evolve the broker, don't replace it.** The broker already holds the hard parts: serial-keyed reservations,
   detached captures with restart recovery, the processing gate, validation. Generalize it.
2. **Safe by default.** A fresh install records nothing until the user enables it, binds to localhost, and never
   touches hardware it wasn't told about.
3. **Everything optional except the core.** Slack, Google Calendar, Tailscale, SatNOGS, antenna switches, ADS-B
   sharing and the old recorders are integrations, off by default.
4. **One source of truth per setting.** User-editable settings live in the database, versioned. Files hold only
   what is needed before the database can be opened, plus secrets.
5. **No fabricated capability.** Hardware capabilities come from installed drivers and probes; passive RF parts are
   always user-declared.
6. **This station keeps working throughout.** Every phase runs here in compatibility mode before anything is retired.

## Shape of the system

```
                    ┌──────────────────────── starlab server (one process, one systemd unit) ─────────────────────┐
 browser ──HTTP──▶  │ web UI (static, no build) + REST API + auth                                                │
                    │ config service (versioned, validated, import/export) ── SQLite (or PostgreSQL)             │
                    │ predictor (Skyfield, TLE/OMM sources, cache) ─▶ planner (rules) ─▶ reservation store       │
                    │ runner ─▶ recording backends: local │ SSH │ (future) agent                                │
                    │ processor (SatDump, analysis) ─▶ archive + retention                                       │
                    │ integrations: notifications outbox ─▶ Slack / Calendar / webhooks; switches; legacy        │
                    └───────────────────────────────────────────────────────────────────────────────────────────┘
                              │ SSH (pinned host key, forced command)
                              ▼
                    remote receiver host: starlab-remote helper + SDR tools
```

The Slack bot remains a separate process talking to the API, because it has its own dependencies (Slack SDK,
optionally Anthropic) and may run elsewhere.

## Configuration and persistence

### Two layers

| Layer | Holds | Where |
|---|---|---|
| **Bootstrap file** (TOML) | data directory, database URL, bind address and port, log level, secrets directory | `--config` → `$STARLAB_CONFIG` (and `$AOBS_CONFIG` for compatibility) → `/etc/starlab/starlab.toml` → `$XDG_CONFIG_HOME/starlab/starlab.toml`. **No fallback to a file inside the source tree.** |
| **Settings** (database) | observatory identity and location, receiver hosts, receivers, inventory, signal chains, satellites, orbital sources, rules, decoder profiles, storage, retention, notifications, integrations, UI preferences | versioned configuration tables |

The existing TOML becomes an *importer*: on first start, if the settings tables are empty, it seeds them — exactly
what `rflayout.seed_from_config` does today for signal paths. After that the database wins and the TOML is never
rewritten by tooling.

### Versioned settings

Generalize the `rf_layouts` pattern: each settings document (e.g. `observatory`, `receivers`, `inventory`,
`rules`, `integrations`) is stored as append-only versions with author, note and timestamp. Writes use optimistic
concurrency (`base_version`), validation runs server-side before commit, and history/restore come free. Recording
jobs keep snapshotting what they used (as `spec.resolved.rf_path` already does).

API: `GET/PUT /v1/config/{doc}`, `POST /v1/config/{doc}/validate`, `GET /v1/config/{doc}/history`,
`POST /v1/config/{doc}/restore/{version}`, `GET /v1/config/export`, `POST /v1/config/import` (dry run first).
Exports never include secrets.

### Secrets

A secrets directory (default `<data>/secrets`, mode 0700, files 0600) holding named secrets: API tokens, Slack
tokens, calendar credentials, SSH private keys. Settings reference secrets by name. Secrets are written through the
API (write-only; the UI shows "set / not set") and excluded from exports, logs and error messages. Optional later:
system keyring.

### Database

SQLite by default; PostgreSQL supported because the schema is already written to be portable. **Versioned
migrations** with a numbered migration runner keyed on `schema_info.schema_version`, an automatic backup (SQLite
online backup API) before every migration, and a refusal to start on a newer schema (kept from today). The unused
tables are either brought into service (`receivers`, `antennas`) or dropped in a migration.

## Observatory, prediction and satellites

- **Observatory identity:** name, description, latitude, longitude, elevation, timezone — in settings, entered in
  the wizard. Never in the repository.
- **Predictor in Starlab** (`aobs.satellites.predict`): Skyfield, multiple orbital-element sources (TLE and OMM;
  CelesTrak groups, custom URLs, local files), a single local cache with fetch timestamps, staleness warnings,
  per-satellite frequency/mode defaults. Calculates AOS, LOS, max elevation and its time, duration, AOS/LOS azimuth.
  UTC internally, the configured timezone for display.
- **Legacy predictor** becomes an adapter used only by `legacy`/`shadow` receivers on stations that still run the
  old recorders, so this station can cut over at its own pace.
- One pass model: `satellite_passes` (predictions) feeding `sched_jobs` (reservations). The satops loop is retired
  after its readiness checks are folded into the planner/health.

## Receivers and hosts

- **Receiver host:** `local`, or `ssh` (hostname, port, user, key secret, **pinned host key** — trust-on-first-use
  shown to the user for confirmation, never `StrictHostKeyChecking=no`). Remote hosts run a small forced-command
  helper (`starlab-remote`) that exposes only `discover`, `capture`, `status`, `fetch`, `cancel`, modelled on the
  SatNOGS gate pattern already in use.
- **Receiver identity:** `(host, driver, serial)`; a user-chosen friendly ID and name are separate fields.
  Receiver IDs no longer have to equal EEPROM serials. Devices without a usable serial are flagged and identified
  by USB path with a warning; duplicate serials are reported, with the `rtl_eeprom` fix suggested.
- **Driver adapters**, each with discovery, capability report, open-by-identity and capture:
  `rtlsdr` (existing), `sdrplay` (existing, generalized beyond RSP1B), then `airspy`, `hackrf`, and a generic
  `soapy` adapter. Capabilities (frequency range, sample rates, gains, bias tee) come from the driver/probe output
  or a known-model table, never assumed.
- **Simulation driver:** produces synthetic IQ so the whole pipeline (scheduling, capture, validation,
  processing, UI) runs with no hardware — for demo mode, tests and development.
- **Inventory and signal chains:** the RF editor generalized: user-declared antennas, LNAs, filters, attenuators,
  splitters, switches, rotators, cables; chains per receiver; warnings for frequency/bias-tee/power mismatches.
  Switches become a component type with optional drivers (`antsw-http` for ANTSW-01-style boards); nothing named
  `ANTSW-01` in code.

## Scheduling and execution

- **Rules** (persistent, per the Phase 6 brief): satellites, min max-elevation, preferred and backup receivers,
  frequency/rate/gain profile/format, pre/post margins, decode, raw retention, priority, storage target.
- **Planner** generates `auto` jobs from rules; manual jobs and manual overrides of auto jobs are never silently
  rewritten by a refresh (today's restore/skip logic extends to this).
- **Reservation store** stays serial-keyed. The cross-process lock moves into the database (a reservation row with
  expiry, or `BEGIN IMMEDIATE` transactions on SQLite / advisory locks on PostgreSQL) so it is not tied to one
  filesystem.
- **Recording backends:** `LocalBackend` (today's runner + capture process), `SshBackend` (runs the same capture
  module through `starlab-remote`, streams status, validates on the remote side, transfers only what policy says),
  later an agent backend. Structured request/result JSON, never shell strings built from user input.
- **Job states:** keep the existing states and map to the Phase 6 vocabulary (`scheduled`→SCHEDULED,
  `preparing`→RESERVED, `recording`, `processing`, `completed`, `failed`, `cancelled`, `skipped`; `predicted` is a
  pass without a job). `completed` stays gated on validated output.
- **External device users** (ADS-B decoders, SDR++, other recorders) are modelled as *device sharing policies*:
  "stop unit X while Starlab needs device Y" with an explicit permission step, instead of hard-coded ADS-B logic.

## Processing and archive

Keep the processor and its recording-priority gate. Add decoder profiles (pipeline, parameters, per satellite/mode),
distinct outcomes (recorded / signal detected / frames decoded / image produced — partly present today), storage
locations per host, optional transfer to central storage, and retention policies applied only as configured.

## Web interface

A real application shell around today's page: **Dashboard**, **Passes & Schedule** (calendar, upcoming passes,
rules, manual jobs, history), **Recordings** (library, details, images, logs), **Receivers & RF** (hosts,
receivers, discovery, inventory, signal paths, Test Lab), **Settings** (observatory, storage, integrations,
users), and the **Setup wizard**. Same technology as today — static HTML/CSS/JS modules served by the API, no
build toolchain — so installing needs no Node.js. Every editor follows the pattern the RF editor now has: explicit
Save/Cancel, unsaved indicator, server-confirmed saves, local draft recovery.

## Security

- Default bind `127.0.0.1`; LAN or other interfaces are an explicit setting, with a warning when not on HTTPS.
- **Local accounts:** the wizard creates an admin (password hash in the database); roles admin / operator / viewer,
  mirroring the Slack tiers. Session cookies (`Secure` when served over HTTPS). Named, revocable API tokens for the
  Slack bot and scripts, replacing the single shared bearer token (still accepted during migration).
- OpenAPI docs require login or are disabled.
- Sudo is never required by the core. Device-sharing policies that need to stop system units document the exact,
  minimal sudoers line for the user to install.

## Installation and packaging

- **Python package** installable with `pip`/`pipx`, all Python dependencies declared (numpy, scipy, skyfield,
  sgp4 from PyPI; SoapySDR detected as an optional system module).
- **Installer** (`starlab install`, bootstrapped by a small shell script): detects OS/architecture (Debian/Ubuntu,
  Raspberry Pi OS; x86_64/arm64), checks Python, tools and disk, creates a service user (default `starlab`) and
  directories (all paths configurable), installs udev rules for supported SDRs, writes the bootstrap TOML and a
  systemd unit, initializes the database, starts the service and prints the URL. No recording until the wizard
  finishes and the user enables rules.
- **System dependencies** (rtl-sdr, SoapySDR, SatDump, vendor SDRplay API) are detected and reported with install
  hints; the installer does not silently install vendor binaries.
- **Containers:** the layout (one process, one data directory, config via env/file) is container-friendly; a
  Dockerfile with USB passthrough is a later deliverable.
- **Upgrades:** `starlab upgrade` = backup → install new version → migrate → health check; documented rollback =
  previous version + restore of the pre-migration backup.

## Integrations

| Integration | Shape |
|---|---|
| Slack | Optional. Settings page generates a minimal Socket Mode manifest for the enabled features. Notifications and `/starlab` commands need no LLM; the Claude-driven conversational agent is a further opt-in needing an Anthropic key. Station facts in the agent's prompt come from the API, not hard-coded text. |
| Google Calendar | Optional, one publisher (the scheduler's). |
| Webhooks / email | Optional notification sinks on the same outbox. |
| SatNOGS | Optional "external station" integration (status/logs via a gated SSH key, never using its radio). |
| Legacy recorders | Compatibility adapter for this station's migration only; not documented for new installs. |

## Compatibility with this station

This installation is the first user of every phase. Concretely: its TOML is imported into settings, its receivers
keep their IDs, `legacy`/`shadow` modes stay available, the existing bearer token stays valid, the ANTSW-01 board
becomes a configured switch, Slack keeps working with the same tokens, and its database is migrated in place with a
backup taken first.
