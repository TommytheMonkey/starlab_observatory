# RF discovery engine — design (Phase 2)

## The coordination problem as it really is
* The recorder reserves with `preempt=False` and **crashes if anything holds a lease**. It also fails if the USB device is open (rtl_sdr cannot claim it).
* SDR++ server opens a receiver whenever the Mac client presses play, with no lease. It holds OBS-R02 right now.
* dump1090 (root) selects by index.
So priority cannot be "enforced" by lease priority numbers. Protection has to come from the lower-priority party **leaving early**, verified by observation.

## Pass guard (core safety component)
Shared by every discovery worker:
```
guard_window(receiver) = [capture_start − pre_release, capture_end + post_hold]   # pre_release = 180 s, post_hold = 60 s
```
* Source: the satops DB, populated from the recorder's own predictor. OBS-R02 ISS passes are added the same way as OBS-R01 METEOR passes (`forecast.receiver` becomes a list).
* **Fail closed**: if the last successful forecast for that receiver is older than 30 min, or the DB is unreadable, the guard says "release now" and stops granting.
* A scan is only started if `now + max_chunk_duration + pre_release < next guard window`. Chunks are short (≤ 10 s IQ), so a scan never runs into a pass.
* An independent watchdog thread (separate from the scan loop) re-evaluates the guard every 5 s and SIGKILLs the scan's process group if the deadline is reached. A hung rtl_sdr is the documented rtl_power failure mode.
* After release, the guard verifies that the USB node has no holder from our UID and that the lease is gone, and logs `released_for_pass`. If it cannot verify this, a CRITICAL notification goes out.

## Receiver broker (priority: satellite > manual SDR++ > discovery)
* Discovery takes a coordinator lease (`owner=aobs:discovery:<rx>`, priority 10) with a TTL of `min(300 s, time to guard window)` and renews it every chunk. If we crash, the lease expires quickly and the PID check also frees it.
* **Manual SDR++ use wins over discovery**: before each chunk the broker checks the receiver's USB node for any holder that isn't ours. If sdrpp (or anything else) holds it, discovery backs off for 10 min and records a `manual_use` gap.
* Manual override: `aobs discovery pause <rx> [--for 2h]` writes a pause row and is honoured before the next chunk. `aobs discovery release-all` stops everything immediately.
* SDR++ protection for satellite passes (advisory, Phase 4): notify when sdrpp holds a pass receiver less than 30 min before a pass. Optionally run SDR++ server under a systemd unit with an `ExecCondition` gate. This needs your approval because it changes how SDR++ is run.
* Selection is always by serial (`rtl_sdr -d OBS-R02`), never by index.

## Scan pipeline (per chunk)
1. `rtl_sdr -d <serial> -f fc -s 2.4e6 -n N` into a RAM ring buffer (in its own session/process group, niceness 10).
2. Welch PSD (numpy) → per-bin noise floor from a rolling median of historical scans for that (receiver, frequency) cell → adaptive threshold floor + k·MAD.
3. Peak grouping → center frequency, −6 dB bandwidth, SNR, duty cycle across chunks.
4. Cluster match (±bandwidth/2) → `signal_clusters` (first_seen, last_seen, count) plus `signal_observations`.
5. Interest score = novelty + SNR + not in the known list + not excluded. Above threshold: save the pre-trigger ring buffer plus a post-trigger cu8 capture with JSON metadata, insert into `recordings` (kind=discovery), and enqueue an `analysis_jobs` row.
6. Next step, or yield to the guard.

## Scan profiles (TOML, validated against receiver and antenna ranges)
AVIATION 118–137 MHz (AM, 25/8.33 kHz) · AMATEUR_VHF 144–148 · AMATEUR_UHF 420–450 · ISM_433 433.05–434.79 · ISM_900 902–928 · SATELLITE_VHF 137–138, 145.8 · SATELLITE_UHF 435–438 · GENERAL_SURVEY.
Every profile declares dwell, step, receiver, antenna, and the allowed band. The loader rejects profiles outside the antenna's or tuner's range. OBS-R02 is now Discone > externally powered Nooelec LaNA > OBS-R02 (owner, 2026-10-13; the HAM/144 MHz filter was removed, so the whole LaNA/discone range is usable). The legacy label in /etc/observatory-sdr still says "Discone, LaNA, 144 MHz filter"; config/observatory.toml is authoritative.

## Storage
Discovery IQ quota (default 50 GB) and age-based retention. Only files we created (tracked in `recordings` with owner=aobs) are ever deleted. The guard refuses to write if `/data` free < recorder floor (64 GiB) + 100 GiB headroom.

## Analysis (Phase 3)
Durable queue in the local DB (`analysis_jobs` with lease/expiry/attempts/not_before). The monkeybrain worker pulls over Tailscale through a small authenticated FastAPI endpoint on observatory, bound to the tailnet IP with a bearer token. It downloads the IQ, classifies it (DSP features first: spectral flatness, envelope variance, instantaneous-frequency histogram, cyclostationary features; ML only where it improves on DSP), runs decoder plugins, and posts results back. Monkeybrain being offline only delays jobs. IQ stays on observatory until the result is stored.
Decoder plugin contract: `name, version, signal_types, sample_format, sample_rate_range, protocols, resources, output_kind, run(iq_path, meta) -> Result`. `decoder_attempts` has a unique key on (recording, decoder, version), so known-incompatible pairs are not retried. Evidence levels are DETECTED → CLASSIFIED → PROBABLE MATCH → VERIFIED DECODE. VERIFIED requires a decoder CRC/FEC pass.
