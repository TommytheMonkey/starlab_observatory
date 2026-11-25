Implement reliable distributed recording execution and
receiver resource management for Starlab Observatory.

OBJECTIVE

A scheduled recording must be able to execute on:

- The local Starlab host
- A remote Linux host over SSH
- A future Starlab receiver agent

RECEIVER OWNERSHIP

Implement exclusive receiver reservations.

Before starting a recording:

1. Verify receiver connectivity.
2. Verify supported frequency.
3. Verify sample rate.
4. Verify gain configuration.
5. Verify storage availability.
6. Verify receiver availability.
7. Acquire an exclusive reservation.
8. Start the recording.
9. Monitor recording progress.
10. Validate output.
11. Release the reservation.

Prevent two Starlab jobs from claiming the same receiver.

Use atomic reservations with expiration and recovery
for abandoned jobs.

Detect externally occupied devices where possible.

Do not assume Starlab controls every program that can
access an SDR.

DISTRIBUTED EXECUTION

Create a recording backend abstraction.

Support:

- Local process execution
- SSH execution
- Future receiver agents

Use structured recording requests and results.

Avoid building shell commands from unvalidated user input.

Support cancellation and timeout handling.

REMOTE HOSTS

Handle:

- SSH disconnection
- Remote host reboot
- Missing recording dependencies
- Insufficient disk space
- Permission errors
- USB device disconnection
- Receiver busy errors

RECORDING VALIDATION

Validate:

- File exists
- File is nonempty
- File size is plausible
- Sample count is consistent
- Recording duration is plausible
- Recorder exit status
- Device errors

Do not mark a recording successful solely because
a process started.

BACKUP RECEIVERS

When the preferred receiver is unavailable, attempt
a compatible configured backup receiver.

Respect priorities and user overrides.

Do not silently switch to incompatible antennas.

ERROR REPORTING

Translate common errors into actionable messages.

Example:

"OBS-R01 could not start because another process
has claimed its USB interface."

Include diagnostic logs and timestamps.

TESTING

Simulate:

- Receiver contention
- USB busy
- SSH disconnection
- Recorder crash
- Partial files
- Disk full
- Service restart

DELIVERABLE

Reliable distributed recording execution with
resource reservations, validation, and diagnostics.