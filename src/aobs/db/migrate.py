"""Versioned schema migrations, with an automatic backup before the first step.

A new database is created from the current metadata and stamped with SCHEMA_VERSION. An
existing one is brought forward one numbered step at a time. Before the first step, a
consistent copy is taken with SQLite's online backup API into `backups/` next to the
database file, so `aobs db restore` (or a plain file copy) can always go back. Every step is
idempotent (safe to run again after an interruption) and the version is stamped only after
the step succeeds. A database newer than the code is refused, never downgraded.
"""

from __future__ import annotations

import datetime as dt
import logging
import pathlib
import sqlite3
from typing import Any, Callable

from sqlalchemy import Connection, Engine, inspect, select, text

from .schema import (SCHEMA_VERSION, inv_events, inv_hosts, inv_receivers, metadata, rx_sessions, schema_info,
                     sched_rule_versions, sched_rules, settings_docs)

LOG = logging.getLogger("aobs.db.migrate")


def _m0002_inventory(conn: Connection) -> None:
    """Receiver inventory. The old `receivers` table was never written; keep it only if it holds rows."""
    names = set(inspect(conn).get_table_names())
    if "receivers" in names:
        if conn.execute(text("SELECT COUNT(*) FROM receivers")).scalar():
            conn.execute(text("ALTER TABLE receivers RENAME TO receivers_pre_v2"))
        else:
            conn.execute(text("DROP TABLE receivers"))
    inv_receivers.create(conn, checkfirst=True)
    inv_events.create(conn, checkfirst=True)


def _m0003_settings(conn: Connection) -> None:
    """Versioned settings documents; seeded from observatory.toml on the next start (settings.store)."""
    settings_docs.create(conn, checkfirst=True)


def _m0005_hosts(conn: Connection) -> None:
    """Remote receiver hosts, and a receiver identity that also covers devices without a serial.

    inv_receivers was unique on (host, driver, serial), so two serial-less devices (identified by USB port)
    could not both be added. SQLite cannot change a constraint in place: the table is rebuilt with
    identity_key ("sn:<serial>" or "port:<usb_path>") and an `identification` column, rows copied as-is."""
    inv_hosts.create(conn, checkfirst=True)
    cols = [c["name"] for c in inspect(conn).get_columns("inv_receivers")]
    if "identity_key" in cols:
        return                                       # created by step 2 from the current schema, or already done
    conn.execute(text("ALTER TABLE inv_receivers RENAME TO inv_receivers_v4"))
    inv_receivers.create(conn)
    keep = ", ".join(f'"{c}"' for c in cols)
    conn.execute(text(
        f"INSERT INTO inv_receivers ({keep}, identity_key) SELECT {keep}, "
        "CASE WHEN identity = 'usb_path' THEN 'port:' || COALESCE(usb_path, '') ELSE 'sn:' || serial END "
        "FROM inv_receivers_v4"))
    before = conn.execute(text("SELECT COUNT(*) FROM inv_receivers_v4")).scalar()
    after = conn.execute(text("SELECT COUNT(*) FROM inv_receivers")).scalar()
    if before != after:
        raise RuntimeError(f"receiver rebuild copied {after} of {before} rows; nothing was changed")
    conn.execute(text("DROP TABLE inv_receivers_v4"))


