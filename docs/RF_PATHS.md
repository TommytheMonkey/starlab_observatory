# Inventory and signal paths: the /rf editor

The page at `http://<broker>:8751/rf` shows every receiver, antenna, filter, amplifier and other part. You
drag parts to match how they are actually connected.

Every connection on the page is one you made (or, for the very first version, one written in
`observatory.toml`). Starlab never guesses how hardware is wired: a receiver found by discovery starts with an
empty path, and nothing here can detect an antenna, filter or amplifier.

## Using it

- **Signing in:** ask `@observatory` in Slack for "the RF editor link". The link is valid for 15 minutes. After
  you open it, the browser stays signed in for 30 days through an HttpOnly cookie. The page only answers on the
  tailnet.
- **Dragging:** drag a part onto a receiver's path, between two parts, onto an ANTSW-01 port, or back to the
  parts bin. Mouse and touch both work. Antennas always go at the start of a path; dropping a new antenna there
  moves the old one to the bin.
- **Editing a part:** click it. The editor also shows the last recording that went through that part. A part
  must be in the bin before you can delete it. See [Inventory records](#inventory-records) for the fields.
- **Saving:** Save creates a new version; nothing is ever overwritten. Cancel (click twice) drops every unsaved
  change. Unsaved changes are also kept in the browser until you save or cancel. History lists the versions and
  can load an old one back into the editor; it becomes current only when you save it again.
- **Shelving:** the – button on a receiver hides its lane on this page, in this browser only. It changes nothing
  else.
- **Health:** each receiver's dot combines USB presence, the latest IQ health check, recent failures and the
  status you gave its parts. Receivers the broker doesn't run, such as OBS-R03 on the SatNOGS node, show only
  the status you set.

## Inventory records

Every part has a name, kind, manufacturer, model, description, notes, status (Working / Suspect / Broken /
Unknown), power (none, own supply, bias tee) and a frequency range. What the range means depends on the kind.

| Kind | Extra fields |
|---|---|
| Antenna | type (dipole, QFH, discone, …), polarization (RHCP, LHCP, vertical, horizontal, linear, dual), mounting location, orientation, mount (fixed or rotatable), the rotator that turns it. The range is where it works. |
| Filter | type: bandpass (range = passband), notch (range = stop band), low-pass (upper = cutoff), high-pass (lower = cutoff), or not specified. Insertion loss. |
| Amplifier / LNA | gain; the range is its rated range. |
| Splitter | number of outputs (2–16). |
| Switch | ANTSW-01 is driven by the broker. Any other switch is manual: 2–16 ports, and the port it is set to now. |
| Attenuator, cable, connector / adapter | loss; cable length. |
| Rotator | none. It is not in any path; an antenna names it. Starlab does not control rotators. |

For parts in a path, "Bias-tee DC" says whether the part passes DC (unknown by default). For parts with their own
supply, "Its power supply" says whether that supply is connected.

**Receivers.** A receiver Starlab runs shows what its driver supports:
- hardware and identity (serial, or USB port when there is no usable serial), host and friendly name;
- tuning range, sample rates, gain controls and recording formats. These come from the driver tables for
  RTL-SDR and SDRplay, or from the SoapySDR probe for the others;
- bias tee: switchable, permanently on or off, or not available;
- its automatic-recording settings.

Name, notes and enabling stay in **Receivers**. In this editor you add:
- **Gain profiles:** named gain settings, checked against the driver (RTL-SDR gain steps, SDRplay IFGR/RFGR, the
  gain elements a SoapySDR driver reported). They are stored with the layout. Recordings and tests do not use
  them yet.
- **Fed from:** see shared antennas below.

A documentation-only receiver (one Starlab never opens) records the same things by hand: name, serial, host,
hardware, tuning range, sample rates, formats and bias tee.

**Shared antennas (splitters).** Put a splitter in one receiver's path. Then open another receiver and choose
"Fed from: an output of <splitter>". That receiver's own path holds only the parts after its splitter output. Its
lane shows the shared parts faded, and its recorded path is the whole thing, for example
`Discone > LaNA > 2-way splitter > OBS-R01`. Splitters can be chained.

**Manual switches.** Drop a switch at the start of any receiver's path and put antennas on its ports. Click the
circle next to a port to mark which one the switch is set to now. Recordings note that antenna, so update it
whenever you turn the switch.

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

These rules are enforced (the path is impossible, so Save is refused):
- One physical part can be in only one place at a time. To feed two receivers from one antenna, use a splitter.
- A splitter can't feed more receivers than it has outputs (the receiver whose path holds it uses one). A
  receiver can only be fed from a splitter that is in a path, never from one in its own path, and feeds can't loop.
- A receiver fed from a splitter gets its antenna through it, so its own path can't start with an antenna or
  switch.
- ANTSW-01 must stay at the start of the path of the receiver it feeds, because the broker drives that receiver
  through the switch. It can't feed any other receiver, not even through a splitter: the broker changes its port for
  that receiver's jobs. Which receiver that is: an observatory.toml receiver with `antenna = "ANTSW-01"`, otherwise
  "Receiver the antenna switch feeds" on the Settings page (`[antenna_switch] receiver`), by receiver ID or by serial,
  so a receiver added in the web UI can sit behind the switch. It applies at once; the broker, the pirate watch and
  scheduled jobs lease that receiver by its serial. (The switch board's address needs a restart.)
- Rotators are not in paths, and an antenna's rotator must be a rotator in the inventory.
- Broker receivers can't be removed.

You get a warning, not a refusal, for:
- a part marked broken or suspect;
- a path with no antenna, or a manual switch with no port selected (or set to an empty port);
- **frequencies**:
  - an antenna and its receiver with no frequencies in common;
  - a bandpass filter whose passband misses the antenna's range;
  - a receiver's automatic satellite recordings that fall outside its antenna's range or an amplifier's rated
    range, outside a bandpass filter, or inside a notch filter;
- **automatic-recording settings** the receiver does not support, such as the sample rate or gain;
- **power**:
  - a bias-tee-powered part on a receiver that can't supply DC, or whose bias tee is permanently off;
  - a bias-tee-powered part with a DC-blocking part between it and the receiver;
  - a part with its own supply that you marked as not connected;
  - a permanently-on bias tee feeding an amplifier that has its own supply;
  - two receivers with permanently-on bias tees on one splitter;
- gain profiles the driver can't use.

Recording jobs get the same frequency check for the path they will use (for OBS-R05, the antenna the job picks on
ANTSW-01). It appears among the job's warnings and never refuses the job.

## Recordings

Each recording's path is stored once in `spec.resolved.rf_path` and never changed afterwards. It holds the
layout version, the parts with their status at that time, and the path text. A path through a splitter includes
the shared parts, marked `shared`. A manual switch adds the antenna on its selected port.

- **Broker recordings:** the layout as it is when the capture starts. The switch port and the antenna on it are
  included.
- **Legacy-recorder files:** the layout version that was in effect at the recording's start time.

The path also goes into the recording's metadata sidecar (`rf_path`) and into `/radio-results` as "Signal path".
Recordings made before the first layout version have no path; nothing is filled in after the fact.

## Bias tee

Starlab switches the bias tee itself on receivers that support it:
- **RTL-SDR Blog V3/V4** (such as OBS-R07), recognised from their USB strings;
- **SDRplay RSP1A/RSP1B/RSP2/RSPdx/RSPduo** (not the original RSP1), recognised from the model;
- **Airspy R2/Mini** (such as RX-02), when the driver probe (Receivers → Identify) reports SoapyAirspy's `biastee`
  setting (4.5 V);
- any receiver with `bias_tee_supported = true` and no `bias_tee_fixed` in observatory.toml.

All of these must be plugged into this computer, not a remote host. For those receivers the saved RF layout decides:

- **On** when any part in the receiver's path has power "Bias tee".
- **Off** when no part does. Bias-tee DC into a bare antenna (a QFH, a dipole) is a DC short.
- A test recorded with a 50-Ω termination or an open input keeps it off.

What the DC meets on the way is shown as a warning in the path editor. It never quietly changes the decision:
- a part marked "Blocks DC" between the receiver and the powered part: that part gets no power;
- DC-sensitive parts the DC reaches: an antenna switch, or a filter or splitter that isn't marked "Blocks DC".

Where it is applied, per receiver type. The recorder's run file carries `bias_tee_control` and the value:
- **RTL-SDR** (`capture.capture_rtl`): rtl_sdr has no per-serial bias-tee option. The recorder switches the tee
  through librtlsdr (`rtlsdr_set_bias_tee`, `stepcapture.set_rtl_bias_tee`) on the one connected RTL-SDR with that
  serial, and refuses if the serial is missing or shared. Then it starts rtl_sdr; the setting outlasts the close.
  When rtl_sdr exits it switches it off again (5 tries). librtlsdr cannot read the state back.
- **SDRplay** (`capture.capture_sdrplay`): `writeSetting("biasT_ctrl")` and `readSetting`. A readback mismatch
  refuses the recording. The tee is switched off afterwards, also when setup fails.
- **Airspy** (`capture.capture_soapy`): `writeSetting("biastee")` and `readSetting`, which SoapyAirspy turns into
  `airspy_set_rf_bias`. It's set before anything else. A readback mismatch refuses the recording. It's switched
  off afterwards, however the recording ended.
- **Scans and gain sweeps** (RTL-SDR, SDRplay) hold it for the whole sweep and switch it off as the receiver
  closes.
- SatDump never opens a receiver here; it only decodes recorded files, so the bias tee doesn't apply to it.
  SDR++ listening sessions leave the bias tee to SDR++'s own source settings.

Booking fills the job's `radio.bias_tee` from the layout (listed in `resolved.defaults_applied`). At capture start
the runner decides again from the layout as it is then, so a path changed after booking wins. A difference from
the booking is a warning in the job's diagnostics.

**As applied:** each recording result has `bias_tee` with `requested`, `applied` (true, false, or null when the
switch failed) and `method`. The metadata sidecar's `bias_tee` is `on`/`off`/`unknown` as applied (plus
`bias_tee_requested`). The recording page shows "Bias tee … (as applied)". Every waterfall and spectrum footer
says `bias tee on/off` next to the gain.

**Overload warning:** when a receiver's gain is mid-to-high and a powered LNA sits in front, the path editor (on
gain profiles) and the test form show a warning. That means about 18 dB or more summed over Airspy LNA/MIX/VGA
(e.g. 8/8/8), or 33 dB or more on an RTL-SDR. Starlab never changes the gain itself.

