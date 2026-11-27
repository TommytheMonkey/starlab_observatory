# Missions

A mission is a flight with a lifecycle. (Missions are stored as rows of the original `flights` table with added
columns; flights recorded before missions existed open as COMPLETE/ARMED/PREPARATION missions and keep their data.)

## States

```
PREPARATION ⇄ PREFLIGHT → ARMED ──launch──→ ASCENT ⇄ FLOAT / CRUISE
                  ↑ disarm ↓                   ↓  ↖      ↓
                                              DESCENT ⇄ LANDED (awaiting confirmation) ──confirm──→ COMPLETE
        any state before COMPLETE ──→ ABORTED (reason required)
```

| State | Recording | T+ | How it is entered |
|---|---|---|---|
| PREPARATION, PREFLIGHT | no | — | created; operator |
| ARMED | **yes**: telemetry is recorded from arming, before launch | — | operator (**Arm**) |
| ASCENT | yes | runs from the launch time | **launch detected** (automatic, only from ARMED) or **Launch now** |
| FLOAT / CRUISE, DESCENT | yes | runs | detected, or operator |
| LANDED | yes | runs | **possible landing** detected, or operator; leaves again automatically if it moves |
| COMPLETE | no | stops | operator only: **Confirm landing** (or Set state → COMPLETE) |
| ABORTED | no | stops | operator, with a reason |

Every change is logged on the timeline with its time, reason, who made it, and whether it was automatic. The
original Start/End buttons map to ARM and COMPLETE.

T+00:00:00 is the **launch time**, not the recording start: an automatic launch is back-dated to its estimate (see
below), a manual one is the moment *Launch now* was pressed. *Armed* shows the time since arming.

## Launch detection (ARMED only)

Nothing ever launches automatically unless an operator armed the mission.

1. **Baseline**: at least `baseline_min_fixes` (3) valid fixes on the ground, from arming (and the 30 minutes
   before it), whose altitudes agree (median absolute deviation ≤ `baseline_mad_m`). Baseline altitude and launch
   coordinates are their medians.
2. **Sustained climb**: the next `launch_confirm_fixes` (3) fixes are all more than `launch_gain_m` above the
   baseline and stay above half of it, and the Theil-Sen rate over the last ground fixes plus the climb is at least
   `launch_rate_mps`.
3. **Estimate**: launch time = first climbing fix − (its height above baseline ÷ the climb rate of the climbing
   fixes), never before the last ground fix. Recorded with the baseline coordinates and altitude.

GPS altitude spikes are removed first (below). An isolated spike, carrying the tracker around, a 30 m hill, or a
two-fix bump do not launch it (tests: `test_false_launches_rejected`, `test_two_fix_climb_is_not_enough_but_three_are`).

## Phases, burst, landing

All decisions use timestamped fixes (GPS time when it is within 10 minutes of reception), one per GPS fix (repeats
and the same frame heard by several stations count once), with spikes removed:

