# Slack command reference

Everything refers to a **recording id**: the id of the job that made the recording.

| Prefix | Origin |
|---|---|
| `m…` | manual job |
| `a…` | broker automatic pass |
| `l…` | window recorded by a legacy service |
| `r…` | imported historical file |

Replies go to your DM with the bot. `/radio-help [command]` shows the same text in Slack.

| Command | Tier | Purpose |
|---|---|---|
| `/radio-status [receivers\|jobs\|processing\|storage\|services\|health] [receiver=ID]` | read | live state of everything |
| `/radio-health` | read | functional checks: PASS / WARNING / FAIL / UNKNOWN, with actions |
| `/radio-capabilities [receiver]` | read | hardware limits and reservations (+ JSON for ChatGPT) |
| `/radio-recordings [latest\|today\|yesterday\|failed\|partial\|DATE] [filters]` | read | find recordings (paginated) |
| `/radio-recordings import [apply]` | operate | index historical IQ files (dry run unless `apply`) |
| `/radio-results <rec> [type=…] [attempt=…] [link=<artifact>]` | read | attempts, artifacts, images, download links |
| `/radio-jobs [today\|tomorrow\|active\|queued\|failed\|preempted] [receiver=]` | read | unified schedule |
| `/radio-jobs <job_id>` | read | exact settings (JSON) and history |
| `/radio-jobs clone=<job_id> start=ISO duration=S [receiver=] [book]` | operate | copy settings to a new window |
| `/radio-schedule [dry-run] {"jobs": […]}` | operate | book recordings atomically |
| `/radio-decode recording=<id> [pipeline=] [target=137.9\|offset=Hz] [output_rate=] [shift_method=] [start= end=] [flags]` | operate | SatDump decode attempt |
| `/radio-analyze recording=<id> [analysis=a,b] [fft= window= overlap= frequency= bandwidth= start= end= dynamic_range= colormap=]` | operate | RF analysis attempt |
| `/radio-analyze compare=<id1>,<id2>[,…]` | operate | comparison attempt |
| `/radio-waterfall recording=<id> [fft= frequency= bandwidth= …]` | operate | waterfall only (same engine) |
| `/radio-reprocess {"recording_id": …, "operation": "satdump_decode"\|"analyze", "parameters": {…}}` | operate | new attempt with other settings |
| `/radio-cancel <job_id> [confirm=code]` | disrupt | cancel; a running recording needs the code it returns |
| `/radio-cancel processing=<attempt_id>` | disrupt | stop a decode/analysis (source kept) |
| `/radio-cancel batch receiver= start= end= [confirm=code]` | disrupt | preview, then cancel exactly the previewed jobs |

## Filters for `/radio-recordings`

`receiver=`, `satellite=`, `frequency=137.9` (a recording whose band contains it), `date=YYYY-MM-DD`
(local), `since=`/`until=` (ISO), `format=cu8|cs16|cf32`, `status=`, `decoded=true|false`,
`analyzed=true|false`, `min_size_gb=`, `experiment=`, `limit=` (10 by default, 50 at most), `page=`.

## Decoding

These defaults come from the recording itself: center, sample rate, format, target frequency,
and the pipeline (from the request, or `meteor_m2-x_lrpt` when the target is a METEOR satellite).

`frequency_offset_hz = target − center`. With a non-zero offset, `shift_method=internal` (the
default) streams the recording through a phase-continuous mixer and a Kaiser-windowed FIR,
decimating by an integer factor. The default output is the largest decimation that still gives
at least 500 kS/s. SatDump then gets a cf32 baseband centered on the target, with no
`--freq_shift`. The channel must fit within 90% of the recorded half-band.

- `shift_method=satdump` passes `--freq_shift` instead. Its sign has not been verified on a real decode.
- `shift_method=none` is refused when the target is off-center.
- SatDump always runs with `--disable_multi_modules`. Without it, this SatDump 1.2.2 build skips the
  demodulator and nothing can sync.

An attempt is `decoded: true` only when SatDump exits 0 **and** at least one image has real
content (pixel standard deviation > 2). The demodulator SNR, sync reports and per-image checks are
stored with the attempt.

## Analysis

| Task | Output |
|---|---|
| `waterfall` | accurate axes; FFT size/window/overlap, frequency and time range, dynamic range, colormap, size |
| `spectrum` | mean + peak hold, noise floor (dBFS/bin and dBFS/Hz), peak, SNR, occupied bandwidth |
| `signal_detection` | candidate signals with frequency, start/end, bandwidth, dB over floor, confidence; never identified as a satellite |
| `frequency_offset` | measured peak frequency over time, drift Hz/s, excursion; labelled measured, not a Doppler prediction |
| `clipping` / `signal_quality` | ADC clipping, DC spike and IQ imbalance (measured); excessive gain, broadband overload and spur/IMD comb (suspected); strong interference |
| `integrity` | size vs expected, whole samples, duration, driver overflows, metadata consistency |
| `checksum` | SHA-256 of the raw file |

`/radio-analyze compare=` normalizes every recording to its own noise floor. It warns when the
recordings differ in time, receiver (uncalibrated gains) or sample rate. All levels are relative,
not calibrated received power.

## Authorization

The bot reads `~/.config/observatory/slack.env` on monkeybrain:

| Variable | Who it covers |
|---|---|
| `SLACK_OWNER_ID` | the owner; allowed everything |
| `SLACK_OPERATORS` | comma-separated Slack user ids allowed read + operate + disrupt |
| `SLACK_VIEWERS` | comma-separated ids allowed read only |

Identity is Slack's authenticated `user_id` on the request, never anything inside the command
text. The app uses Socket Mode, so requests arrive over its authenticated websocket and there is
no HTTP request signature to check. Disruptive actions need a confirmation code. Parameters are
allowlisted: pipelines must be installed, paths come from the artifact index, and options are
restricted to safe names and values. No Slack text ever reaches a shell.

## Notifications

Notices go to your DM, **one thread per recording/job**. Kinds:

| Kind | When |
|---|---|
| `scheduled` | a job was booked |
| `recording` | a manual job started recording |
| `completed`, `failed`, `partial`, `missed` | a job ended that way |
| `preempted`, `skipped` | an automatic pass gave way to a manual job |
| `attempt_completed`, `attempt_failed` | a decode or analysis finished |
| `health` | a check entered or left FAIL |
| `calendar` | Google Calendar sync failed or recovered |

To silence a kind, list it in `[scheduler] notifications_disabled`.
