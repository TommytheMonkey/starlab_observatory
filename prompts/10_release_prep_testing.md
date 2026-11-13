Prepare Starlab Observatory for its first portable release.

OBJECTIVE

Verify that the application can be installed and operated
by someone who has never used our development environment.

TEST SCENARIOS

1. Fresh installation on Ubuntu.
2. Fresh installation on Raspberry Pi OS.
3. Installation without SDR hardware.
4. Installation with one RTL-SDR.
5. Installation with multiple RTL-SDRs.
6. Installation with Airspy.
7. Remote receiver discovery over SSH.
8. Receiver disconnection and reconnection.
9. Automatic satellite scheduling.
10. Manual satellite scheduling.
11. Overlapping recording jobs.
12. Receiver busy errors.
13. Failed recording recovery.
14. Application restart during scheduling.
15. Configuration backup and restore.
16. Database migration.
17. Slack integration disabled.
18. Slack integration enabled.
19. Recording and decoding.
20. Storage cleanup and retention.

DOCUMENTATION

Create:

README.md
docs/installation.md
docs/first-run-setup.md
docs/receiver-configuration.md
docs/remote-receivers.md
docs/satellite-scheduling.md
docs/slack-integration.md
docs/troubleshooting.md
docs/backup-and-restore.md

RELEASE REQUIREMENTS

- No hardcoded developer-specific paths.
- No hardcoded geographic coordinates.
- No hardcoded receiver serial numbers.
- No plaintext credentials in the repository.
- No required Slack integration.
- No required Tailscale installation.
- No dependency on our specific homelab.
- No automatic recording until explicitly enabled.
- Persistent configuration across restarts.
- Clear diagnostics for hardware failures.
- Documented upgrade and rollback process.

Run all available tests.

Fix regressions.

Report any untested hardware-specific functionality
honestly.

DELIVERABLE

A release-readiness report identifying:

- Completed features
- Tested functionality
- Known limitations
- Remaining issues
- Installation instructions
- Recommended release version

Do not claim the application is production-ready
unless the relevant tests actually pass.