* **Spike**: a fix far (> `spike_m`, or 25 % of its neighbours' difference) from the line between its neighbours,
  reached and left by jumps that break the local trend by more than `spike_rate_mps` in opposite directions. A burst
  apex is reached at the trend's own rate, so it is kept.
* **Rates**: Theil-Sen (median of pairwise slopes) over `rate_window_s`, widened when fixes are sparse; confidence
  high/medium/low from the number of fixes, their span and the residuals.
* **Turns** (to DESCENT, back to ASCENT): the last `descent_confirm_fixes + 1` fixes, monotone, at the threshold
  rate, and the same must hold without the newest fix (the newest cannot yet be checked for a spike).
* **Burst** (latex, mylar): leaving ASCENT/FLOAT for DESCENT ≥ `burst_drop_m` below the maximum at ≥ `burst_rate_mps`.
  Logged as *possible balloon burst* at the time of the maximum. Pico balloons sink at night: that is DESCENT, never
  a burst.
* **Float**: ≥ 4 fixes over ≥ `float_min_s`, level (window rate and current rate within `float_rate_mps`, altitude
  spread small), at least `float_min_agl_m` above the launch site.
* **Possible landing** (from DESCENT): ≥ `landing_min_fixes` over ≥ `landing_min_s` that are level, within
  `landing_alt_range_m`, and slower than `landing_speed_mps` horizontally. The mission goes to LANDED and waits; if
  later fixes show it moving it returns to DESCENT/ASCENT (*moving again: landing not confirmed*).

Thresholds by balloon type (`mission.PROFILES`, override any per mission in "Detection and prediction thresholds"):

| | latex | mylar | pico | tethered |
|---|---|---|---|---|
| launch gain / rate | 60 m / 1.0 m/s | 50 m / 0.5 | 50 m / 0.3 | 10 m / 0.1 |
| rate window | 5 min | 10 min | 30 min | 3 min |
| float | ±0.4 m/s, 15 min, ≥ 3 km | ±0.3, 15 min, ≥ 1 km | ±0.25, 30 min, ≥ 1 km | ±0.1, 3 min, ≥ 8 m |
| descent | 2 m/s | 1 | 0.6 | 0.1 |
| burst | ≥ 300 m drop at ≥ 5 m/s | ≥ 500 m at ≥ 4 | never | never |
| landing | 5 min level | 10 min | 30 min | 2 min, back near the baseline |

Detection takes as long as the fixes take to arrive. On the public LongFast channel the firmware sends positions at
most every hour (see [reporting-intervals.md](reporting-intervals.md)), so a launch is detected hours late: use the
private channel, or **Launch now**.

## Contact, GPS fix, freshness

Kept apart from the flight phase:

* **RF contact**: any packet from the tracker (late after the mission's *late* threshold, lost after *lost*).
  Events: *RF contact lost*, *RF contact restored*.
* **GPS fix**: packets arriving but no valid fix for longer than the staleness limit → *GPS fix lost*, then
  *restored*.
* **Freshness** of the last fix: fresh / aging / stale, stale after `stale_factor` × the observed reporting interval
  (at least `stale_min_s`). While stale the phase is shown as **uncertain** and no automatic transition is made.

Silence is never evidence: losing contact never lands, bursts or changes the phase of a mission. The last valid
position stays on the map with its age, T+ keeps counting, and tracking resumes by itself when packets return; no
positions are interpolated across the gap (`test_contact_loss_is_never_landing_and_the_clock_keeps_running`).

## Timeline

Mission created and armed, launch (detected or manual), altitude milestones (per type, e.g. 1, 5, 10 ... 40 km for
latex), maximum altitude (when ascent ends), float, descent, possible burst, low battery (≤ 20 % or ≤ 3.45 V for
latex; once until it recovers), RF contact lost/restored, GPS fix lost/restored, possible landing, moving again,
landing confirmed, completed, aborted, and operator notes (**Add**). Detected events carry the time they happened
(a launch is back-dated) and when they were logged.

## Dashboard

The mission strip shows the state, T+, time armed, RF contact and GPS fix ages (separately), data freshness and phase
confidence, and the controls the state allows. The telemetry panel shows altitude (ft and m), height above the
launch site, ascent/descent rate (ft/min and m/s, with confidence), ground speed (mph and km/h), maxima (altitude,
ascent, descent, ground speed), distance travelled, downrange, distance/bearing/elevation from every station that
heard it, battery, last RF contact, last valid fix, observed reporting interval, and the last known position. On
screens ≥ 1600 px wide the timeline and prediction get a third column.

## Prediction

Optional, only when asked (**Predict** / **Recalculate from the latest fix**), using the Tawhiri predictor
(`prediction_url`, SondeHub's by default; set it to `""` to switch prediction off). Each run sends only the start
position, altitude, time and rates.

| State | Prediction |
|---|---|
| before launch | from a given site (or the latest fix), with the profile's ascent, burst and descent |
| ASCENT | from the latest fix, with the measured ascent rate when it is trustworthy |
| FLOAT | drift at the current altitude for `float_hours`; no landing |
| DESCENT | from the latest fix to the ground; the measured descent rate is converted to the sea-level rate Tawhiri expects (ISA density) |
| LANDED and later | none |

It refuses rather than guesses: no position, a fix over an hour old, no burst altitude, or an unreachable predictor
gives "unavailable" with the reason. Each prediction is re-run with burst ±10 %, ascent ±15 % and descent ±20 %; the
spread of those landings is drawn as the landing area. **It does not include wind-forecast error**, and the panel
says so, along with the prediction's age and the GFS run it used. Predictions are drawn dashed in amber, separate from
the observed track.

## Archive

At COMPLETE or ABORTED a summary is stored with the mission: times and durations, launch and landing sites, all
statistics, fix/packet/reception counts, battery range, receptions by ground station, the full timeline, and how far
the last prediction was from the actual landing. Flights → **Archive** shows it with map replay, CSV/GeoJSON/KML
exports and the summary as JSON. `GET /v1/stratolink/flights/ID/summary` builds one on demand for older flights.

## API

`GET flights/ID/mission`, `GET flights/ID/summary`, `POST flights/ID/arm|launch|state|confirm-landing|complete|abort|note|predict`
(see `control.py`). The daemon steps every armed or flying mission every `mission_step_s` (5 s).
