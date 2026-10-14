# Signal paths: the /rf editor

The page at `http://<broker>:8751/rf` shows every receiver, antenna, filter, amplifier and other part. You
drag parts to match how they are actually connected.

## Using it

- **Signing in:** ask `@observatory` in Slack for "the RF editor link". The link is valid for 15 minutes. After
  you open it, the browser stays signed in for 30 days through an HttpOnly cookie. The page only answers on the
  tailnet.
- **Dragging:** drag a part onto a receiver's path, between two parts, onto an ANTSW-01 port, or back to the
  parts bin. Mouse and touch both work. Antennas always go at the start of a path; dropping a new antenna there
  moves the old one to the bin.
- **Editing a part:** click it to set whether it is working (Working / Suspect / Broken / Unknown), its power
  (none, own supply, bias tee), model, gain, frequency range and notes. The editor also shows the last recording
  that went through that part. A part must be in the bin before you can delete it.
- **Saving:** Save creates a new version; nothing is ever overwritten. History lists the versions and can load
  an old one back into the editor; it becomes current only when you save it again.
- **Health:** each receiver's dot combines USB presence, the latest IQ health check, recent failures and the
  status you gave its parts. Receivers the broker doesn't run, such as OBS-R03 on the SatNOGS node, show only
  the status you set.

## What a save changes

- **Receivers:** the broker's receivers take their antenna, signal_path, amplifier and filters from the layout.
  That covers job validation, `/radio-status`, capabilities and the agent. `observatory.toml` is only where the
  very first version came from.
- **Switch ports:** the ANTSW-01 port map is updated for both the scheduler and the lease API.
- **The switch board:** when a save changes ANTSW-01's port names, or the board does not match the saved layout, the
  broker sends them to the board
  (antswitch.local, `PUT /api/ports` with the switch API token). Each port's hardware code, the switch ID and the
  receiver are read from the board and sent back unchanged, so a push can never rewire which code selects which
  port. The broker reads the board back to confirm it took, and checks the selected port did not move. Switching
  itself always goes by port number, so names never decide which RF port is used. Starlab is authoritative: the
  board's names are never copied back into the layout.
  - **Limits:** the board stores antenna names of up to 16 characters using only letters, digits, `_ . -` (firmware
    2.1 refuses the whole map with HTTP 400 otherwise), and labels of up to 48. Each run of other characters is sent
    as one `-` ("ADS Airband" is stored as `ADS-Airband`) and the two count as the same antenna. A switch-port
    antenna whose ID is too long, or that would collide with another port's board name, is refused when you save.
    Longer labels are shortened to 48 characters and the result says so.
  - **Unreachable board:** the save still happens, the result says the board was NOT updated, and the switch box
    offers "Send these names to the switch" to retry.
  - **Startup:** the broker never pushes on its own. The switch box only shows whether the board matches.
- **Upcoming jobs:** future scheduled, shadow and legacy rows on the affected receivers get the new path, so the
  calendar matches. An OBS-R05 job follows its antenna to the antenna's new port. If the antenna leaves the
  switch, the save result and a Slack notice tell you to reschedule the job.
- **Slack:** each save posts a one-line summary in Slack.

These rules are enforced:
- One physical part can be in only one place at a time.
- ANTSW-01 must stay at the start of OBS-R05's path, because the broker drives that receiver through the switch.
- Broker receivers can't be removed.

You get a warning, not a refusal, for:
- a part marked broken or suspect;
- a path with no antenna;
- a bias-tee-powered part on a receiver that can't supply DC;
- a permanently-on bias tee feeding an amplifier that has its own supply.

## Recordings

Each recording's path is stored once in `spec.resolved.rf_path` and never changed afterwards. It holds the
layout version, the parts with their status at that time, and the path text.

- **Broker recordings:** the layout as it is when the capture starts. The switch port and the antenna on it are
  included.
- **Legacy-recorder files:** the layout version that was in effect at the recording's start time.

The path also goes into the recording's metadata sidecar (`rf_path`) and into `/radio-results` as "Signal path".
Recordings made before the first layout version have no path; nothing is filled in after the fact.

## Signal Path Test Lab

To start a test, click **Test** on any receiver the broker runs (OBS-R01, OBS-R02, OBS-R05 and the RSP1B; not
OBS-R03). The panel fills in what the system already knows:
- the receiver, its hardware and serial;
- the current path drawn top to bottom, with the switch port for OBS-R05;
- the bias tee state;
- default settings: your last test on that receiver, or else its automatic-pass settings.

You choose the frequency (MHz or Hz), sample rate (only ones the receiver supports) and gain:
- **RTL-SDR:** tuner gain steps, or auto.
- **RSP1B:** IFGR and RFGR (the LNA state), plus AGC.

