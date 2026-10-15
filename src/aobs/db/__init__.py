"""Database engine setup."""

from __future__ import annotations

import pathlib
from typing import Any

from sqlalchemy import Engine, create_engine, event


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///"):
        pathlib.Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=FULL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.close()
    return engine


def init_db(engine: Engine) -> dict[str, Any]:
    """Create a new database, or migrate an existing one (backup first). See migrate.py."""
    from .migrate import migrate
    return migrate(engine)
