# Starlab Observatory — current state

Audit of the repository as of 2026-10-14 (`feature/observatory-updates`, based on `main` 56ac0c0), plus the
production host it runs on. Read-only: nothing was changed while gathering this. File references are
`path:line` in the repository. Station coordinates, credentials and internal addresses are deliberately left out.

**One-paragraph summary.** Starlab is a working, single-station system: a Python package (`aobs`) whose main
process, the *broker*, owns every SDR reservation, records, decodes and analyses, publishes to Google Calendar,
and serves an HTTP API plus one web page (the RF signal-path editor). A separate Slack bot on another machine is
the main operator interface. It is well tested (303 tests) and careful about hardware, but it is built around one
machine: it imports pass prediction, orbital data, station coordinates and a device-lock library from an older
codebase installed under `/opt` and `/etc`, and it assumes a specific user, paths, receivers, antenna switch and
network.

---

## 1. Components and processes

| Process | Started by | What it does |
|---|---|---|
| **Broker** (`aobs broker`) | `observatory-broker.service` (system unit, `User=tommy`, runs from the `~/auto-observe` checkout) | HTTP API + `/rf` page (FastAPI/uvicorn), unified scheduler: planner, runner, processor, calendar sync, health watch, receiver leases for the Slack agent |
| **satops loop** (`aobs run`) | `deploy/systemd/aobs-satops.service` — **not installed, not running** | METEOR pass forecast, 10 readiness checks, outcome reconciliation, satops calendar publisher (`satops_publish = false`) |
| **Slack agent** (`agent/obsagent`) | systemd `--user` unit on a second host | Socket Mode Slack bot: Claude-driven conversational agent for the owner, `/radio-*` slash commands, posts broker notifications |
| **Legacy recorders** (outside the repo) | `observatory-record@OBS-R01`, `meteor-rsp1b` | Still record METEOR passes for receivers in `shadow` mode |
| **ADS-B** (outside the repo) | `observatory-adsb.service` (dump1090, root) | Shares OBS-R05; the broker stops/starts it via a sudoers rule when it needs the dongle |
| **SDRplay API service** | `sdrplay.service` (vendor) | Required for the RSP1B |

Broker internals run as threads in one process: runner tick 1 s (`scheduling/runner.py:70`), planner every 300 s
and calendar every 60 s (`scheduling/service.py` ~154-177), processing loop (`scheduling/processing.py:707`), lease
watchdog 5 s (`coordination/api.py:147-158`). Captures and decode/analysis workers are detached child processes
(`python -m aobs.scheduling.capture|procworker`) that survive broker restarts (`KillMode=process`) and are adopted
on startup (`runner.py:250-281`, `processing.py:681-684`).

## 2. Technology

- **Backend:** Python ≥ 3.12, FastAPI + uvicorn, SQLAlchemy Core (no ORM), `tomllib` config.
  numpy, scipy, skyfield, sgp4 and SoapySDR come from **system packages** via a `--system-site-packages` venv;
  only sqlalchemy/fastapi/uvicorn/google-auth are declared in `pyproject.toml` (python3-soapysdr is not mentioned).
- **Frontend:** one self-contained page, `src/aobs/scheduling/rf_ui.html` (~2,100 lines of vanilla HTML/CSS/JS, no
  build step, no external assets), served by `GET /rf` and re-read from disk on every request (`api.py:89, 393`).
  It contains the signal-path editor, the Signal Path Test Lab and the component-performance dashboard. There is
  no general dashboard; `docs/ARCHITECTURE.md` lists a `web/` dashboard as future work.
- **Primary UI today:** Slack (natural-language agent + slash commands).
- **Database:** SQLite (WAL, `synchronous=FULL`, `busy_timeout=10000`, `db/__init__.py:12-25`). Schema written to
  be PostgreSQL-portable (`JSONText`, `UTCDateTime`).
- **External tools:** `rtl_sdr`/`rtl_power`/`rtl_fm` (two builds installed: Debian and a source-built RTL-SDR Blog
  fork — `docs/RTL_SDR.md`), librtlsdr via ctypes (prefers `/usr/local/lib/librtlsdr.so.0`, `stepcapture.py:25`),
  SoapySDR + SoapySDRPlay3 + SDRplay API 3.15, SatDump 1.2.2, dump1090, sox/ffmpeg.

