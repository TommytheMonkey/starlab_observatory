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

from .schema import SCHEMA_VERSION, inv_events, inv_receivers, metadata, schema_info

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


MIGRATIONS: dict[int, tuple[str, Callable[[Connection], None]]] = {
    2: ("receiver inventory", _m0002_inventory),
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
