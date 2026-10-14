# Phase 0 — Environment inventory (verified 2026-10-11, read-only)

Everything here was observed on the host, not taken from the brief. Discrepancies with the brief are marked ⚠.

## Host `observatory`
Ubuntu 26.04.1, kernel 7.0.0-38, Python 3.14.4, x86_64. User `tommy` (groups adm, plugdev, dialout, docker). **No passwordless sudo.**
Disks: `/` 913 GB (6 % used); `/data` 916 GB NVMe ext4, 859 GB free. `/opt` and `/etc` are root-owned → development happens in `/home/tommy/auto-observe`, data in `/data/autonomous-observatory`.

## SDR receivers (sysfs + librtlsdr, no device claimed)
| Serial | Hardware | USB | librtlsdr index | Current user |
|---|---|---|---|---|
| OBS-R01 | Realtek RTL2838UHIDIR | 3-10.2 | 1 | `observatory-record@OBS-R01` (METEOR passes only) |
| OBS-R02 | Realtek RTL2838UHIDIR | 3-10.1 | 2 | `observatory-record@OBS-R02` (ISS passes) **and SDR++ server currently has it open** |
| OBS-R05 | Nooelec NESDR SMArt v5 | 3-4 | 0 | `observatory-adsb.service` (root dump1090) |

⚠ SDRplay RSP1B, Airspy, HackRF and additional RTL-SDRs are **not attached to this host**.
⚠ `/etc/observatory-sdr/config.json` still describes OBS-R05 as "MA-30 active loop / HF". It is the ADS-B receiver now.

## Existing services
| Unit | State | Notes |
|---|---|---|
| observatory-adsb.service | active, enabled, root | `dump1090 --device-index 0` — **selects by index, not serial**. Index 0 is OBS-R05 today; if USB enumeration changes, a restart would seize another receiver. Readiness checks this every cycle. |
| observatory-record@OBS-R01 / @OBS-R02 | active, user tommy | `/opt/observatory-recorder/record.py`; pass-only raw cu8 capture. |
| observatory-sdr-scheduler.service | **disabled / not running** | Older scheduler+SatDump worker; superseded by the recorder. |
| observatory-sdr-preflight.timer | disabled | |
| lighttpd | active, :80 / :8080 | ADS-B radar at `http://192.168.4.66:8080/gmap.html`. Untouched. |
| SDR++ server | `sdrpp --server` on :5259, **started by hand from a login shell** (PID 574447) | Not a systemd unit → does not survive reboot. Bypasses the coordinator. Configured device: OBS-R02. |
| Intercept (?) | something root-owned listens on :5050 but **does not answer** (backlog 37, HTTP timeouts) | The coordinator's `intercept_release()` would time out against it. |
| rf_observatory console | :8740/:8741 on the tailnet IP, hand-started (`~/radio-observatory`) | Earlier project; not modified. |
| meshtastic-gateway.service | active | Unrelated, untouched. |

## Satellite recorder — actual behaviour
* `PassSchedule` imports `observatory_scheduler.predict_passes()` with `/etc/observatory-sdr/config.json`.
* **Forecast horizon is `passes.look_ahead_hours = 12`** (3 METEOR windows). Calling the same predictor with the value overridden on a deep copy gives a complete 72 h forecast (18 windows); a test proves the 12 h set is an exact subset.
* "rise/set" are **crossings of the job's `min_elevation_degrees` (20°)**, not the horizon. Recording window = 20° rise − 120 s → 20° set + 120 s. Overlapping windows of M2-3 and M2-4 are merged.
* Captures are chunked: `segment_seconds = 600`, so an 11.5-minute window becomes 600 s + ~91 s segments. Calendar events therefore show the full window in the description.
* Reservation: `coordinator.reserve(..., priority=100, preempt=False)`. Any existing lease (any priority) makes `reserve()` raise → the recorder exits → systemd restarts it after 30 s. **A lease or open device held by anything else at pass time costs the recording.**
* `PassSchedule` ignores a station job's `enabled` flag (ISS job is `enabled: false` but OBS-R02 still records ISS passes ≥ 15°).
* Retention: **6 h** (`retention_hours`), applied when the next capture starts; min free 64 GiB. Nothing decodes automatically — the only METEOR image on disk (2026-10-11) was produced manually with SatDump.
* gain_db 0 → rtl_sdr automatic gain.

## SDR coordinator
File-locked JSON leases in `/run/observatory-sdr/leases.json` (tmpfs; cleared at reboot). Stale-lease cleanup by expiry and dead PID. No process is ever killed except Intercept via HTTP. SDR++ and dump1090 do not use it.

## ANTSW-01 (192.168.4.97)
ESP32-S3, firmware `antsw-web-2.1`, REST API: `GET /api/state`, `GET /api/history`, `GET /api/ports`, `POST /api/select` (X-Api-Token), `PUT/DELETE /api/ports`. Has its own lease field.
⚠ **It feeds OBS-R05 — the live ADS-B receiver** (currently port 7, TELE-WHIP; port 6 is labelled "ADS / Airband Antenna"). It does not feed OBS-R01 or OBS-R02. Automation is disabled; the code has no write path.
Ports: 1 V-DIPOLE, 2 SPRING-SHORT, 3 SPRING-TALL, 4–5 empty, 6 ADS, 7 TELE-WHIP, 8 MA-30-LOOP.

## Network
Tailscale up (tailnet `tail3eac32.ts.net`). **monkeybrain is online** (100.100.105.88, 19 ms). SSH is not yet trusted: host key unknown to observatory and observatory has no SSH keypair. Host key fingerprint seen: `SHA256:KZNxAy2YSvocFrYjNJ2ZgBj0zCdF0mXePBwkut9ArcY` (ED25519) — verify on monkeybrain before trusting.

## Tools present
rtl_sdr/rtl_fm/rtl_power (`/usr/local`, rtl-sdr-blog build, and `/usr/bin`), SatDump (+ UI running), SoX, FFmpeg, SDR++, multimon-ng, rtl_433, Dire Wolf, SoapySDR, dump1090. Python: skyfield 1.54, numpy 2.3, scipy 1.16 (system).
Earlier rf_observatory work found that rtl_power can hang permanently (PHASE2A_FAILURE_ANALYSIS.md). The discovery engine uses rtl_sdr + in-process FFT with process-group kills instead.

## External
* Google Cloud project `rf-observatory`: OAuth client (installed app) + service account `rf-observe@rf-observatory.iam.gserviceaccount.com`. Key works (token obtained). No "Satellite Observatory" calendar exists in the user's account.
* Neon PostgreSQL 18.6 reachable, empty.