## 3. HTTP API

All routes are in `src/aobs/coordination/api.py`. About 80 endpoints in these groups:

| Group | Examples | Auth |
|---|---|---|
| Receiver leases (Slack agent, OBS-R05 only) | `POST /v1/lease`, `/sweep`, `/audio`, `/iq`, `/port` | Bearer |
| Unified scheduler | `POST /v1/schedule`, `GET /v1/jobs`, `/v1/capabilities`, `/v1/notifications`, `/v1/scheduler/status` | Bearer |
| Processing & artifacts | `/v1/recordings/{id}/decode|analyze|results`, `/v1/artifacts/{id}/…` | Bearer |
| Catalog & health | `/v1/catalog/*`, `/v1/status`, `/v1/health` | Bearer |
| RF editor | `GET /rf`, `GET/POST /v1/rf`, history, versions, rename, switch push | Cookie session or Bearer |
| Test Lab | `/v1/rf/tests*`, `/v1/rf/dashboard`, `/v1/rf/components/*` | Cookie session or Bearer |
| Signed downloads | `GET /dl/{id}/{exp}/{sig}`, `GET /rf/login/{exp}/{sig}` | HMAC signature |
| FastAPI built-ins | `/docs`, `/redoc`, `/openapi.json` | **None** (left enabled, `api.py:99`) |

**Authentication.** One shared static bearer token (`[broker].token_file`). No users or roles at the HTTP layer.
The web page signs in via a 15-minute HMAC link minted by the bot (`/v1/rf/link`), which sets a 30-day stateless
cookie (`HttpOnly`, `SameSite=Strict`, not `Secure` — plain HTTP on the tailnet); CSRF is an `X-AOBS-RF: 1` header
on non-GET requests. Revocation means rotating `rf_ui_key`. Authorization (owner / operators / viewers) exists only
on the Slack side. The API binds to a fixed Tailscale address; startup fails on any machine without it.

## 4. Persistence

### Database tables (`src/aobs/db/schema.py`)

| In use | Tables |
|---|---|
| Unified scheduler | `sched_jobs` (every reservation; recordings are rows here), `sched_job_events`, `sched_notifications` (Slack outbox), `sched_calendar` |
| Processing | `proc_attempts`, `artifacts` |
| RF editor / Test Lab | `rf_layouts` (append-only versions), `rf_tests`, `rf_test_presets` |
| satops | `satellite_passes`, `pass_status_history`, `recordings`, `calendar_events`, `readiness_checks`, `decoded_artifacts` |
| Meta | `schema_info` (`schema_version = 1`) |
| **Defined but unused** | `receivers`, `antennas`, `scan_profiles`, `scan_sessions`, `signal_clusters`, `signal_observations`, `analysis_jobs`, `decoder_attempts`, `notifications`, `system_health` |

**Migrations: none.** `init_db` runs `metadata.create_all()` and refuses a newer `schema_version`
(`db/__init__.py:28-35`). `create_all` never alters an existing table, so column changes are not applied to older
databases. Example: commit c0c254d added `rf_tests.test_type/component_id/refs`. Production happens to have them
(its table was created afterwards), so the gap is latent, but any older database would break. No automatic
backups (a manual `.backup` command is in `docs/RUNBOOK.md`).

### Configuration

- Resolution (`src/aobs/config.py:11-14, 46-56`): `--config` → `$AOBS_CONFIG` → `/etc/autonomous-observatory/observatory.toml`
  → **the repository's own `config/observatory.toml`**. Production has no `/etc` file and no `AOBS_CONFIG`, so it runs
  on the tracked file in the checkout. Consequence: any checkout of this repo, run without `AOBS_CONFIG`, points at
  the production database and `/data` paths.
- Tooling edits that tracked file in place: `aobs schedule set-mode` (`cli.py:438-460`) and the
  migrate/rollback scripts' `sed` of `satops_publish`.
- TOML sections: `[site]`, `[paths]`, `[legacy]`, `[forecast]`, `[readiness]`, `[outcome]`, `[calendar]`,
  `[antennas.*]`, `[antenna_switch]`, `[broker]`, `[scheduler]`, `[scheduler.receivers.<id>]` (+ `.auto`).
  Keys read by code but absent from the file: `test_min_lead_seconds`, `test_output_root`, `rtl_sdr_path`,
  `legacy_import_days`, `health_watch_seconds`, `auto_horizon_hours`. Present but never read:
  `[paths].secrets_dir`, `[antenna_switch].automation_enabled`, `[antenna_switch].id`.
