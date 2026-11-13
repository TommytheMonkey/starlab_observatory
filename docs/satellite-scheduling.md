# Satellite scheduling

This guide covers predicting satellite passes, booking recordings by hand, and turning on automatic recording with
rules. It also explains why **automatic recording stays off until you enable it**, and what Starlab refuses to book.

Everything is on the **Scheduler** page (header button **Scheduler**). It has five tabs: **Calendar**, **Upcoming
passes**, **Automatic rules**, **New recording** and **Recording history**. Times are shown in the observatory's
display time zone and stored in UTC.

## Before you start

| Needed | Where | Without it |
|---|---|---|
| Observatory latitude and longitude | Settings → Observatory | No passes are predicted; the Scheduler says so. |
| Satellites chosen | Settings → Satellites | No passes are listed. |
| Orbital elements | shipped sample, or downloads (below) | A satellite without elements has no predicted passes. |
| A verified receiver (or a simulated one) | Receivers → Verify | Bookings on that receiver are refused. |

## Satellites and orbital elements

Starlab predicts passes itself, offline, from orbital elements. It knows these satellites, including their downlink
frequencies:

| Satellite | NORAD | Downlink | Signal |
|---|---|---|---|
| METEOR-M2 3 | 57166 | 137.900 MHz | LRPT weather images (decodable with SatDump) |
| METEOR-M2 4 | 59051 | 137.900 MHz | LRPT weather images (decodable with SatDump) |
| ISS | 25544 | 145.800 MHz | FM voice and SSTV events |

You can also enter other NORAD catalogue numbers, in Settings → Satellites or in a rule. Starlab predicts their
passes but doesn't know their downlink, so give a rule a frequency, or book them from **New recording**.
**Upcoming passes** refuses to book them ("Starlab does not know the downlink of NORAD …; book it manually").

**Settings → Satellites:**

| Setting | Default | Meaning |
|---|---|---|
| Satellites (NORAD IDs) | METEOR-M2 3 and 4 | What to predict. |
| Ignore passes peaking below (°) | 20 | Hides low passes. |
| Download current orbital elements | off | Off: the **sample elements shipped with Starlab** are used. They need no network, but pass times drift by minutes after a few weeks. |
| Orbital-element source | CelesTrak OMM | `celestrak_omm` (OMM JSON, falling back to TLE), `celestrak_tle`, or `custom` (an address containing `{norad}`, in TLE or OMM format). |
| Refresh automatically every (hours) | 12 | Only while downloading is on. Every refresh recalculates future passes. |
| Warn when elements are older than (days) | 14 | Stale-element warning threshold. |

Downloaded elements are cached under `<data>/tle/`. A failed download keeps the cached elements. With none at all,
the shipped sample elements are used and labelled as such.

**Warnings** appear at the top of the Scheduler and in each rule's planning result:

- *"elements are N days old (limit 14); pass times may be off by minutes. Refresh them when online."*
- *"no orbital elements for this satellite: its passes cannot be predicted"*. Its existing jobs are left alone.
- *"The observatory location is not set"*.

To refresh now: Settings → Satellites, turn on downloading, save, then refresh. Refreshing is refused while
downloading is off.

## Book a recording by hand

### A predicted pass

1. **Scheduler → Upcoming passes** lists the next 72 hours.
2. Press **Schedule recording** on a pass. The **New recording** form opens, pre-filled with the satellite's
   frequency and the pass times.
3. Tick the receivers to record on (one, several, or **All**), check the settings, and press **Book**.

### The same recording on several receivers

Tick more than one receiver and Starlab books one recording per receiver, with the same frequency and times. Use
it to record a pass on every receiver and compare them side by side in **Recordings**.

- **All or nothing:** if any ticked receiver can't take it (busy, not verified, unplugged, can't do the settings),
  nothing is booked, and the message names the receiver and the reason. Untick it and book again.
- **Each receiver keeps its own defaults** for the recording format and gain. A gain set in the form applies to
  the RTL-SDRs only. A gain profile and a specific format can be chosen only when a single receiver is ticked.
- **Sample rate:** leave it empty to use each receiver's default. A rate you type must suit every ticked receiver.
- **The antenna switch:** if a switched receiver is ticked, the antenna (switch port) applies to it.
- **Check** shows the combined size of all the recordings.
- **All** ticks every verified (or simulated) receiver that isn't recorded by another service.

### Anything else

**New recording** books any frequency at any time (in local time). Starlab validates the settings against the
receiver's real limits (frequency range, legal sample rates, gain steps, bias tee). It **rejects** anything the
receiver can't do instead of silently adjusting it.

### What Starlab refuses to book

| Refusal | Why |
|---|---|
| "… has not passed a hardware test yet" | Only tests run on a real receiver until it's verified. |
| The receiver is already booked for an overlapping time | Each physical receiver is locked for the recording plus its setup and teardown time. Recordings on different receivers may overlap freely. |
| "reservation starts … less than 120 s from now" | A booking (including its setup time) must start at least 2 minutes ahead. |
| Longer than 4 hours, or more than 30 days ahead | Default limits. |
| Not enough storage | A job is refused if it would leave less free space than *Settings → Storage → Always keep free*, counting space already committed to other bookings. |
| Settings the receiver can't do | Frequency, sample rate, gain or bandwidth outside the receiver's capabilities. |
| A satellite pass already recorded | A satellite pass that's already booked needs **Record it again** to book more. Ticking several receivers in one booking doesn't need it. |