def _m0006_rules(conn: Connection) -> None:
    """Automation rules, and which rule generated a job / whether it was changed by hand. Columns are added
    (nullable), so existing jobs are untouched: rule_id NULL = not from a rule, override NULL = not changed."""
    sched_rules.create(conn, checkfirst=True)
    sched_rule_versions.create(conn, checkfirst=True)
    cols = {c["name"] for c in inspect(conn).get_columns("sched_jobs")}
    if "rule_id" not in cols:
        conn.execute(text("ALTER TABLE sched_jobs ADD COLUMN rule_id VARCHAR(32)"))
    if "override" not in cols:
        conn.execute(text("ALTER TABLE sched_jobs ADD COLUMN override TEXT"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sched_jobs_rule ON sched_jobs (rule_id)"))


MIGRATIONS: dict[int, tuple[str, Callable[[Connection], None]]] = {
    2: ("receiver inventory", _m0002_inventory),
    3: ("settings documents", _m0003_settings),
    4: ("listening sessions", lambda conn: rx_sessions.create(conn, checkfirst=True)),
    5: ("receiver hosts and identity keys", _m0005_hosts),
    6: ("automation rules", _m0006_rules),
}
assert max(MIGRATIONS) == SCHEMA_VERSION, "every schema version needs a migration step"


def current_version(engine: Engine) -> int | None:
    """None for a brand-new (empty) database."""
    with engine.connect() as conn:
        names = set(inspect(conn).get_table_names())
        if "schema_info" not in names:
            return 1 if names else None
        row = conn.execute(select(schema_info.c.value).where(schema_info.c.key == "schema_version")).first()
        return int(row.value) if row else (1 if names - {"schema_info"} else None)


def sqlite_path(engine: Engine) -> pathlib.Path | None:
    if engine.dialect.name != "sqlite":
        return None
    db = engine.url.database
    return pathlib.Path(db) if db and db != ":memory:" else None


def backup(engine: Engine, label: str, now: dt.datetime | None = None) -> pathlib.Path | None:
    """Consistent online copy (WAL-safe) next to the database: backups/<stem>-<label>-<time>.sqlite3."""
    src = sqlite_path(engine)
    if src is None or not src.exists():
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    dest_dir = src.parent / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{src.stem}-{label}-{now:%Y%m%dT%H%M%SZ}.sqlite3"
    a, b = sqlite3.connect(src), sqlite3.connect(dest)
    try:
        a.backup(b)
    finally:
        b.close()
        a.close()
    return dest


def _stamp(conn: Connection, version: int) -> None:
    conn.execute(schema_info.delete().where(schema_info.c.key == "schema_version"))
    conn.execute(schema_info.insert().values(key="schema_version", value=str(version)))


def migrate(engine: Engine) -> dict[str, Any]:
    found = current_version(engine)
    if found is None:
        metadata.create_all(engine)
        with engine.begin() as conn:
            _stamp(conn, SCHEMA_VERSION)
        return {"created": True, "from": None, "to": SCHEMA_VERSION, "backup": None, "steps": []}
    if found > SCHEMA_VERSION:
        raise RuntimeError(f"Database schema {found} is newer than this code ({SCHEMA_VERSION}); "
                           "install the newer Starlab version or restore an older backup")
    if found == SCHEMA_VERSION:
        metadata.create_all(engine)          # no-op on an up-to-date database
        return {"created": False, "from": found, "to": found, "backup": None, "steps": []}
    saved = backup(engine, f"pre-migration-v{found}-v{SCHEMA_VERSION}")
    LOG.warning("migrating database schema %s -> %s (backup: %s)", found, SCHEMA_VERSION, saved)
    steps = []
    for version in range(found + 1, SCHEMA_VERSION + 1):
        name, step = MIGRATIONS[version]
        with engine.begin() as conn:
            step(conn)
            _stamp(conn, version)
        steps.append(f"{version}: {name}")
        LOG.warning("database schema now %s (%s)", version, name)
    metadata.create_all(engine)
    return {"created": False, "from": found, "to": SCHEMA_VERSION, "backup": str(saved) if saved else None,
            "steps": steps}


def list_backups(engine: Engine) -> list[dict[str, Any]]:
    src = sqlite_path(engine)
    if src is None or not (src.parent / "backups").is_dir():
        return []
    out = []
    for f in sorted((src.parent / "backups").glob(f"{src.stem}-*.sqlite3"), reverse=True):
        st = f.stat()
        out.append({"file": f.name, "bytes": st.st_size,
                    "created_at": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).isoformat()})
    return out


def backup_version(path: pathlib.Path) -> int | None:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT value FROM schema_info WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def restore(engine: Engine, source: pathlib.Path) -> dict[str, Any]:
    """Replace the database with a backup. The broker must be stopped; a safety backup is taken first."""
    dest = sqlite_path(engine)
    if dest is None:
        raise RuntimeError("only SQLite databases can be restored this way")
    ver = backup_version(source)
    if ver is None:
        raise RuntimeError(f"{source} is not a Starlab database (no schema version)")
    if ver > SCHEMA_VERSION:
        raise RuntimeError(f"{source.name} is schema {ver}, newer than this code ({SCHEMA_VERSION})")
    safety = backup(engine, "pre-restore")
    engine.dispose()
    a, b = sqlite3.connect(f"file:{source}?mode=ro", uri=True), sqlite3.connect(dest)
    try:
        a.backup(b)
    finally:
        b.close()
        a.close()
    return {"restored_from": str(source), "schema_version": ver, "safety_backup": str(safety) if safety else None,
            "note": "the next start migrates it to the current schema" if ver < SCHEMA_VERSION else ""}
