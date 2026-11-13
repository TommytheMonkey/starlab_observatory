# Backup and restore

This guide covers what to back up, the automatic and manual database backups, restoring a backup, moving
Starlab to a new computer, exporting and importing the configuration, and rolling back an upgrade.

`starlab` below means the command run as the service account; see
[Installation → Running Starlab commands](installation.md#running-starlab-commands). Paths are for a system install;
a user install keeps the same layout under `~/.local/share/starlab/data` and `~/.config/starlab`.

## What to back up

| What | Where | How |
|---|---|---|
| **Database**: settings and their history, receivers, rules, the schedule, job and test history, the recordings index, signal-path diagram, receiver hosts and their accepted keys | `/var/lib/starlab/db/starlab.sqlite3` | Starlab's database backups (below) |
| **Bootstrap configuration** | `/etc/starlab/starlab.toml` and `install.env` | Copy the files |
| **Secrets**: API token, Starlab's SSH key for receiver hosts, Slack tokens, Google credentials | `/var/lib/starlab/secrets/` | Copy securely (they're credentials); keep the mode 700/600 |
| **Recordings** (raw IQ, decoded images, plots) | `/var/lib/starlab/recordings/` and any other storage folders | Ordinary file backup; they can be large |

A database backup does **not** include the recording files, the secrets or `starlab.toml`. A configuration export
includes even less: see [Export and import](#configuration-export-and-import).

## Database backups

### Automatic backups

Starlab takes backups by itself:

- **Before every schema migration** (when a new version first starts on an older database):
  `db/backups/starlab-pre-migration-v<old>-v<new>-<time>.sqlite3`.
- **At the start of every upgrade** with `install.sh --upgrade`: `starlab-manual-<time>.sqlite3`.
- **Before every restore**, a safety copy of the database being replaced: `starlab-pre-restore-<time>.sqlite3`.

Each backup is a complete, consistent SQLite database, made with SQLite's online backup, so it's safe to take while
Starlab runs. Times in file names are UTC.

Starlab never deletes backups. Remove old ones yourself from `/var/lib/starlab/db/backups/` when you no longer need
them, but keep the latest `pre-migration` backup until you're sure you won't roll back.

### Back up now

- In the web page: **Settings → Database backups → Back up now**. The card lists the latest backups.
- On the server:

  ```bash
  starlab db backup        # prints the new file's path
  starlab db list          # every backup, newest first: time, size, file name
  starlab db status        # schema version and recent backups (read-only)
  ```

To keep a copy elsewhere, copy the backup file (for example with `rsync` or to another disk). It's a standalone
`.sqlite3` file.

Only SQLite databases (the default) can be backed up this way. With another database, use that database's own
tools.

### Restore a database backup

Restoring replaces the whole database: anything written after the backup was taken is lost (settings changes, new
jobs, test results). Recording files stay on disk.

1. Choose a moment when nothing is recording or about to start.
2. **Stop Starlab:**

   ```bash
   sudo systemctl stop starlab
   ```

   Always stop the service yourself. The restore command tries to detect a running Starlab, but don't rely on that.
3. Find the backup:

   ```bash
   starlab db list
   ```

4. Restore it. Give the file name as listed, which is looked up in the `backups/` folder, or an absolute path to a
   backup file elsewhere:

   ```bash
   starlab db restore starlab-manual-20261016T120000Z.sqlite3          # explains what it will do; changes nothing
   starlab db restore starlab-manual-20261016T120000Z.sqlite3 --yes    # does it
   ```

   Starlab first takes a safety backup of the current database (`starlab-pre-restore-…`), then copies the backup
   in. It prints the schema version restored and the safety backup's name.
5. Start Starlab:

   ```bash
   sudo systemctl start starlab
   ```

   If the backup has an older schema than the installed version, Starlab migrates it on start (taking a
   pre-migration backup first).

Starlab refuses to restore:

| Refusal | Why |
|---|---|
| "the broker is running (pid …); stop it first" | Replacing the database under a running Starlab would corrupt its view. |
| "… is not a Starlab database (no schema version)" | The file isn't a Starlab database. |
| "… is schema N, newer than this code (M)" | The backup came from a newer Starlab. Install that version first. |
| "only SQLite databases can be restored this way" | Use your database's own tools. |
| Without `--yes` | It only explains what it would do. |

Run `starlab db restore` as the service account (as shown), so new files keep the right owner.

## Configuration export and import

An export is a JSON file with what you configured in the web page. Use it to copy a setup to another Starlab, or to
keep a readable record.

### Export

**Settings → Configuration file → Download export.** The file `starlab-config-<time>.json` contains:

- the settings documents (Observatory, Storage, Notifications, Integrations, Slack, Simulation, Satellites,
  Scheduling, Interface), current version only;
- the signal-path layout (antennas, parts, chains, switch ports);
- the receivers added in the web page (ID, name, host, driver, serial, model, capabilities).

It **never** contains:

- **credentials**: they stay in files in the secrets folder, and any key that looks like a secret (token, password,
  private key, …) is dropped and listed under `not_included`;
- hardware test evidence: readiness has to be proven again on the hardware;
- recordings, tests and job history (they're in the database and its backups);
- receiver hosts and their accepted SSH keys;
- automatic rules and decoder profiles (they're in the database and its backups);
- receivers declared in `starlab.toml` (they come with that file).

The export does include the observatory's latitude and longitude, so treat the file as private.

### Import

1. **Settings → Configuration file → Import a file…** and choose the JSON file.
2. Starlab shows a **preview** of exactly what would change, document by document, and **changes nothing yet**.
3. Press **Apply this import** to write it. Each changed document becomes a new version, so you can go back with
   that document's **History**.

Starlab refuses an import:

- **"this is not a Starlab configuration export (format 1)"**: wrong file.
- **"this export comes from a newer Starlab (schema N); update this installation first"**.
- **As a whole, when any part is invalid** (for example a storage folder outside the allowed output roots, or an
  unknown setting). Nothing is written; the preview lists the errors.

Receivers are **never created** by an import. The preview lists each one as "already here" or "re-add from
Receivers when it is plugged in". A receiver has to be plugged in, discovered, added and verified on the new
computer.

## Moving Starlab to a new computer

To keep everything (history, schedule, recordings index):

1. On the old computer, stop Starlab (`sudo systemctl stop starlab`) and take a backup (`starlab db backup`).
2. Install Starlab on the new computer with the **same data folder path** (`--data-dir`), so that recording paths in
   the database stay valid. Don't open the setup wizard yet.
3. Stop the new service: `sudo systemctl stop starlab`.
4. Copy over:
   - the backup file, into the new `/var/lib/starlab/db/backups/`;
   - the secrets folder, if you want to keep the same SSH key (no need to re-authorize receiver hosts) and Slack or
     Google credentials. Keep ownership `starlab:starlab` and mode 700;
   - any `starlab.toml` changes you made (merge them; don't overwrite the new file blindly);
   - the recordings folders, if you want them.
5. Restore: `starlab db restore <backup file> --yes`, then `sudo systemctl start starlab`.
6. Discover and verify the receivers again on **Receivers**: a different computer may give them different USB
   ports.

To start fresh with the same settings instead, install normally, then import a configuration export.

## Rolling back an upgrade

Starlab never downgrades a database. An older version refuses to start on a database a newer version migrated
("Database schema N is newer than this code"). Rolling back therefore means restoring the backup taken before the
migration, **then** reinstalling the older version. Everything written after the upgrade is lost from the database;
recording files made since then stay on disk.

1. Choose a moment when nothing is recording. Stop Starlab:

   ```bash
   sudo systemctl stop starlab
   ```

2. With the **new version still installed** (it can read both schemas), find and restore the pre-migration backup:

   ```bash
   starlab db list                        # look for starlab-pre-migration-v<old>-v<new>-<time>.sqlite3
   starlab db restore starlab-pre-migration-v7-v8-20261016T120000Z.sqlite3 --yes
   ```

   If the upgrade didn't change the schema, there's no pre-migration backup and you can skip this step (or restore
   the `starlab-manual-…` backup the upgrade took, to undo later changes).
3. Get the previous version's source (`git checkout <previous tag>`, or the previous release archive) and install
   it:

   ```bash
   sudo ./install.sh --upgrade
   ```

   This backs up the restored database, installs the older code, finds the database already at its schema, and
   starts Starlab.
4. Check:

   ```bash
   cat /opt/starlab/VERSION       # the previous version
   starlab db status               # "state": "up to date"; schema_version equals code_schema_version
   journalctl -u starlab -n 50     # no "newer than this code" errors
   ```

Don't reinstall the older version **before** restoring. Its installer would try to migrate a database newer than
itself, fail, and leave Starlab stopped. If that happens, reinstall the newer version, then follow the steps above.
