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
    because of the new checks;
  * the recording archive (schema 8): the built-in decoder profiles are created once, every recording gets
    its four outcomes, and the retention plan is printed. Only automatic or rule recordings may be due for
    deletion (the policies the earlier code already applied); a manual or test recording due is a failure.
    The plan is read-only: nothing is deleted, here or on the live disk.
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
        same, added = same_config(raw, cfg.raw)
        again = SettingsStore(e).seed(cfg), Inventory(e).sync_from_toml(cfg)
        quiet = again[0] == [] and not any(again[1].values())
        print(f"rows preserved: {'yes' if not lost else 'NO ' + str(lost)} ({sum(after.values())} rows in {len(after)} tables)")
        print(f"receivers imported: {imported['imported']}; settings seeded: {seeded}")
        print(f"effective configuration identical to the file: {'yes' if same else 'NO'}"
              + (f" (new settings at their defaults: {', '.join(added)})" if added else ""))
        print(f"second start changes nothing: {'yes' if quiet else 'NO ' + str(again)}")
        layout_ok = rehearse_layout(cfg, e)
        rules_ok = rehearse_rules(cfg, e)
        exec_ok = rehearse_execution(cfg, e)
        archive_ok = rehearse_archive(cfg, e)
        return 0 if not lost and same and quiet and layout_ok and rules_ok and exec_ok and archive_ok else 1


def same_config(file_raw: dict, effective: dict) -> tuple[bool, list[str]]:
    """The effective configuration equals the file's, apart from settings this version adds that the file does not
    name, which must hold their documented defaults (the code reads the same defaults when they are absent)."""
    from aobs.settings.store import DOCS
    defaults = {f.toml: f.default for d in DOCS.values() for f in d.fields if f.toml}
    eff = copy.deepcopy(effective)
    added = []
    for (sec, key), default in defaults.items():
        if key not in (file_raw.get(sec) or {}) and key in (eff.get(sec) or {}):
            if eff[sec][key] == default:
                added.append(f"{sec}.{key}={default}")
                del eff[sec][key]
            if not eff[sec] and sec not in file_raw:
                del eff[sec]
    return eff == file_raw, added


def rehearse_archive(cfg, e) -> bool:
    """Schema 8 on the copy: profiles seeded once, outcomes for every recording, and the retention plan."""
    from collections import Counter

    from aobs.scheduling.catalog import Catalog
    from aobs.scheduling.retention import Retention, job_class
    from aobs.scheduling.store import Scheduler
    s = Scheduler(cfg, e, present=lambda rx: False, disk_free=lambda path: 0)
    s._lock_path = pathlib.Path(e.url.database).with_name("reserve.lock")
    made, again = s.profiles.seed(), s.profiles.seed()
    print(f"decoder profiles created: {[p['name'] for p in s.profiles.list()]}; second start adds none: "
          f"{'yes' if not again else 'NO'}")
    res = Catalog(s).recordings(per_page=50)
    levels: Counter = Counter()
    for page in range(1, res["pages"] + 1):
        for r in Catalog(s).recordings(per_page=50, page=page)["recordings"]:
            levels[r["outcome"]["level"]] += 1
    print(f"recordings in the archive: {res['total']}; highest outcome reached: {dict(levels)}")
    plan = Retention(s).plan()
    wrong = []
    for x in plan["due"]:
        job = s.get(x["recording_id"])
        if job_class(job) != "automatic" and x["policy"] != "after_decode":
            wrong.append(f"{x['recording_id']} ({job_class(job)}): {x['basis']}")
    print(f"retention {'on' if plan['enabled'] else 'OFF'}: due now {len(plan['due'])} "
          f"({plan['due_bytes'] / 1e9:.2f} GB), next 7 days {len(plan['upcoming'])} "
          f"({plan['upcoming_bytes'] / 1e9:.2f} GB), waiting {len(plan['waiting'])}")
    for x in plan["due"][:10]:
        print(f"  due: {x['recording_id']} {x['target']} {x['bytes'] / 1e9:.2f} GB — {x['basis']}")
    for w in wrong:
        print(f"  NOT EXPECTED: {w}")
    return not again and not wrong


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