Then you choose:
- **Duration:** 10, 30, 60, 120 s, or a custom length up to 600 s.
- **Bandwidth:** the RSP1B's IF filter (RTL-SDRs have none).
- **Signal type:** it sets the width of the measured channel.
- **Labels:** a name, description and tags.
- **Presets:** save the current settings as one, and reapply them later.

### How a test runs

A test is an ordinary broker recording, so it gets the same locking, conflict checks, storage checks, IQ health
check and signal-path snapshot as every other recording. On top of that:
- **It never displaces anything.** A test never pushes an automatic or legacy satellite pass aside
  (`conflict_policy.on_automatic_job = "reject"`). Its job is refused if any reservation on that receiver starts
  within 2 minutes of the test's end.
- **It checks the receiver is really free first.** It won't start if the receiver is open in another program,
  leased (for example to the bot's scanner), being written by the RSP1B recorder, or reserved. The panel says why
  and lets you check again.
- **It starts quickly.** A test starts about 25 s after you click (`test_min_lead_seconds`, default 20, plus 5).
  Ordinary manual jobs need 2 minutes.
- **It stays out of Slack.** Its status updates show only in the panel. Receiver health alerts still go to Slack.
- **It keeps the DC spike out of the channel.** The receiver tunes 1/8 of the sample rate below the target unless
  you set the center yourself.
- **Hardware stays as configured.** The bias tee keeps its configured state: always on for OBS-R05, which has it
  permanently on, and off everywhere else. Nothing else about the hardware is changed.
- **Stopping:** cancelling a running test keeps the IQ captured so far, marked partial.
- **Files:** raw IQ is kept under `/data/observatory/tests/`. Nothing is deleted automatically.

### What it measures

The `rf_test` analysis task (`src/aobs/scheduling/testmetrics.py`) uses fixed settings: FFT 2048, Hann window,
600 rows of 8 averages, detection threshold 8 dB. It reads the file in chunks. All levels are dBFS (uncalibrated;
never dBm). Band and channel powers are divided by the window's ENBW (equivalent noise bandwidth).

- **Power:** mean, peak, band and target-channel power.
- **Noise floor:** the median across frequency, per bin and per Hz; its spread across frequency and over time;
  and the frequency-dependent floor.
- **SNR:** reported only when the channel holds at least 1 dB more power than the noise floor predicts. Otherwise
  it reads "SNR unavailable" with the reason.
- **Detected peaks:** each with frequency, power over the floor, bandwidth, how long it was present and when.
  Peaks are classed as persistent, intermittent or transient, and strong narrow carriers are flagged. Possible
  intermodulation (2·f1 − f2) is shown only as a hypothesis.
- **Time:** short bursts.
- **IQ:** clipping, peak sample, RMS, ADC codes used, DC offset, I/Q imbalance and correlation, histograms, and a
  raw scatter (not a constellation).
- **Spectral shape:** flatness, peak-to-average, asymmetry.

### What you see

- **Plots:** spectrum, waterfall, power over time, noise floor, interference map and IQ diagnostics. They are
  drawn in the browser from the stored data, so you can zoom, read values by hovering, adjust the waterfall's
  color range and save PNGs. The server's PNGs are under Images.
- **Downloads:** Download IQ uses a signed link that expires after 1 hour. The printable HTML report includes the
  plots and methodology, and the comparison when there is one.

### Comparing tests

- **Baselines.** "Compare against" a previous test copies its settings. When you save a diagram change, the page
  offers to test the receivers that changed against their last test.
- **The comparison shows:**
  - both paths side by side;
  - what changed: the parts, any differing settings and the time between the tests;
  - overlaid spectra and a B − A difference spectrum, in dBFS/Hz so different RBWs line up;
  - side-by-side waterfalls on one color scale;
  - a metrics table;
  - a report split into Measured, Interpretation and Not confirmed.
- **Verdicts.** It only says a configuration "suggests" better or worse when the target's SNR (or its power over
  the noise) changed by at least 3 dB. The gain must also be the same, and the tests at most 30 minutes apart.
  Otherwise it says inconclusive. It warns about differing gain, receivers, sample rates or frequencies, long gaps
  between tests, variable band power, and satellite signals. It never claims a change in RF sensitivity.

### History

Test history filters by receiver, part, tag, frequency, date and result, and sorts by date, SNR, noise floor,
channel power, clipping or strong peaks. Tick two tests to compare them. A part's details list the tests that used
it, and every comparison where it was added or removed.

### Where it's stored

- `rf_tests`: what each test was for (request, tags, baseline, comparison group, the path when you requested it,
  notes, cached summary).
- The recording (`sched_jobs`): the exact radio settings, and the path as it was at capture start.
- The artifact index: IQ, metrics JSON and plots.

Changing the diagram later never rewrites a past test.
