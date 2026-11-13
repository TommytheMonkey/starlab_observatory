Implement a portable installation system and first-run
configuration wizard for Starlab Observatory.

OBJECTIVE

A new user should be able to install Starlab on a supported
Linux computer without manually editing Python files,
systemd units, or hardcoded configuration paths.

SUPPORTED DEPLOYMENTS

Design for:

- Ubuntu / Debian Linux
- Raspberry Pi OS
- ARM64 Linux
- x86_64 Linux

Provide an architecture compatible with future containerized
deployments.

INSTALLER

Implement an installation process that:

1. Detects operating system and architecture.
2. Checks Python and required dependencies.
3. Checks available disk space.
4. Creates required directories.
5. Installs application dependencies.
6. Initializes the configuration database.
7. Creates necessary service definitions.
8. Configures appropriate permissions.
9. Starts the application.
10. Displays the local web interface URL.

Do not assume the username is tommy.

Do not assume installation under /home/tommy.

Do not assume recordings are stored under /data.

All installation paths must be configurable.

FIRST-RUN WIZARD

Create a setup wizard with these stages:

STEP 1 — Welcome
Explain Starlab and supported deployment models.

STEP 2 — Observatory Identity
Collect:
- Observatory name
- Description
- Latitude
- Longitude
- Elevation
- Timezone

STEP 3 — Receiver Hosts
Allow:
- Local USB devices
- Remote Linux machines over SSH
- Multiple receiver hosts

STEP 4 — Hardware Discovery
Discover available SDR receivers.

STEP 5 — Receiver Configuration
Configure hardware and signal chains.

STEP 6 — Satellite Configuration
Choose satellites and recording preferences.

STEP 7 — Scheduling
Choose automatic, manual, or hybrid scheduling.

STEP 8 — Integrations
Optional Slack and other integrations.

STEP 9 — Validation
Test configuration and receiver availability.

STEP 10 — Finish
Save configuration and activate Starlab.

IMPORTANT

The wizard must be resumable.

Progress must be persisted.

Users must be able to skip optional steps.

All settings must remain editable after setup.

Do not automatically start recording during installation.

Provide a simulation/demo mode when no SDR is connected.

TESTING

Test fresh installations using temporary directories
and isolated configuration databases.

Do not modify the existing production installation
during installer testing.

DELIVERABLE

Portable installer, first-run wizard, documentation,
and installation tests.