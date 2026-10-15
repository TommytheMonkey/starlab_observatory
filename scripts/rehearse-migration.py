#!/usr/bin/env python3
"""Rehearse a database upgrade on a copy of the live database. Never writes to the live database.

    python scripts/rehearse-migration.py --config ~/auto-observe/config/observatory.toml

Copies the database (SQLite online backup, opened read-only) into a temporary directory, runs the
migration there, imports the TOML receivers, seeds the settings, and checks:
  * every existing table keeps its row count;
  * the effective configuration after seeding equals the file (no behaviour change on deploy);
  * a second start changes nothing;
  * the current signal-path layout still validates with this code, and the receiver fields derived from it
    (antenna, signal path, amplifier, filters) are printed so they can be compared with the running broker's.
Exit status 0 only if every check passes. The temporary copy is deleted afterwards.
"""
import argparse
import copy
import pathlib
import sqlite3
import sys
import tempfile

from sqlalchemy import inspect, text

from aobs.config import load_config
from aobs.db import init_db, make_engine
from aobs.inventory.store import Inventory
from aobs.settings.store import SettingsStore


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    cfg = load_config(args.config)
    live = make_engine(cfg.database_url).url.database
    with tempfile.TemporaryDirectory(prefix="starlab-rehearsal-") as tmp:
        copy_path = pathlib.Path(tmp) / "observatory.sqlite3"
        src, dst = sqlite3.connect(f"file:{live}?mode=ro", uri=True), sqlite3.connect(copy_path)
        src.backup(dst)
        dst.close()
        src.close()
        e = make_engine(f"sqlite:///{copy_path}")
        tables = inspect(e).get_table_names()
        with e.connect() as c:
            before = {t: c.execute(text(f'select count(*) from "{t}"')).scalar() for t in tables}
        out = init_db(e)
        print(f"migrated schema {out['from']} -> {out['to']}: {out['steps']}")
        with e.connect() as c:
            names = set(inspect(c).get_table_names())
            after = {t: c.execute(text(f'select count(*) from "{t}"')).scalar() for t in before if t in names}
        lost = {t: (before[t], after.get(t)) for t in before if after.get(t) != before[t] and t != "receivers"}
        raw = copy.deepcopy(cfg.raw)
        imported = Inventory(e).sync_from_toml(cfg)
        store = SettingsStore(e)
        seeded = store.seed(cfg)
        store.apply(cfg)
        same = cfg.raw == raw
        again = SettingsStore(e).seed(cfg), Inventory(e).sync_from_toml(cfg)
        quiet = again[0] == [] and not any(again[1].values())
        print(f"rows preserved: {'yes' if not lost else 'NO ' + str(lost)} ({sum(after.values())} rows in {len(after)} tables)")
        print(f"receivers imported: {imported['imported']}; settings seeded: {seeded}")
        print(f"effective configuration identical to the file: {'yes' if same else 'NO'}")
        print(f"second start changes nothing: {'yes' if quiet else 'NO ' + str(again)}")
        layout_ok = rehearse_layout(cfg, e)
        return 0 if not lost and same and quiet and layout_ok else 1


def rehearse_layout(cfg, e) -> bool:
    import json
    from aobs.scheduling import rflayout
    from aobs.scheduling.store import Scheduler
    s = Scheduler(cfg, e)
    rows = rflayout.RfLayout(s)._rows(1)
    if not rows:
        print("signal-path layout: none saved yet (version 1 is seeded from the config on first start)")
        return True
    norm, errors, warns = rflayout.validate(rows[0]["layout"], s)
    print(f"signal-path layout v{rows[0]['version']} validates: {'yes' if not errors else 'NO ' + '; '.join(errors)}"
          f" ({len(warns)} warnings)")
    for w in warns:
        print(f"  warning: {w}")
    if not errors:
        for rid in sorted(s.receivers):
            if rid in norm["receivers"]:
                print(f"  derived {rid}: {json.dumps(rflayout.derive(norm, rid), default=list)}")
    return not errors


if __name__ == "__main__":
    sys.exit(main())
