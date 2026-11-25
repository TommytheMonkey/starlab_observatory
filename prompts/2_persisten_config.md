Implement durable, centralized configuration persistence
for Starlab Observatory.

FIRST

Read:

docs/architecture/current-state.md
docs/architecture/target-architecture.md
docs/architecture/migration-plan.md

Inspect the existing configuration implementation.

Do not introduce a second configuration system unnecessarily.

OBJECTIVE

All user-configurable settings must survive:

- Browser refresh
- Application restart
- Server reboot
- Application updates
- Receiver disconnection
- Temporary network outages

REQUIREMENTS

Persist:

1. Observatory identity and location
2. Receiver hosts
3. SDR hardware inventory
4. Receiver settings
5. Antenna inventory
6. Signal chains
7. Satellite selections
8. Automatic scheduling rules
9. Manual recording jobs
10. Decoder profiles
11. Storage preferences
12. Notification preferences
13. Integration settings
14. User interface preferences where appropriate

DATABASE

Reuse the existing database if suitable.

If no database exists, evaluate SQLite for standalone
installations and PostgreSQL for larger deployments.

Implement versioned database migrations.

CONFIGURATION API

Create or extend backend endpoints for:

- Reading configuration
- Updating configuration
- Validating configuration
- Exporting configuration
- Importing configuration
- Viewing configuration history
- Restoring previous configuration

FRONTEND

Add explicit Save and Cancel controls.

Display unsaved-change indicators.

Show successful save confirmation only after the backend
confirms persistence.

Prevent accidental loss of unsaved changes.

BACKUPS

Implement:

- Automatic backups before migrations
- Manual configuration export
- Configuration import
- Backup restoration
- Secret exclusion from exports

MIGRATION

Preserve existing user settings wherever possible.

Do not overwrite existing configuration with defaults.

TESTING

Verify that configuration survives application restarts.

Verify that invalid configuration cannot corrupt existing data.

Verify that migrations preserve existing records.

DELIVERABLE

A functioning persistent configuration system integrated
with the existing Starlab interface.

Do not proceed to the next implementation phase.