- **TOML vs database:** only the RF layout lives in the database. The newest `rf_layouts` version overrides each
  receiver's antenna, signal path, amplifier, filters and the ANTSW-01 port map, at startup and on every save
  (`rflayout.py:292-341`); version 1 is seeded from the TOML. Everything else (receivers, modes, serials, auto
  rules, integrations) exists only in the TOML.

### Secrets

| Secret | Location | Notes |
|---|---|---|
| Broker API token | `[broker].token_file` under `/data/autonomous-observatory/secrets` | Broker exits if missing |
| Web sign-in key, download-link key | `<data_root>/secrets/{rf_ui_key,download_link_key}` | Auto-created 0600 |
| Google OAuth client/token or service account | `[calendar]` file paths | 0600 enforced |
| Antenna switch token | `~/.config/observatory/antswitch.env` | Broker raises `KeyError` at startup if missing — even with no switch |
| Slack, Anthropic, broker URL/token, SatNOGS | `~/.config/observatory/{slack,broker,satnogs}.env` on the agent host | Read by the agent only |

### State outside the database

- **Browser:** unsaved RF edits in `localStorage` (`aobs.rf.draft.v1`); per-browser only.
- **Broker memory, rebuilt on restart:** receiver/port overrides, presence and journal caches, `last_plan` /
  `last_calendar` status (a calendar-failure alert edge can be lost or repeated across restarts), the in-flight
  ad-hoc radio operation (lost).
- **Broker files:** `<data_root>/broker/lease.json` (leases, recovered on restart), `<data_root>/scheduler/reserve.lock`,
  ad-hoc captures under `<data_root>/captures`, `/run/observatory-sdr/adsb.env` (tmpfs).
- **Agent host:** conversations, pending requests, notification cursor and thread map under
  `~/observatory-agent/state` (persistent); Slack redelivery dedup and the receiver lease session are in memory
  (after an agent restart the lease is orphaned until its TTL, so ADS-B stays paused up to 10 minutes).

## 5. Satellites, scheduling and recording

### Pass prediction is not Starlab's own

All prediction goes through the legacy codebase (`src/aobs/legacy.py:44-56`): it imports `observatory_scheduler`
and `sdr_coordinator` from `/opt/observatory-sdr` and `pass_schedule` from `/opt/observatory-recorder`, and reads
`/etc/observatory-sdr/config.json` and `/etc/observatory-recorder/config.json`.

- **Station coordinates** come from that `/etc` file (`site.latitude_degrees` etc.). Starlab has none of its own.
- **TLE sources** are URLs in the same `/etc` file; the repository contains none. Fetching, the 12 h refresh and the
  14-day cache fallback are legacy code.
- **Skyfield** is used by the legacy predictor and, in Starlab, for 0° AOS/LOS and revolution numbers
  (`satellites/forecast.py:175-195`).
- **Three separate TLE caches** (satops, the planner — seeded from hard-coded `/data/...` paths at
  `planner.py:30-31` — and the legacy recorder).
- **Satellites:** METEOR-M2 3/4 only (NORAD IDs in `[scheduler.receivers.*.auto]`). Elevation limits come from
  the legacy station jobs.

### Two scheduling systems plus legacy recorders

1. **satops** (`src/aobs/satellites`, `src/aobs/calendar`): passes in `satellite_passes`, readiness, outcomes.
   Never records. Not currently running.
2. **Unified scheduler** (`src/aobs/scheduling`), inside the broker: the single reservation authority. Each
   receiver has a migration `mode`:
   - `legacy` — the old recorder's predicted windows are imported as locked reservations;
   - `shadow` — legacy windows plus the broker's own automatic jobs, planned but not executed, for comparison;
   - `broker` — the broker plans and records.
   Cutover is `deploy/sudo/migrate-recorders.sh` (preflight on the shadow report, mask legacy units, set mode
   `broker`). **It has not run:** OBS-R01 and RSP1B are `shadow` and their legacy recorders are live; OBS-R02 and
   OBS-R05 are `broker`.