A running recording is never interrupted, and a conflicting request is rejected.

## Automatic recording

Automatic recording is **off after installation and stays off** until all of these are true:

1. at least one **rule** exists and is **enabled**;
2. the rule names a **verified** receiver (or a simulated one) that is connected and can use the rule's settings;
3. **Settings → Scheduling** is **automatic** or **hybrid**.

With Scheduling on **manual** (the default), enabled rules only produce **PREDICTED** jobs. They're shown in the
calendar, hold no receiver and never record. That's the safe way to see what a rule would do.

### Create a rule

1. **Scheduler → Automatic rules → New rule.**
2. Fill in the rule:

   | Field | Meaning |
   |---|---|
   | Rule name, **Enabled** | New rules start disabled. |
   | Satellites | From the catalogue, or add a NORAD number. |
   | Minimum peak elevation (°) | Passes peaking lower are ignored (default 20°). |
   | Priority (1–100) | A higher-priority rule may take a receiver from a lower one's job that hasn't started. |
   | Preferred receiver, Backup receivers | Tried in order. |
   | Frequency, Sample rate, Gain profile, Recording format | Empty: the satellite's downlink and the receiver's defaults. Gain profiles are set per receiver in the signal-path editor. |
   | Start before AOS / Stop after LOS (s) | Margins around the pass (default 30 s each). |
   | Decode automatically | Runs SatDump after the recording (needs SatDump installed). |
   | Keep raw IQ | Storage default, always, delete after a good decode, or for a number of hours. |
   | Storage destination | Under the allowed output roots. |

3. Press **Preview passes**. For the next 48 hours it shows which receiver would take each pass, and why others
   wouldn't.
4. Read the rule's warnings, for example: *"RX1 has not passed a hardware test (configured); no automatic recording
   on it until it is verified"*, or *"none of this rule's receivers can record its passes right now, so it will not
   book anything"*.
5. **Save**.

### Turn it on

1. Enable the rule and leave **Settings → Scheduling** on **manual**. Check the PREDICTED jobs on the **Calendar**.
2. When they look right, set **Settings → Scheduling** to **hybrid** (or automatic; the two behave the same).
3. Press **Re-plan now** on the Scheduler, or wait for the next planning cycle (every 5 minutes). Passes become
   **SCHEDULED** on their receivers.

*Book automatic passes this far ahead* (Settings → Scheduling, default 48 hours) sets how far ahead rules book.

### How the planner behaves

- A pass that new orbital elements move by a few seconds updates its existing job; no duplicate is added.
- **Never touched by the planner:** jobs starting within 5 minutes; jobs preparing, recording or finished; jobs a
  person cancelled; jobs a person changed.
- If the preferred receiver is busy, absent, unverified, short of storage, or can't use the settings, the backups
  are tried in order. A backup on a different antenna is used only if the signal-path diagram confirms that antenna
  covers the frequency.
- A pass no receiver can take becomes **SKIPPED**, with each receiver's reason. A later cycle books it if a receiver
  frees up.
- Manual bookings win over automatic ones: the automatic job is preempted (except within 3 minutes of its start)
  and retried on the rule's backups.
- Jobs of disabled or deleted rules, and passes no longer predicted, become SKIPPED, never deleted.
- A receiver whose automatic passes are configured elsewhere (in `starlab.toml`, or by an older recorder) is never
  used by rules.

### Change, skip or hand back a job

Open a job from the Calendar:

| Action | Effect |
|---|---|
| **Change** | Edit times, receiver, frequency, rate, gain, format, decoding, note. Validated like a new booking. A PREDICTED job becomes SCHEDULED. The planner keeps your changes from then on. |
| **Skip this recording** | CANCELLED. The rule never books that pass again. |
| **Hand back to the rule** | Clears your changes; the next cycle sets the job from its rule again. |

## Job states

| State | Meaning |
|---|---|
| PREDICTED | A rule would record it; nothing is booked (Scheduling is manual). |
| SCHEDULED | Booked; the receiver is reserved. |
| RESERVED | The receiver is being prepared. |
| RECORDING | Recording now. |
| PROCESSING | Recorded; the output is being checked, analysed and decoded. |
| COMPLETED | The output was validated. |
| SKIPPED | Not recorded, with the reason (busy, unavailable, no longer predicted, rule off). |
| CANCELLED | A person cancelled it, or stopped it while recording. |
| FAILED | The recording or its output failed, or Starlab wasn't running at the time. |

**Scheduled is not recorded.** A recording is COMPLETED only if its file exists, has the expected size (at least 98%
of the scheduled samples), and isn't flat or broken. Otherwise it's FAILED with the reason, and the file is kept for
inspection. The job's **Execution** card shows every start check, errors in plain language, and the end of the
recorder log. See [Troubleshooting](troubleshooting.md#a-recording-failed).

If Starlab is stopped when a recording should start, the job becomes FAILED (missed) when it starts again. A late
recording is never started, because it could collide with the next one.

## After recording

Recordings, decoding, decoder profiles, storage and raw-IQ retention are on the **Recordings** page. See
[ARCHIVE](ARCHIVE.md). By default, raw IQ of automatic recordings is kept for 48 hours, and manual recordings and
tests are kept until you delete them. Recordings marked **Keep** never expire.

Notifications can go to Slack: see [Slack integration](slack-integration.md).
