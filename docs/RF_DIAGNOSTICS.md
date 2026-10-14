# Advanced RF diagnostics

There are four features, reached from each receiver's **Test ▾** menu on the `/rf` page. All of them use
the same receivers, locking, recording path, signal-path snapshots, test history and analysis queue.

| Menu item | What it does | Test type |
|---|---|---|
| Quick Signal Test | 10–600 s recording at one frequency; power, noise, SNR, clipping, IQ | `quick` |
| Wideband RF Scan | Stepped sweep wider than the receiver's bandwidth; before/after; filter test | `scan`, `filter_test` |
| Investigate Interference | Anomaly report; antenna-vs-termination wizard; gain sweep | `interference`, `gain_sweep` |
| Satellite Quality Test | METEOR LRPT: evidence ladder with SatDump; existing recordings; pass comparison | `satellite`, `sat_compare` |
| Compare Previous Tests | Test history for that receiver; tick two tests to compare | — |

There are also two overview pages:
- **Testing** (toolbar): recent tests, saved baselines, A/B comparisons, detected anomalies, satellite results
  and component performance.
- **Performance** (a tab in each part's details, plus a full history page): what the test history says about
  that part.

## Hardware safety (all test types)

Every recording test is an ordinary broker job, so it gets:
- serial-number receiver selection and exclusive locking;
- conflict checks against every scheduled and legacy pass;
- the ANTSW-01 lease for OBS-R05, which pauses ADS-B for the test;
- the storage check against the estimated size;
- the IQ health check;
- a snapshot of the signal path when the capture starts.

On top of that:
- **Never displaces a pass.** `conflict_policy.on_automatic_job = "reject"` refuses the test instead of
  displacing an automatic pass. A test is also refused if any reservation on that receiver starts within
  2 minutes of the test's end.
- **Checks the receiver is free.** Before booking, the receiver must be on USB and not open in another
  program, not leased (for example to the bot's scanner), and, for the RSP1B, not being written by its
  recorder. If it can't run, the reason is shown and nothing is booked.
- **No hardware changes.** The bias tee keeps the receiver's fixed value: always on for OBS-R05, which has it
  permanently on, and off everywhere else. Nothing powers an LNA, switches a service off or reconfigures
  another receiver. The antenna-vs-termination wizard warns about externally powered and bias-tee-powered
  parts before you disconnect anything.
- **Cleanup.** Stepped captures close the receiver in a `finally` block, so it is released even after an
  error. A capture stops at the end of its reservation; any steps it did not reach are reported.
- **Quiet.** Test progress shows in the web UI, not Slack (`metadata.quiet`). Receiver health alerts still go
  to Slack.
- **Low-priority analysis.** All analysis runs in the existing queue at `nice 19` / `ionice idle`, and pauses
  while anything records.
- **Nothing is deleted automatically.** Raw IQ is kept under `/data/observatory/tests/`.

## Stepped captures (scans and gain sweeps)

These live in `src/aobs/scheduling/stepcapture.py`. The receiver is opened once and retuned in place:
- **RTL-SDR:** librtlsdr through ctypes (the same library `rtl_sdr` uses): `set_center_freq` /
  `set_tuner_gain`, `reset_buffer`.
- **SDRplay:** SoapySDR with the stream left running: `setFrequency` / `setGain`.

After each retune, a settle period of samples is read and thrown away (60 ms for the RTL-SDR, 80 ms for the
SDRplay scans, 200 ms in gain sweeps). The kept samples are appended to one raw file in the receiver's own
format. `<raw>.steps.json` records each step's offset, frequency (requested and tuned), gain (requested and
applied), start time and overflows. The job carries the steps in `spec.steps`, and every step is checked
like a single recording with those settings (`spec._check_steps`).

## Measurement methods

| Module | Measures |
|---|---|
| `testmetrics.py` | Quick/interference tests. dBFS; band and channel power ÷ Hann ENBW; noise floor = median of the mean spectrum; frequency-dependent floor = 20th percentile per bin; SNR only when the channel holds ≥ 1 dB more than the noise predicts; IQ statistics from a sampled raw pass |
| `scanmetrics.py` | Welch PSD per step (Hann, 50% overlap, dBFS/Hz); middle 75% of each step kept, DC notch dropped; steps on the FFT grid with half-span stride, so every bin is seen twice; linear averaging; running 30th-percentile floor over 1 MHz; carriers ≥ 10 dB over the floor with persistence and variation across sweeps |
| `scanmetrics.compare` / `filter_response` | B − A on a common grid. A change is measured only where the baseline had signal ≥ 6 dB above its own floor; regions are compared with everything outside them. Filter "attenuation" is over the air and comparative, never insertion loss |
| `interference.py` | Anomalies from a signal test, each with frequency, time, relative power, persistence, severity, confidence and possible explanations; follow-up suggestions name the measurement behind them |
| `gainsweep.py` | Per gain step: RMS, peak, clipping, Welch noise floor, channel power and its contrast over the noise; SDRplay IFGR/RFGR are labelled as gain reductions |
| `satquality.py` | Evidence ladder (RF energy → modulation possibly present → sync → frames → images); SatDump log parsing (demodulator SNR, Viterbi state/BER, deframer state, per-channel lines); two-receiver pass comparison by UTC alignment using relative contrast only |
| `componentperf.py` | Per-part history from snapshots, before/after pairs, filter tests and layout versions |

Each module's docstring holds its full methodology, and the UI and reports quote it.

### Measurement rules (enforced in code and wording)

- **dBFS, not dBm.** Nothing is labelled dBm.
- **Power and floor alone don't mean better reception.** More power is never "better" in itself, and a lower
  floor is never "better reception" without the signal.
- **No clipping isn't proof of no overload.** Analog overload is only ever "possible".
- **Peaks aren't automatically interference.**
- **No absolute dBFS across receivers.** Comparisons between receivers use contrast over each receiver's own
  noise floor.
- **No invented decoder numbers.** No SNR, decoder or sync figure is made up. Missing decoder metrics read
  "Not reported by decoder", and SNR reads "unavailable" with the reason.
- **A failed decode is never proof the satellite was absent.**
- **Verdicts need comparable evidence.** A/B verdicts need a ≥ 3 dB change with the same gain, at most
  30 minutes apart. Component claims need at least 3 comparable pairs that agree.

## SatDump integration

- **Version and pipelines** are detected (`satdump version`, the pipelines directory), not assumed.
- **Running it:**
  - argument lists only, never a shell;
  - the existing timeout, exit-code handling and per-attempt output directories, so an earlier result is never
    overwritten;
  - success is judged from CADU frames and real image content (this build exits 1 even after a good decode).
- **Channel extraction** (`dsp.extract`) streams the file and keeps phase and FIR filter state across chunks.
- **Experimental conjugated-IQ variant:** `iq_conjugate` mirrors the spectrum before extraction and negates the
  offset. It runs only when requested, and is labelled EXPERIMENTAL in every result.

## Data model

`rf_tests` holds one row per test:

| Column | Holds |
|---|---|
| `test_id` | the test's ID |
| `test_type` | the kind of test (table at the top) |
| `job_id` | the recording; null for analysis-only tests |
| `receiver_id` | the receiver |
| `component_id` | the part a filter test is about |
| `refs` | baseline/modified scans, attempt IDs, the recordings compared |
| `name`, `description`, `signal_type`, `tags` | labels |
| `baseline_test_id`, `group_id` | A/B links |
| `request` | the normalized settings |
| `rf_path` | the path when requested |
| `notes` | the owner's notes |
| `result` | the cached summary |
| `created_by`, `created_at`, `updated_at` | bookkeeping |

- **The authoritative path** for a recording is the job's `spec.resolved.rf_path`, taken at capture start.
  Changing the diagram later never rewrites it.
- **Raw IQ:** the recording's `raw_iq` artifact.
- **Plots and metrics:** artifacts of its analysis attempts.
- **Results files:** `rf_test.json`, `rf_scan.json` + `rf_scan.npz`, `rf_gain_sweep.json` and
  `sat_compare.json`.
- **Presets:** `rf_test_presets` holds saved test settings.

## API (cookie session from the bot's sign-in link, or the broker bearer token)

Write requests made with the cookie must also send the header `X-AOBS-RF: 1`.

**Tests, settings and history**

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/rf/tests/options/{rx}` | defaults, capabilities, path, availability, presets |
| GET | `/v1/rf/tests/availability/{rx}?duration_s=` | can a test run now, and if not, why |
| POST | `/v1/rf/tests` | start a test: `test_type` = quick, interference, scan, gain_sweep or satellite |
| POST | `/v1/rf/tests/scan-plan` | steps, resolution, estimated time and size, availability (books nothing) |
| GET | `/v1/rf/tests` | history: `receiver`, `tag`, `part`, `fmin_mhz`, `fmax_mhz`, `since`, `until`, `status`, `test_type`, `sort` |
| GET | `/v1/rf/tests/{id}` | detail: progress, metrics, files, satellite ladder or pass comparison |
| POST | `/v1/rf/tests/{id}` | notes, name, tags |
| POST | `/v1/rf/tests/{id}/cancel` | cancel; a running recording keeps its captured IQ as partial |
| GET | `/v1/rf/tests/{id}/files/{artifact}` | PNG or JSON belonging to that test |
| GET | `/v1/rf/tests/{id}/text/{artifact}` | log text |
| POST | `/v1/rf/tests/{id}/iq-link` | signed raw-IQ download link, valid 1 hour |
| GET | `/v1/rf/tests/{id}/report[?compare_with=]` | printable HTML report |
| GET, POST, DELETE | `/v1/rf/tests/presets` | test presets |

**Comparisons and filter tests**

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/rf/tests/compare?a=&b=` | signal-test A/B comparison |
| GET | `/v1/rf/tests/compare-scans?a=&b=[&regions=]` | before/after scan comparison |
| GET | `/v1/rf/tests/compare-termination?a=&b=` | antenna connected vs removed |
| POST | `/v1/rf/tests/filter-test` | `{component_id, baseline_test_id, modified_test_id, stopband_start, stopband_stop}` |

**Satellites**

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/rf/satellite/options` | SatDump version and pipelines, satellite profiles, recent recordings |
| POST | `/v1/rf/satellite/existing` | quality of an existing recording: `{recording_id, satellite, start_s, end_s, try_conjugate}` |
| POST | `/v1/rf/satellite/compare` | two recordings of one pass: `{recording_a, recording_b, satellite}` |

**Components and dashboard**

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/rf/components/{id}/performance` | a part's history and observations |
| GET | `/v1/rf/components/compare?a=&b=` | antenna vs antenna by frequency, with confounders |
| GET | `/v1/rf/dashboard` | the Testing page |

## Known limits

- **Not yet run on real hardware.** The step engine has run only against simulated receivers in the tests. The
  first real scan and gain sweep on each receiver type is its real check.
- **Busy detection.** RSP1B busy detection is weak, because the SDRplay service always holds the device; a
  clash shows up as a failed test. Receivers still run by their old recorders refuse tests while that recorder
  holds the device open.
- **SatDump metrics.** Only what SatDump 1.2.3 prints is available. Symbol timing and carrier recovery read
  "Not reported by decoder".
- **Reports** are printable HTML, not PDF.