3. **Legacy recorders** (`observatory-record@`, `meteor-rsp1b`), plus a legacy device coordinator (JSON lease file
   under `/run`).

### How a job gets an SDR

- `sched_jobs` keyed by **device serial**. Overlap is checked in application code under a process `RLock` +
  `fcntl.flock` on `reserve.lock` (`store.py:97-130, 223-229`); single-host only.
- Booking checks presence, storage (`free − committed ≥ estimate + reserve_free_gib`) and conflicts. Preemption:
  never a running job, never manual-over-manual, never legacy rows, never an auto job within 180 s of start
  (`store.py:199-219`).
- At start (`runner.py:98-160`): presence check → for the switched receiver, take a broker lease (stop ADS-B,
  select the switch port with read-back) → otherwise take a legacy-coordinator lease → wait up to 20 s for no
  visible USB holder (`/proc/*/fd` scan; no `lsof`/`fuser`) → spawn the capture 15 s before start.
- Devices are opened **by serial**: `rtl_sdr -d <serial>`, `SoapySDR.Device("driver=sdrplay,serial=…")`,
  `rtlsdr_get_index_by_serial`. Exception: dump1090 only takes an index, which the broker computes from the serial.
  Broker ad-hoc ops pass the **receiver ID** as `-d`, which only works because RTL receiver IDs equal their EEPROM
  serials.
- Capture validation (`capture.py:48-170`): `recorded` only if the process ran to the end, a first sample was seen,
  and the file is ≥ 98 % of the expected size; SDRplay settings are read back and verified. Writes `<raw>.part`,
  renames on success, writes `<raw>.result.json`.
- Job states: `scheduled → preparing → recording → recorded → processing → completed`, plus `failed`, `partial`,
  `missed`, `cancelled`, `preempted`, `skipped`, `shadow`; every transition is in `sched_job_events`.

### Known conflict risks between the systems

1. Legacy recorder (coordinator priority 100, no preempt) vs broker lease (priority 30) on the same dongle: the
   loser fails; avoided only because legacy windows are imported as reservations.
2. Independent TLE refreshes mean predicted windows drift; guard seconds absorb most of it.
3. **RSP1B has no device-level exclusion**: the USB holder check is RTL-only.
4. dump1090 is index-based; enumeration changes after a reboot can point it at another dongle (flagged by readiness).
5. Root-owned holders (dump1090) are invisible to the `/proc` scan.
6. satops and the broker would both publish calendar events for the same pass; prevented by `satops_publish = false`.
7. The reservation lock covers aobs processes only.

### Processing, analysis, retention

- One attempt at a time, `nice`/`ionice`, paused with SIGSTOP whenever a recording is near
  (`processing.py:502-569`). SatDump via `postprocess.satdump_command` (`postprocess.py:96-129`). Both METEOR auto
  rules currently have `satdump_pipeline = ""` (record only). A decode with no images is marked failed.
  `postprocess.postprocess()` is unused.
- Analysis: spectrogram/waterfall/clipping, IQ health (`iqhealth.py`), rtl_sdr log parsing (`rtllog.py`), Test Lab
  metrics, satellite quality, interference, component performance.
- Deletion: raw IQ of **auto** jobs after `auto_retention_hours` (48); raw after a successful decode if
  `retain_raw_iq = false`; broker ad-hoc captures after 72 h. Manual recordings are never pruned. Legacy recorders
  prune their own files (6 h).

## 6. Hardware support

| Hardware | Status |
|---|---|
| RTL-SDR (R820T) | Supported: sysfs discovery by VID:PID, serial identity, R820T gain table, `rtl_sdr` CLI |
| SDRplay RSP1B | Supported (RSP1A/1B limits only): SoapySDR, bootloader-state detection |
| Airspy, HackRF, Pluto, Lime, USRP, generic Soapy | **Not supported** (any other `driver` is rejected) |
| ANTSW-01 antenna switch | Custom ESP32 HTTP API, by port number; hard-coded ID `ANTSW-01` and LAN IP |
| Remote receivers | **Not supported**: readiness fails if `hostname != [site].host` (`readiness.py:130-133`) |

`rtl_test` and `lsusb` are not used. udev rules for USB access are installed on the host but not in the repository.

## 7. Integrations

