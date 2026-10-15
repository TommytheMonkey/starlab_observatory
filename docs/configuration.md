# Configuration

Starlab keeps its configuration in two places, each for a reason.

| Where | What | Why |
|---|---|---|
| `observatory.toml` (bootstrap) | data and database paths, listen address, allowed output roots, credential file paths, legacy integration, receivers' modes and automatic rules | needed before the database opens, or edited by the cutover scripts |
| the database (versioned) | everything below | edited in the web UI, kept with full history |

## In the database

| Document or store | Edited in | Contents |
|---|---|---|
| Observatory | Settings | name, description, display time zone, latitude/longitude/elevation (never in the repository) |
| Storage | Settings | output folders (under the allowed roots), automatic-recording retention, free-space reserve |
| Notifications | Settings | which notification kinds reach Slack |
| Integrations | Settings | Google Calendar on/off and ID, publishing jobs to it, antenna switch address |
| Interface | Settings | web defaults, such as the signal-test length |
| Signal paths | the diagram | antennas, parts, chains, switch ports (versioned since before this phase) |
| Receivers | Receivers | inventory, capabilities, readiness evidence |
| Manual jobs | Slack / API | the schedule |

Per-browser conveniences (unsaved drafts, the last opened tab) stay in the browser.

## Rules

- **Seeded once.** On the first start after the upgrade, each settings document is copied from `observatory.toml`.
  After that the database wins, and a later start never overwrites it with the file or with defaults. If the file
  and the database disagree, the Settings page says so.
- **Versioned.** Every save is a new version with author, time and the list of changes. Restoring an old version
  appends a copy, so nothing is lost.
- **Validated before writing.** A document is checked as a whole (types, ranges, time zone, paths under the allowed
  roots, no unknown keys) before anything is stored, so an invalid value cannot replace a valid one. Two people
  editing the same document: the second save is refused with "changed since you loaded it".
- **Confirmed by the server.** The page reports "Saved as version N" only after the server answers. Unsaved edits
  survive a refresh as a draft in the browser.
- **Live where possible.** Most settings apply at once. Integration settings marked "applies after a broker
  restart" are read when the broker starts.

## Export and import

*Settings → Download export* writes one JSON file with the settings documents, the signal-path layout and the
receivers added in the web UI. It never contains credentials (they stay in files on the server, and any key that
looks like a secret is dropped). It never contains hardware test evidence either: readiness is proven again on
the hardware it applies to.

*Import a file…* first shows exactly what would change and changes nothing; *Apply this import* then writes it.
An invalid file is refused as a whole. Receivers are never created by an import: they are listed to re-add from
the Receivers page once plugged in.

## Backups

- Before every schema upgrade the database is copied to `backups/` next to it (SQLite online backup).
- `aobs db backup` (or *Settings → Back up now*) makes a copy at any time, safely while the broker runs.
- `aobs db list` shows the backups; `aobs db status` shows the schema version without changing anything.
- `aobs db restore <file> --yes` replaces the database. The broker must be stopped first, and a safety copy of the
  current database is taken first.