### Default gain

A receiver's gain profile can be ticked **Default** (RF editor → receiver → Gain profiles; one per receiver). Every
recording on that receiver then starts from it:
- manual bookings from the web;
- API or Slack jobs that give no gain;
- rule recordings without a named gain profile, including a move to a backup receiver;
- a change of receiver on a booked job;
- the Signal Path Test form, which otherwise copies the last test's gain.

Without a default, Airspy and other SoapySDR receivers start at the middle of each gain element's range (8/8/8 on
an Airspy R2). A profile that doesn't fit the driver is ignored, with a warning. Bookings made earlier keep the
gain they were booked with.

### Bias-tee A/B test

In the Signal Path Test form, a receiver whose bias tee Starlab switches and whose path has a "Bias tee" part
gets a **Bias tee on/off** checkbox. It books the same Quick Signal Test twice, back to back: bias tee OFF, then
ON, with the same frequency, sample rate, gain and duration. Then it reports:
- the noise-floor change (dBFS/Hz, ON minus OFF);
- the ADC spread change (the larger of the I and Q standard deviations, in LSB);
- clipping with the LNA on;
- the bias tee as applied in each run.

A powered LNA should lift the floor by roughly 20 dB or more. Under about 3 dB the verdict is "LNA not powered or
not working". It refuses a path with nothing marked "Bias tee", because the ON half would only put DC on the
antenna. Cancelling it stops both halves.

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
- **The bias tee follows the signal path.** On a receiver whose bias tee Starlab switches, the test records with it
  on when the path calls for it, and off otherwise (see [Bias tee](#bias-tee)). The panel shows which, and why. A
  hard-wired one keeps its state (always on for OBS-R05). Nothing else about the hardware is changed.
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