- **Google Calendar:** two publishers (satops and broker) with separate event-ID schemes; OAuth installed-app or
  service account; credentials 0600.
- **Slack:** a separate deployable (`agent/`), Socket Mode, Anthropic SDK with a model and system prompt that
  hard-code this station (receivers, ports, timezone). No Slack app manifest or slash-command registration in the
  repository. Notifications travel via the `sched_notifications` outbox, polled every 20 s.
- **SatNOGS:** SSH to a locked-down gate on the station's own SatNOGS node (agent only).
- **Tailscale:** the broker binds a tailnet address; download links and the sign-in flow are documented as
  tailnet-only.

## 8. Hard-coded assumptions

Counts are matches in shipped code (`src/`, `agent/`); config, docs, deploy scripts and tests have many more.

| Assumption | Where |
|---|---|
| User `tommy`, checkout `/home/tommy/auto-observe` | all systemd units, sudoers rule, `deploy/sudo/*`, `deploy/temp-r02/*` |
| `/data` mount and fixed subpaths | config; code defaults in `planner.py:30-31, 268`, `spec.py:265`, `testlab.py:196`, `catalog.py:376` |
| `/opt/observatory-*`, `/etc/observatory-*` legacy stack | `legacy.py`, `coordination/hardware.py:89-100`, config `[legacy]` |
| Receiver IDs `OBS-R01/R02/R05`, `RSP1B`, switch `ANTSW-01` | 43 matches in code (e.g. `broker.py:91-93`, `scheduling/hardware.py:64-65`, `rflayout.py:40`, `api.py:25`) |
| Display timezone `America/New_York` | `config.py:39`, `timeutil.py:31,35`, `analysis.py:93`, three agent modules |
| A town name in calendar event locations | `calendar/render.py:117` |
| Tailscale bind address, switch LAN IP | config `[broker].host`, `[antenna_switch].base_url` |
| Second host names (agent host, SatNOGS node) | agent code and system prompt |
| Station coordinates | **not in the repository** (only in the legacy `/etc` file) |
| Sudo for ADS-B, `adm` group for journals, `plugdev` + udev rules for USB | host setup, partly in `deploy/sudo/broker-adsb-setup.sh` |

## 9. Reusable as-is or nearly

- Serial-keyed reservation store with preemption rules and storage accounting (`store.py`), job spec validation
  and per-driver radio limits (`spec.py`, `scheduling/hardware.py`).
- Detached capture processes with a result-file protocol and restart adoption (`capture.py`, `stepcapture.py`,
  `runner.py`) — the natural unit for remote execution.
- Processing queue with the recording-priority gate, SatDump wrapper, analysis suite, IQ health diagnostics.
- sysfs/`/proc` RTL inventory and holder detection; SDRplay presence.
- Append-only versioned configuration with optimistic concurrency, seeding from TOML and per-recording snapshots
  (`rf_layouts` / `rflayout.py`) — a ready pattern for all user configuration.
- Idempotent calendar mirroring; notification outbox; lease API.
- The test suite and its fakes (fake hardware, runner, calendar, switch board, SSH, Slack, satdump).

## 10. Documentation status

Current: `RF_PATHS.md`, `RF_DIAGNOSTICS.md`, `RTL_SDR.md`, `RUNBOOK.md`, mostly `SCHEDULER.md` and `COMMANDS.md`.
Stale: `ARCHITECTURE.md` (pre-broker layout, lists packages that don't exist), `OPERATIONS.md` (an `/opt` install
that never happened), `ROADMAP.md` (contradicts the README), `ENVIRONMENT.md` (a 2026-10-11 host snapshot with
internal addresses). `DISCOVERY_ENGINE.md` is an unimplemented design.

## 11. Open questions for the owner

1. `[forecast].receiver = "OBS-R02"` is marked TEMP (2026-10-12) while OBS-R02's legacy recorder is disabled and
   R01 is recording. Is the `deploy/temp-r02` revert still pending? (Its input files are gitignored, so the
   scripts cannot run from a fresh clone.)
2. Is the shadow → broker cutover for OBS-R01 and RSP1B still planned before the distribution work, or should the
   new architecture absorb it?
3. Is the satops loop (`aobs run`) still wanted, or is the broker the only scheduler going forward?
4. `meteor-auto.py` (RSP1B legacy recorder) lives outside the repository and was not audited.
