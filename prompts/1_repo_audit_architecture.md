You are working on Starlab Observatory, an existing self-hosted
satellite observation and SDR management application.

Repository:
~/auto-observe

OBJECTIVE

Prepare this application to become a portable, installable,
distributed satellite observatory platform.

Users should eventually be able to:

- Install Starlab on their own Linux machine.
- Connect local USB SDR receivers.
- Connect remote SDR receivers over SSH.
- Automatically discover supported receiver hardware.
- Configure antennas, filters, LNAs, and signal chains.
- Predict satellite passes using Skyfield.
- Automatically schedule recordings based on user-defined rules.
- Manually schedule satellite passes or arbitrary recordings.
- Prevent conflicting jobs from claiming the same receiver.
- Decode and archive recordings.
- Optionally integrate with Slack.
- Manage everything through the web interface.

IMPORTANT

This is an EXISTING application.

Do not rewrite it from scratch.

Do not delete existing functionality.

Do not change running systemd services or recording schedules
during this audit.

Do not assume the current architecture, framework, database,
or scheduler implementation.

Inspect the repository and establish what already exists.

TASKS

1. Examine the complete repository structure.

2. Identify:
   - Frontend framework
   - Backend framework
   - Database and persistence mechanisms
   - Configuration files
   - API endpoints
   - Satellite prediction implementation
   - Skyfield integration
   - Scheduling implementation
   - Receiver management
   - Recording scripts
   - systemd integration
   - SatDump integration
   - Slack integration
   - Authentication
   - Tests
   - Deployment configuration

3. Identify configuration currently stored only in frontend state,
   memory, temporary files, or other nonpersistent locations.

4. Identify any hardcoded references to:
   - /home/tommy
   - /data/observatory
   - observatory
   - monkeybrain
   - comet
   - Specific receiver serial numbers
   - Specific SDR devices
   - Specific geographic coordinates
   - Tailscale addresses
   - Local network addresses

5. Identify assumptions preventing installation on another machine.

6. Determine how recording jobs currently acquire SDR devices.

7. Identify how multiple scheduling systems could conflict.

8. Identify existing components that can be reused.

DELIVERABLES

Create:

docs/architecture/current-state.md
docs/architecture/target-architecture.md
docs/architecture/migration-plan.md

The migration plan must include:

- Incremental implementation phases
- Database migration requirements
- Backward compatibility considerations
- Service migration strategy
- Testing strategy
- Rollback procedures
- Known risks

Do not implement the new architecture yet.

Do not modify production services.

At completion, summarize findings and recommend the first
implementation step.