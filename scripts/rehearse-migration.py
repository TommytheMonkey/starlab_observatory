#!/usr/bin/env python3
"""Rehearse a database upgrade on a copy of the live database. Never writes to the live database.

    python scripts/rehearse-migration.py --config ~/auto-observe/config/observatory.toml

Copies the database (SQLite online backup, opened read-only) into a temporary directory, runs the
migration there, imports the TOML receivers, seeds the settings, and checks:
  * every existing table keeps its row count;
  * the effective configuration after seeding equals the file (no behaviour change on deploy);
  * a second start changes nothing;
  * the current signal-path layout still validates with this code, and the receiver fields derived from it
    (antenna, signal path, amplifier, filters) are printed so they can be compared with the running broker's;
  * the automatic settings in observatory.toml are imported as disabled rules, and a rule-planning cycle
    changes no existing job and books nothing (schema 6);
  * no receiver reservation exists after the upgrade, and every booked job that will execute passes the new
    recording-request validation and the start-time radio checks (schema 7), so no existing booking fails
    because of the new checks.
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
        rules_ok = rehearse_rules(cfg, e)
        exec_ok = rehearse_execution(cfg, e)
        return 0 if not lost and same and quiet and layout_ok and rules_ok and exec_ok else 1


def rehearse_execution(cfg, e) -> bool:
    """Schema 7 on the copy: no reservations yet; booked jobs pass the request validation and radio checks."""
    from aobs.db.schema import sched_reservations
    from aobs.scheduling.backends import BackendError, RecordingRequest
    from aobs.scheduling.hardware import validate_radio
    from aobs.scheduling.store import Scheduler
    s = Scheduler(cfg, e, present=lambda rx: False, disk_free=lambda path: 0, satdump_pipelines=frozenset())
    s._lock_path = pathlib.Path(e.url.database).with_name("reserve.lock")
    with e.connect() as c:
        held = c.execute(sched_reservations.select()).all()
    jobs = [j for j in s.jobs(status=["scheduled"], limit=5000) if j["executes"]]
    past = [j for j in s.jobs(status=["completed", "processing", "partial"], limit=5000)
            if j["executes"] and j["source"] in ("manual", "auto")]
    bad, old_bad = [], []
    for j in jobs:
        rx = s.receivers.get(j["receiver_id"])
        if rx is None:
            bad.append(f"{j['job_id']}: receiver {j['receiver_id']} unknown")
            continue
        try:
            req = RecordingRequest.from_job(j, rx, s.settings)
        except BackendError as exc:
            bad.append(f"{j['job_id']}: {exc}")
            continue
        if not j["spec"].get("steps"):
            errs: list = []
            validate_radio(rx, dict(j["spec"]["radio"]), j["spec"]["recording"].get("engine") or rx.engines()[0],
                           req.format, errs, [])
            bad += [f"{j['job_id']} ({j['receiver_id']}): {x}" for x in errs]
    for j in past:                     # recordings that succeeded before: would they still start today?
        rx = s.receivers.get(j["receiver_id"])
        if rx is None or not j.get("paths"):
            continue
        try:
            req = RecordingRequest.from_job(j, rx, s.settings)
            if not j["spec"].get("steps"):
                errs = []
                validate_radio(rx, dict(j["spec"]["radio"]),
                               j["spec"]["recording"].get("engine") or rx.engines()[0], req.format, errs, [])
                old_bad += [f"{j['job_id']} ({j['receiver_id']}): {x}" for x in errs]
        except BackendError as exc:
            old_bad.append(f"{j['job_id']}: {exc}")
    print(f"past successful broker recordings re-checked: {len(past)}; would still start: "
          f"{len(past) - len({b.split(' ')[0].rstrip(':') for b in old_bad})}")
    for b in old_bad[:10]:
        print(f"  {b}")
    remote = [r.id for r in s.receivers.values() if r.remote]
    print(f"reservations after the upgrade: {len(held)}; remote receivers: {remote or 'none'}")
    print(f"booked jobs that will execute: {len(jobs)}; pass the new start checks: "
          f"{'all' if not bad else 'NO'}")
    for b in bad:
        print(f"  {b}")
    return not held and not bad


def rehearse_rules(cfg, e) -> bool:
    """Rules on the copy: import the TOML's automatic settings, run one planning cycle, nothing may change."""
    from aobs.db.schema import sched_jobs
    from aobs.scheduling.rules import RulePlanner
    from aobs.scheduling.store import Scheduler
    s = Scheduler(cfg, e, present=lambda rx: False, disk_free=lambda path: 0, satdump_pipelines=frozenset())
    s._lock_path = pathlib.Path(e.url.database).with_name("reserve.lock")   # never the live broker's booking lock
    with e.connect() as c:
        before = [dict(r) for r in c.execute(sched_jobs.select().order_by(sched_jobs.c.job_id)).mappings()]
    made = s.rules.import_from_toml()
    again = s.rules.import_from_toml()
    out = RulePlanner(s).reconcile(refresh=False)
    with e.connect() as c:
        after = [dict(r) for r in c.execute(sched_jobs.select().order_by(sched_jobs.c.job_id)).mappings()]
    mode = ((s.config_store.current("scheduling") or {}).get("data") or {}).get("mode")
    rules = s.rules.list()
    print(f"rules imported from observatory.toml: {len(made)} ({', '.join(r['name'] for r in rules)}); "
          f"all disabled: {'yes' if not any(r['enabled'] for r in rules) else 'NO'}; second import adds none: "
          f"{'yes' if not again else 'NO'}")
    print(f"scheduling mode: {mode}; rule planning changed existing jobs: {'no' if before == after else 'YES'} "
          f"({len(after)} jobs); rule jobs created: {sum(1 for r in after if r.get('rule_id'))}")
    for w in out.get("warnings", []):
        print(f"  warning: {w}")
    return before == after and not again and not any(r["enabled"] for r in rules)


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
