# Autonomous Radio Observatory — architecture

## Principles
1. **The legacy recorder stays the source of truth for satellite capture.** We import its predictor and read its journal, metadata and leases. We never re-implement or modify it.
2. **Fail closed.** If schedule data is missing or stale, discovery releases receivers, and readiness reports WARNING rather than READY.
3. **Local first.** Observatory keeps working with no internet, no Neon and no monkeybrain. SQLite on `/data` is the operational store. Neon is an optional replica for history and the dashboard (Phase 3/4).
4. **Never claim what wasn't verified.** That covers READY, RECORDED, VERIFIED DECODE and the "priority enforced" claims.

## Components (package `aobs`)
```
hardware/        rtlsdr (sysfs inventory, librtlsdr index map, /proc USB holders), antsw (read-only)
legacy.py        adapter over /opt/observatory-recorder + /opt/observatory-sdr (import, journal parse)
satellites/      forecast → readiness → outcomes → service (cycle + status state machine)
calendar/        render (pure), google_api (REST, SA or OAuth), sync (idempotent plan/apply)
db/              SQLAlchemy Core schema, portable SQLite → PostgreSQL
coordination/    [Phase 2] pass guard + receiver broker (see DISCOVERY_ENGINE.md)
discovery/       [Phase 2] scan profiles, detector, IQ capture
analysis/        [Phase 3] durable queue, monkeybrain worker, classifiers, decoder plugins
web/             [Phase 4] FastAPI dashboard on its own port
notify/          [Phase 4] webhook/email with dedup + cooldown
```

## Satellite pass state machine
```
            ┌──────── forecast ────────┐
 (new) → SCHEDULED ⇄ READY ⇄ WARNING ──┤ capture window opens
                                       ▼
                           RECORDING / WARNING(no capture visible)
                                       │ window end + 5 min grace
                    ┌──────────────────┼──────────────────┐
                 RECORDED            FAILED             MISSED
                    │ verified decode artifact
                 DECODED
```
* SCHEDULED: predicted; every current check passes but the pass is beyond the recorder's own 12 h horizon.
* READY: all 10 checks pass and the pass is inside the recorder horizon. For the recorder's *next* window the journal must show the same plan (Δ ≤ 90 s).
* WARNING: any check WARN/FAIL, or the pass vanished from the forecast.
* RECORDED: every segment has status `recorded`, a raw file of full size, and ≥ 90 % window coverage.
* FAILED: a failed or partial segment, missing or truncated raw file, or low coverage. MISSED: no segment metadata at all.
* DECODED: only on a `decoded_artifacts.verified` row (the Phase 3 decoder writes these).
Every transition is written to `pass_status_history` with its reason. That record is what explains missed recordings.

## Readiness checks
host_reachable · receiver_present (sysfs serial) · recording_service_active (systemd) · valid_prediction (TLE age plus agreement with the recorder's own TLE cache) · recorder_agreement (journal) · storage (free − pass bytes ≥ recorder floor) · receiver_assignment (recorder targets, station job, capability) · antenna_configuration (hard-wired antenna covers the band; ANTSW-01 does not feed this receiver) · destination_writable (mounted, rw, writable, not a symlink) · no_scheduling_conflict (foreign lease, foreign USB holder such as SDR++, ADS-B index-0 drift).
Limitation, stated in every result: root-owned processes' file descriptors cannot be inspected without privileges.

## Calendar
* One event per recording window, 10 min long, starting at the actual capture start. The description shows the full window and the planned segments.
* Event id = `aobs<norad>r<revolution>`, which is deterministic. Insert → 409 → update means a lost database never creates duplicates.
* Body hash stored per event. Unchanged events are not re-sent, and times are rounded to whole seconds so millisecond prediction jitter doesn't cause churn.
* Pass identity across TLE updates: same satellite with culmination within ±15 min (the orbital period is ~101 min).
* Vanished passes are updated to WARNING with the reason. They never keep a stale READY.
* Gate before any write: `enabled`, a calendar id that is accessible with writer role, credentials present, and the next pass's forecast confirmed by the running recorder.
* Auth: service account (scope `calendar.events`, so it can only reach calendars shared with it). OAuth with `calendar.app.created` is also supported.

## Data placement
| What | Where |
|---|---|
| Operational DB | `/data/autonomous-observatory/db/observatory.sqlite3` (WAL, synchronous=FULL) |
| Secrets | `/data/autonomous-observatory/secrets/` (dir 700, files 600; code refuses group/world-readable secrets) |
| TLE cache | `/data/autonomous-observatory/tle` (seeded from the recorder's cache, same fetch policy) |
| Discovery IQ, archive | `/data/autonomous-observatory/{iq,archive}` with quotas (Phase 2/3) |

Our archive quota must never push `/data` free space toward the recorder's 64 GiB floor. If it did, the recorder would refuse to record. The storage guard reserves floor + headroom before writing anything.
