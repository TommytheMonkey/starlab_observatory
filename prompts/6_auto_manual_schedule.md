Implement a unified satellite pass prediction and recording
scheduler for Starlab Observatory.

OBJECTIVE

Users must be able to:

1. Automatically schedule satellite passes using Skyfield.
2. Manually schedule predicted satellite passes.
3. Schedule arbitrary recordings at specific dates and times.
4. Override automatically generated recording jobs.
5. Configure recording rules per satellite.
6. Assign preferred and backup receivers.
7. View all jobs in a calendar.
8. Prevent duplicate and conflicting schedules.

PASS PREDICTION

Use Skyfield.

Support TLE and OMM orbital elements where available.

Allow configurable orbital-element sources.

Cache orbital data locally.

Record orbital-element timestamps.

Warn when orbital data becomes stale.

Calculate:

- AOS
- LOS
- Maximum elevation
- Time of maximum elevation
- Pass duration
- Azimuth at AOS
- Azimuth at LOS

Use the observatory's configured coordinates.

Store timestamps internally in UTC.

Display times in the user's configured timezone.

AUTOMATIC SCHEDULING

Users must be able to create persistent automation rules.

Each rule should support:

- Rule name
- Enabled/disabled
- One or more satellites
- Minimum maximum elevation
- Preferred receiver
- Backup receivers
- Frequency
- Sample rate
- Gain profile
- Recording format
- Pre-AOS recording margin
- Post-LOS recording margin
- Automatic decoding
- Raw IQ retention
- Recording priority
- Storage destination

When a rule is enabled, Starlab should generate future
recording jobs automatically.

MANUAL SCHEDULING

Provide an upcoming-passes interface.

Users should be able to select a predicted pass and click
Schedule Recording.

Prepopulate the recording form with pass information.

Allow manual overrides.

Also allow arbitrary recordings unrelated to satellite passes.

HYBRID MODE

Support automatic scheduling with manual overrides.

Manual changes must not be silently overwritten when
orbital data is refreshed.

SCHEDULE RECONCILIATION

Periodically refresh orbital elements.

Recalculate future passes.

Update eligible automatically generated jobs.

Preserve manual overrides.

Avoid duplicate jobs.

Do not modify recordings already in progress.

JOB STATES

Implement:

PREDICTED
SCHEDULED
RESERVED
RECORDING
PROCESSING
COMPLETED
SKIPPED
CANCELLED
FAILED

Record state transitions and timestamps.

USER INTERFACE

Create a Scheduler section with:

- Calendar view
- Upcoming passes
- Automatic rules
- Manual recordings
- Recording history

Allow users to filter by satellite, receiver, and status.

IMPORTANT

Scheduling a job does not mean the recording succeeded.

A job must be marked COMPLETED only after the recording
output has been validated.

TESTING

Test:

- Multiple overlapping passes
- Duplicate prevention
- Timezone handling
- Orbital-data refresh
- Manual overrides
- Missing orbital data
- Receiver unavailability
- Scheduler restart recovery

DELIVERABLE

A fully integrated Skyfield-based automatic, manual,
and hybrid recording scheduler.

Do not replace existing production scheduling until
the new implementation passes integration tests.