"""Database engine setup."""

from __future__ import annotations

import pathlib

from sqlalchemy import Engine, create_engine, event, select

from .schema import SCHEMA_VERSION, metadata, schema_info


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


def init_db(engine: Engine) -> None:
    metadata.create_all(engine)
    with engine.begin() as conn:
        row = conn.execute(select(schema_info.c.value).where(schema_info.c.key == "schema_version")).first()
        if row is None:
            conn.execute(schema_info.insert().values(key="schema_version", value=str(SCHEMA_VERSION)))
        elif int(row.value) > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema {row.value} is newer than this code ({SCHEMA_VERSION})")
